"""Fail-open, zero-touch capture for Git and coding-agent lifecycle events."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .team_capture import (
    capture_commit,
    capture_worktree,
    developer_identity,
    environment_agent_name,
    list_commit_refs,
    repository_identity,
)
from .team_sync import TeamAPIError, TeamCache, TeamClient, TeamConfig, flush_outbox, sync_events

_DEFAULT_AUTO_TIMEOUT = 3.0
_DEFAULT_CHECKPOINT_SECONDS = 60.0


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _auto_timeout() -> float:
    return _float_env("CRG_TEAM_AUTO_TIMEOUT", _DEFAULT_AUTO_TIMEOUT, 0.25, 15.0)


def _checkpoint_seconds() -> float:
    return _float_env(
        "CRG_TEAM_CHECKPOINT_SECONDS", _DEFAULT_CHECKPOINT_SECONDS, 0.0, 3600.0,
    )


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def ensure_team_config(repo_root: Path) -> tuple[TeamConfig | None, list[str]]:
    """Load Team Sync or enroll from environment variables without persisting secrets."""
    warnings: list[str] = []
    try:
        config = TeamConfig.load(repo_root)
    except ValueError:
        server_url = os.environ.get("CRG_TEAM_SERVER", "").strip().rstrip("/")
        token = os.environ.get("CRG_TEAM_TOKEN", "").strip()
        if not server_url or not token:
            return None, warnings
        repository = repository_identity(
            repo_root, os.environ.get("CRG_TEAM_REPOSITORY", "").strip(),
        )
        developer = developer_identity(
            repo_root,
            external_id=os.environ.get("CRG_TEAM_DEVELOPER", ""),
        )
        config = TeamConfig(
            server_url=server_url,
            token=token,
            repository_key=repository["external_id"],
            developer_id=developer["external_id"],
            developer_name=developer["display_name"],
            developer_email=developer["email"],
        )
        config.save_enrollment(repo_root)

    # Auto-enrolled checkouts have no organization until registration succeeds.
    # Retrying is bounded so an offline hook never hammers the server.
    if not config.organization:
        now = time.time()
        with TeamCache.for_repo(repo_root) as cache:
            retry_at = float(cache.metadata("auto.enrollment_retry_at", "0") or 0)
        if now >= retry_at:
            repository = repository_identity(repo_root, config.repository_key)
            try:
                registered = TeamClient(
                    config.server_url, config.token, timeout=_auto_timeout(),
                ).register_repository(repository)
                config.repository_key = str(registered["external_id"])
                config.organization = str(registered.get("organization") or "")
                if os.environ.get("CRG_TEAM_TOKEN"):
                    config.save_enrollment(repo_root)
                else:
                    config.save(repo_root)
                with TeamCache.for_repo(repo_root) as cache:
                    cache.set_metadata("auto.enrollment_retry_at", "0")
            except (TeamAPIError, ValueError, OSError) as exc:
                warnings.append(f"Enrollment deferred: {exc}")
                with TeamCache.for_repo(repo_root) as cache:
                    cache.set_metadata("auto.enrollment_retry_at", str(now + 30.0))
        else:
            warnings.append("Enrollment retry is deferred by offline backoff.")
    return config, warnings


def _developer(repo_root: Path, config: TeamConfig) -> dict[str, str]:
    return developer_identity(
        repo_root,
        external_id=config.developer_id,
        display_name=config.developer_name,
        email=config.developer_email,
    )


def _enqueue(repo_root: Path, payload: dict[str, Any]) -> str:
    with TeamCache.for_repo(repo_root) as cache:
        return cache.enqueue(payload)


def _checkout_id(repo_root: Path) -> str:
    """Return a stable, opaque ID that distinguishes simultaneous local clones."""
    with TeamCache.for_repo(repo_root) as cache:
        return cache.get_or_create_metadata("auto.checkout_id", str(uuid.uuid4()))


def _remember_worktree(repo_root: Path, payload: dict[str, Any]) -> None:
    with TeamCache.for_repo(repo_root) as cache:
        cache.set_metadata(
            "auto.worktree_payload",
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )


def _enqueue_completed_worktree(
    repo_root: Path,
    commit_payload: dict[str, Any],
) -> bool:
    """Close the checkout's live WIP record when its commit is created."""
    with TeamCache.for_repo(repo_root) as cache:
        encoded = cache.metadata("auto.worktree_payload")
        if not encoded:
            return False
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            cache.delete_metadata("auto.worktree_payload")
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("capsule"), dict):
            cache.delete_metadata("auto.worktree_payload")
            return False
        commit = commit_payload.get("commit") or {}
        sha = str(commit.get("sha") or "")
        capsule = payload["capsule"]
        capsule["status"] = "completed"
        capsule["head_sha"] = sha
        capsule["outcome"] = f"Superseded by commit {sha[:12]}." if sha else "Committed."
        capsule["metadata"] = {
            **(capsule.get("metadata") or {}),
            "live": False,
            "superseded_by_commit": sha,
        }
        if isinstance(payload.get("session"), dict):
            payload["session"]["summary"] = capsule["outcome"]
        cache.enqueue(payload)
        cache.delete_metadata("auto.worktree_payload")
    return True


def _automatic_worktree_payload(
    repo_root: Path,
    config: TeamConfig,
    agent_name: str,
) -> dict[str, Any]:
    payload = capture_worktree(
        repo_root,
        config.repository_key,
        _developer(repo_root, config),
        title="Automatic working-tree checkpoint",
        summary="Automatic checkpoint of the current uncommitted work.",
        status="in_progress",
        agent_name=agent_name,
    )
    capsule = payload["capsule"]
    original_external_id = str(capsule["external_id"])
    branch = str(capsule.get("branch") or "detached")
    stable_source = "\0".join(
        [
            config.repository_key,
            payload["developer"]["external_id"],
            branch,
            _checkout_id(repo_root),
        ]
    )
    stable_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()
    capsule["external_id"] = f"auto-worktree:{stable_id}"
    capsule["summary"] = (
        f"Automatic checkpoint of {len(payload.get('files') or [])} changed file(s) "
        f"on {branch}."
    )
    # No timestamp in the payload: an unchanged tree must hash to the same
    # capsule content so the server can deduplicate it without a new event.
    capsule["metadata"] = {
        **(capsule.get("metadata") or {}),
        "automatic": True,
        "source_event": "change",
        "content_fingerprint": original_external_id.removeprefix("worktree:"),
        "live": True,
    }
    if payload.get("session"):
        payload["session"]["external_id"] = capsule["external_id"]
        payload["session"]["summary"] = capsule["summary"]
    return payload


def _commit_payloads(
    repo_root: Path,
    config: TeamConfig,
    revision_range: str,
    agent_name: str,
) -> list[dict[str, Any]]:
    developer = _developer(repo_root, config)
    revision = revision_range.strip()
    if ".." in revision:
        refs = list_commit_refs(repo_root, revision, max_commits=1000)
    else:
        # Git's `rev-list HEAD` means the entire reachable history. Lifecycle
        # hooks use a bare revision to mean exactly that commit.
        refs = [_git(repo_root, "rev-parse", "--verify", revision)]
    return [
        capture_commit(
            repo_root,
            config.repository_key,
            developer,
            ref=ref,
            agent_name=agent_name,
        )
        for ref in refs
    ]


def _outgoing_range(repo_root: Path) -> str:
    try:
        upstream = _git(repo_root, "rev-parse", "--abbrev-ref", "@{upstream}")
    except ValueError:
        return "HEAD"
    return f"{upstream}..HEAD"


def _flush(repo_root: Path, config: TeamConfig, *, force: bool) -> dict[str, Any]:
    now = time.time()
    with TeamCache.for_repo(repo_root) as cache:
        retry_at = float(cache.metadata("auto.publish_retry_at", "0") or 0)
        remaining = cache.outbox_count
    if not force and now < retry_at:
        return {
            "attempted": 0,
            "sent": 0,
            "failed": 0,
            "remaining": remaining,
            "deferred": True,
            "errors": [],
        }
    result = flush_outbox(repo_root, config, timeout=_auto_timeout())
    with TeamCache.for_repo(repo_root) as cache:
        if result["failed"]:
            cache.set_metadata("auto.publish_retry_at", str(now + 30.0))
        else:
            cache.set_metadata("auto.publish_retry_at", "0")
    return result


def run_auto_event(
    event: str,
    *,
    repo_root: Path,
    revision_range: str = "",
    agent_name: str = "",
) -> dict[str, Any]:
    """Process one lifecycle event; all failures are reported but never raised."""
    root = repo_root.expanduser().resolve()
    normalized_event = event.strip().lower()
    agent = agent_name.strip() or environment_agent_name("automatic")
    try:
        config, warnings = ensure_team_config(root)
        if config is None:
            return {
                "status": "skipped",
                "event": normalized_event,
                "reason": "Team Sync is not configured; set CRG_TEAM_SERVER and CRG_TEAM_TOKEN.",
            }

        queued = 0
        flush: dict[str, Any] | None = None
        synchronized: dict[str, Any] | None = None
        if normalized_event == "change":
            try:
                payload = _automatic_worktree_payload(root, config, agent)
            except ValueError as exc:
                if "working tree has no" in str(exc).lower():
                    return {"status": "skipped", "event": normalized_event, "reason": str(exc)}
                raise
            _enqueue(root, payload)
            _remember_worktree(root, payload)
            queued = 1
            now = time.time()
            with TeamCache.for_repo(root) as cache:
                last_flush = float(cache.metadata("auto.checkpoint_at", "0") or 0)
            if now - last_flush >= _checkpoint_seconds():
                flush = _flush(root, config, force=False)
                if not flush.get("deferred"):
                    with TeamCache.for_repo(root) as cache:
                        cache.set_metadata("auto.checkpoint_at", str(now))
        elif normalized_event in {"post-commit", "post-rewrite", "ci"}:
            target = revision_range.strip() or "HEAD"
            payloads = _commit_payloads(root, config, target, agent)
            if normalized_event == "post-commit" and payloads:
                queued += int(_enqueue_completed_worktree(root, payloads[-1]))
            for payload in payloads:
                _enqueue(root, payload)
                queued += 1
            flush = _flush(root, config, force=True)
        elif normalized_event == "post-merge":
            target = revision_range.strip() or "ORIG_HEAD..HEAD"
            try:
                payloads = _commit_payloads(root, config, target, agent)
            except ValueError:
                payloads = _commit_payloads(root, config, "HEAD", agent)
            for payload in payloads:
                _enqueue(root, payload)
                queued += 1
            flush = _flush(root, config, force=True)
        elif normalized_event == "pre-push":
            target = revision_range.strip() or _outgoing_range(root)
            try:
                payloads = _commit_payloads(root, config, target, agent)
            except ValueError:
                payloads = _commit_payloads(root, config, "HEAD", agent)
            for payload in payloads:
                _enqueue(root, payload)
                queued += 1
            flush = _flush(root, config, force=True)
        elif normalized_event in {"session-start", "post-checkout", "flush"}:
            flush = _flush(root, config, force=normalized_event == "flush")
        else:
            return {
                "status": "degraded",
                "event": normalized_event,
                "error": f"Unsupported automatic event: {normalized_event}",
            }

        if normalized_event in {"session-start", "post-checkout", "post-merge"}:
            try:
                synchronized = sync_events(root, config, timeout=_auto_timeout())
            except (TeamAPIError, ValueError, OSError) as exc:
                warnings.append(f"Context sync deferred: {exc}")

        with TeamCache.for_repo(root) as cache:
            remaining = cache.outbox_count
        degraded = bool(warnings or (flush and flush.get("failed")))
        return {
            "status": "degraded" if degraded else "ok",
            "event": normalized_event,
            "queued": queued,
            "outbox_remaining": remaining,
            "publication": flush,
            "synchronization": synchronized,
            "warnings": warnings,
        }
    except Exception as exc:  # Hooks must never block Git or an agent session.
        return {
            "status": "degraded",
            "event": normalized_event,
            "error": str(exc),
        }
