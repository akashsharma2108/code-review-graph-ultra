"""MCP/CLI-neutral functions for collaborative team context."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..incremental import find_project_root
from ..team_capture import (
    capture_commit,
    capture_worktree,
    developer_identity,
    environment_agent_name,
    list_commit_refs,
    parse_test_specs,
)
from ..team_sync import (
    TeamAPIError,
    TeamCache,
    TeamClient,
    TeamConfig,
    context_with_fallback,
    publish_and_cache,
    sync_events,
)


def _root(repo_root: str | None) -> Path:
    return find_project_root(Path(repo_root).expanduser() if repo_root else None).resolve()


def _config_and_developer(root: Path) -> tuple[TeamConfig, dict[str, str]]:
    config = TeamConfig.load(root)
    developer = developer_identity(
        root,
        external_id=config.developer_id,
        display_name=config.developer_name,
        email=config.developer_email,
    )
    return config, developer


def publish_work_capsule_func(
    *,
    repo_root: str | None = None,
    commit: str = "HEAD",
    working_tree: bool = False,
    title: str = "Working-tree handoff",
    summary: str = "",
    intent: str = "",
    approach: str = "",
    outcome: str = "",
    status: str | None = None,
    agent_name: str = "",
    session_id: str = "",
    decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    tests: list[str] | None = None,
) -> dict[str, Any]:
    """Capture a commit or worktree and publish its portable provenance."""
    try:
        root = _root(repo_root)
        config, developer = _config_and_developer(root)
        agent = agent_name.strip() or environment_agent_name()
        test_records = parse_test_specs(tests)
        effective_status = status or ("in_progress" if working_tree else "completed")
        if working_tree:
            if not summary.strip():
                return {
                    "status": "error",
                    "error": "A summary is required when publishing uncommitted work.",
                }
            payload = capture_worktree(
                root,
                config.repository_key,
                developer,
                title=title,
                summary=summary,
                intent=intent,
                approach=approach,
                outcome=outcome,
                status=effective_status,
                agent_name=agent,
                session_id=session_id,
                decisions=decisions,
                open_questions=open_questions,
                tests=test_records,
            )
        else:
            payload = capture_commit(
                root,
                config.repository_key,
                developer,
                ref=commit,
                summary=summary,
                intent=intent,
                approach=approach,
                outcome=outcome,
                status=effective_status,
                agent_name=agent,
                session_id=session_id,
                decisions=decisions,
                open_questions=open_questions,
                tests=test_records,
            )
        result = publish_and_cache(root, config, payload)
        return {
            "status": "ok",
            "summary": (
                f"Published work capsule '{result.get('title', '')}' with "
                f"{len(result.get('symbols') or [])} symbol(s)."
            ),
            "capsule": result,
        }
    except (ValueError, TeamAPIError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def publish_commit_range_func(
    revision_range: str,
    *,
    repo_root: str | None = None,
    max_commits: int = 100,
    agent_name: str = "",
) -> dict[str, Any]:
    """Backfill an oldest-first Git revision range into the shared history."""
    try:
        root = _root(repo_root)
        config, developer = _config_and_developer(root)
        agent = agent_name.strip() or environment_agent_name()
        commits = list_commit_refs(root, revision_range, max_commits=max_commits)
        published: list[dict[str, Any]] = []
        unchanged = 0
        for sha in commits:
            payload = capture_commit(
                root,
                config.repository_key,
                developer,
                ref=sha,
                agent_name=agent,
            )
            result = publish_and_cache(root, config, payload)
            if result.get("unchanged"):
                unchanged += 1
            published.append(
                {
                    "id": result.get("id"),
                    "commit": (result.get("commit") or {}).get("sha", sha),
                    "developer": result.get("developer"),
                    "title": result.get("title"),
                    "unchanged": bool(result.get("unchanged")),
                }
            )
        return {
            "status": "ok",
            "summary": (
                f"Processed {len(published)} commit(s); "
                f"{len(published) - unchanged} published and {unchanged} unchanged."
            ),
            "revision_range": revision_range,
            "processed": len(published),
            "published": len(published) - unchanged,
            "unchanged": unchanged,
            "capsules": published,
        }
    except (ValueError, TeamAPIError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def sync_team_context_func(
    *,
    repo_root: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Synchronize the local offline cache from the central event stream."""
    try:
        root = _root(repo_root)
        config = TeamConfig.load(root)
        result = sync_events(root, config, limit=limit)
        return {
            "status": "ok",
            "summary": (
                f"Received {result['events_received']} event(s); cursor is {result['cursor']}."
            ),
            **result,
        }
    except (ValueError, TeamAPIError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def get_team_context_func(
    *,
    repo_root: str | None = None,
    developer: str = "",
    symbol: str = "",
    commit: str = "",
    since: str = "",
    limit: int = 20,
    offline: bool = False,
) -> dict[str, Any]:
    """Retrieve portable handoff context by developer, symbol, or commit."""
    try:
        root = _root(repo_root)
        config = TeamConfig.load(root)
        result = context_with_fallback(
            root,
            config,
            developer=developer,
            symbol=symbol,
            commit=commit,
            since=since,
            limit=limit,
            offline=offline,
        )
        capsules = result.get("capsules") or []
        return {
            "status": "ok",
            "summary": (
                f"Found {len(capsules)} work capsule(s) from {result.get('source', 'unknown')}."
            ),
            **result,
        }
    except (ValueError, TeamAPIError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def get_developer_context_func(
    developer: str,
    *,
    repo_root: str | None = None,
    since: str = "",
    limit: int = 20,
    offline: bool = False,
) -> dict[str, Any]:
    return get_team_context_func(
        repo_root=repo_root,
        developer=developer,
        since=since,
        limit=limit,
        offline=offline,
    )


def get_symbol_history_func(
    symbol: str,
    *,
    repo_root: str | None = None,
    since: str = "",
    limit: int = 20,
    offline: bool = False,
) -> dict[str, Any]:
    return get_team_context_func(
        repo_root=repo_root,
        symbol=symbol,
        since=since,
        limit=limit,
        offline=offline,
    )


def list_team_activity_func(
    *,
    repo_root: str | None = None,
    limit: int = 20,
    offline: bool = False,
) -> dict[str, Any]:
    """List recent work and summarize activity by developer."""
    try:
        root = _root(repo_root)
        config = TeamConfig.load(root)
        if not offline:
            try:
                result = TeamClient.from_config(config).activity(
                    config.repository_key,
                    limit=limit,
                )
                return {
                    "status": "ok",
                    "summary": (
                        f"Found activity from {len(result.get('developers') or [])} developer(s)."
                    ),
                    "source": "server",
                    **result,
                }
            except TeamAPIError:
                pass
        with TeamCache.for_repo(root) as cache:
            capsules = cache.query(limit=limit)
        counts = Counter(
            (item.get("developer") or {}).get("external_id", "unknown") for item in capsules
        )
        return {
            "status": "ok",
            "summary": f"Found cached activity from {len(counts)} developer(s).",
            "source": "offline-cache",
            "developers": [
                {"external_id": developer, "capsule_count": count}
                for developer, count in counts.most_common()
            ],
            "recent_capsules": capsules,
        }
    except (ValueError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def team_status_func(*, repo_root: str | None = None) -> dict[str, Any]:
    """Report configuration, cache cursor, and central server health."""
    try:
        root = _root(repo_root)
        config = TeamConfig.load(root)
        with TeamCache.for_repo(root) as cache:
            cursor = cache.cursor
            outbox_count = cache.outbox_count
            pending = cache.pending(limit=5) if outbox_count else []
            dead_count = cache.dead_letter_count
            dead = cache.dead_letters(limit=5) if dead_count else []
        health: dict[str, Any]
        try:
            health = TeamClient.from_config(config).health()
            connected = health.get("status") == "ok"
        except TeamAPIError as exc:
            health = {"status": "unavailable", "error": str(exc)}
            connected = False
        return {
            "status": "ok",
            "connected": connected,
            "server_url": config.server_url,
            "repository_key": config.repository_key,
            "developer_id": config.developer_id,
            "cache_cursor": cursor,
            "outbox_count": outbox_count,
            "outbox_preview": [
                {
                    "external_id": item["external_id"],
                    "attempts": item["attempts"],
                    "last_error": item["last_error"],
                    "updated_at": item["updated_at"],
                }
                for item in pending
            ],
            "dead_letter_count": dead_count,
            "dead_letter_preview": [
                {
                    "external_id": item["external_id"],
                    "attempts": item["attempts"],
                    "last_error": item["last_error"],
                    "updated_at": item["updated_at"],
                }
                for item in dead
            ],
            "automation": "enabled",
            "server": health,
        }
    except (ValueError, OSError) as exc:
        return {"status": "error", "error": str(exc)}
