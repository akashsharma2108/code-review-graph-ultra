"""Capture portable work capsules from Git and the local code graph."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .changes import _parse_unified_diff
from .graph import GraphNode, GraphStore
from .incremental import get_db_path

_GIT_TIMEOUT = 30


def _git(
    repo_root: Path,
    args: list[str],
    *,
    text: bool = True,
    check: bool = True,
) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise ValueError(f"git {' '.join(args)} failed: {stderr.strip()[:500]}")
    return result.stdout


def canonical_remote_url(remote_url: str) -> str:
    """Return a credential-free, stable repository remote identity."""
    value = remote_url.strip().replace("\\", "/")
    if "://" not in value and "@" in value.split(":", 1)[0] and ":" in value:
        host, path = value.split(":", 1)
        value = f"ssh://{host.split('@', 1)[-1]}/{path}"
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        return ""
    if "://" in value:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
        value = urlunsplit((parsed.scheme.lower(), hostname + port, parsed.path, "", ""))
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _remote_identity_material(canonical_url: str) -> str:
    """Normalize clone transport so SSH and HTTPS clones share one repository key."""
    if "://" not in canonical_url:
        return canonical_url
    parsed = urlsplit(canonical_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def repository_identity(repo_root: Path, explicit_key: str = "") -> dict[str, str]:
    """Derive the portable repository key, name, remote, and branch."""
    root = repo_root.resolve()
    remote = str(_git(root, ["config", "--get", "remote.origin.url"], check=False)).strip()
    canonical = canonical_remote_url(remote) if remote else ""
    if explicit_key:
        key = explicit_key.strip()
    elif canonical:
        identity_material = _remote_identity_material(canonical)
        digest = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:32]
        key = f"git:{digest}"
    else:
        key = f"local:{root.name.lower()}"
    branch = str(_git(root, ["branch", "--show-current"], check=False)).strip()
    return {
        "external_id": key,
        "name": root.name,
        "remote_url": canonical,
        "default_branch": branch,
    }


def developer_identity(
    repo_root: Path,
    *,
    external_id: str = "",
    display_name: str = "",
    email: str = "",
) -> dict[str, str]:
    """Resolve a developer identity from explicit values or Git config."""
    resolved_name = (
        display_name.strip() or str(_git(repo_root, ["config", "user.name"], check=False)).strip()
    )
    resolved_email = (
        email.strip().lower()
        or str(_git(repo_root, ["config", "user.email"], check=False)).strip().lower()
    )
    resolved_external = external_id.strip() or resolved_email or resolved_name
    if not resolved_external:
        raise ValueError(
            "Developer identity is missing. Configure git user.email or pass a developer ID."
        )
    return {
        "external_id": resolved_external,
        "display_name": resolved_name or resolved_external,
        "email": resolved_email,
    }


def _parse_name_status_z(raw: bytes) -> list[dict[str, Any]]:
    tokens = raw.split(b"\0")
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index].decode("utf-8", errors="replace")
        index += 1
        if not status:
            continue
        if index >= len(tokens):
            break
        code = status[0]
        old_path = ""
        if code in ("R", "C"):
            old_path = tokens[index].decode("utf-8", errors="replace")
            index += 1
            if index >= len(tokens):
                break
        path = tokens[index].decode("utf-8", errors="replace")
        index += 1
        change_types = {
            "A": "added",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "C": "copied",
            "T": "type_changed",
            "U": "unmerged",
        }
        files.append(
            {
                "path": path,
                "old_path": old_path,
                "change_type": change_types.get(code, "modified"),
                "additions": 0,
                "deletions": 0,
            }
        )
    return files


def _parse_numstat_z(raw: bytes) -> dict[str, tuple[int, int]]:
    tokens = raw.split(b"\0")
    counts: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        added_raw, deleted_raw, path_raw = fields
        added = int(added_raw) if added_raw.isdigit() else 0
        deleted = int(deleted_raw) if deleted_raw.isdigit() else 0
        if path_raw:
            path = path_raw.decode("utf-8", errors="replace")
        else:
            # Rename/copy form: the next two NUL fields are old and new path.
            if index + 1 >= len(tokens):
                break
            index += 1  # old path
            path = tokens[index].decode("utf-8", errors="replace")
            index += 1
        if path:
            counts[path] = (added, deleted)
    return counts


def _relative_path(repo_root: Path, file_path: str) -> str:
    path = Path(file_path)
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return file_path.replace("\\", "/").lstrip("/")


def _file_fingerprint(path: Path) -> tuple[str, int]:
    """Hash an untracked file incrementally and estimate its added line count."""
    digest = hashlib.sha256()
    newlines = 0
    last_byte = b""
    try:
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="replace"))
            return digest.hexdigest(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                newlines += chunk.count(b"\n")
                last_byte = chunk[-1:]
    except OSError:
        return digest.hexdigest(), 0
    additions = newlines + (1 if last_byte and last_byte != b"\n" else 0)
    return digest.hexdigest(), additions


def portable_symbol_key(repo_root: Path, node: GraphNode) -> str:
    """Convert an absolute local graph node into a portable symbol key."""
    file_path = _relative_path(repo_root, node.file_path)
    if node.kind == "File":
        return file_path
    owner = f"{node.parent_name}." if node.parent_name else ""
    return f"{file_path}::{owner}{node.name}"


def _portable_endpoint(repo_root: Path, qualified_name: str) -> str:
    if "::" in qualified_name:
        path, suffix = qualified_name.split("::", 1)
        return f"{_relative_path(repo_root, path)}::{suffix}"
    path = Path(qualified_name)
    if path.is_absolute():
        return _relative_path(repo_root, qualified_name)
    return qualified_name


def _map_symbols(
    repo_root: Path,
    files: list[dict[str, Any]],
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> list[dict[str, Any]]:
    def file_symbol(file_data: dict[str, Any]) -> dict[str, Any]:
        relative = file_data["path"]
        return {
            "symbol_key": relative,
            "qualified_name": relative,
            "file_path": relative,
            "kind": "File",
            "line_start": None,
            "line_end": None,
            "change_type": file_data["change_type"],
            "impact": {},
        }

    db_path = get_db_path(repo_root)
    if not db_path.exists():
        return [file_symbol(file_data) for file_data in files]
    store = GraphStore(db_path)
    try:
        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()
        for file_data in files:
            relative = file_data["path"]
            absolute = str((repo_root / relative).resolve())
            nodes = store.get_nodes_by_file(absolute)
            ranges = changed_ranges.get(relative, [])
            candidates = []
            for node in nodes:
                if node.kind == "File":
                    continue
                if ranges and not any(
                    node.line_start <= end and node.line_end >= start for start, end in ranges
                ):
                    continue
                candidates.append(node)
            # A deletion or stale graph may have no line overlap. Keep a file-level
            # symbol so the change remains queryable in the central history.
            if not candidates:
                key = relative
                if key not in seen:
                    seen.add(key)
                    symbols.append(file_symbol(file_data))
                continue
            for node in candidates:
                key = portable_symbol_key(repo_root, node)
                if key in seen:
                    continue
                seen.add(key)
                incoming = store.get_edges_by_target(node.qualified_name)
                outgoing = store.get_edges_by_source(node.qualified_name)
                tests = store.get_transitive_tests(node.qualified_name, max_depth=1)
                symbols.append(
                    {
                        "symbol_key": key,
                        "qualified_name": key,
                        "file_path": relative,
                        "kind": node.kind,
                        "line_start": node.line_start,
                        "line_end": node.line_end,
                        "change_type": file_data["change_type"],
                        "impact": {
                            "callers": [
                                _portable_endpoint(repo_root, edge.source_qualified)
                                for edge in incoming
                                if edge.kind == "CALLS"
                            ][:20],
                            "callees": [
                                _portable_endpoint(repo_root, edge.target_qualified)
                                for edge in outgoing
                                if edge.kind == "CALLS"
                            ][:20],
                            "tests": [
                                _portable_endpoint(repo_root, item.get("qualified_name", ""))
                                for item in tests[:20]
                                if item.get("qualified_name")
                            ],
                        },
                    }
                )
        return symbols
    finally:
        store.close()


def _common_capsule(
    *,
    repository_key: str,
    developer: dict[str, str],
    title: str,
    summary: str,
    intent: str,
    approach: str,
    outcome: str,
    status: str,
    agent_name: str,
    session_id: str,
    branch: str,
    base_sha: str,
    head_sha: str,
    external_id: str,
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    decisions: list[str] | None,
    open_questions: list[str] | None,
    tests: list[dict[str, Any]] | None,
    commit: dict[str, Any] | None,
) -> dict[str, Any]:
    session = None
    if session_id:
        session = {
            "external_id": session_id,
            "agent_name": agent_name,
            "branch": branch,
            "summary": summary,
        }
    return {
        "repository_key": repository_key,
        "developer": developer,
        "session": session,
        "commit": commit,
        "capsule": {
            "external_id": external_id,
            "title": title,
            "summary": summary,
            "intent": intent,
            "approach": approach,
            "outcome": outcome,
            "status": status,
            "agent_name": agent_name,
            "branch": branch,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "metadata": {"capture_version": 1},
        },
        "files": files,
        "symbols": symbols,
        "decisions": [{"summary": item} for item in decisions or []],
        "open_questions": [{"question": item, "status": "open"} for item in open_questions or []],
        "tests": tests or [],
    }


def capture_commit(
    repo_root: Path,
    repository_key: str,
    developer: dict[str, str],
    *,
    ref: str = "HEAD",
    summary: str = "",
    intent: str = "",
    approach: str = "",
    outcome: str = "",
    status: str = "completed",
    agent_name: str = "",
    session_id: str = "",
    decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    tests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture one Git commit with graph-mapped symbols and agent context."""
    root = repo_root.resolve()
    raw_meta = str(
        _git(
            root,
            ["show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%B", ref],
        )
    )
    parts = raw_meta.split("\0", 5)
    if len(parts) != 6:
        raise ValueError(f"Could not parse commit metadata for {ref}")
    sha, parents, author_name, author_email, authored_at, message = parts
    message = message.strip()
    title = message.splitlines()[0] if message else f"Commit {sha[:12]}"
    body = "\n".join(message.splitlines()[1:]).strip()
    branch = str(_git(root, ["branch", "--show-current"], check=False)).strip()

    parent_sha = parents.split()[0] if parents else ""
    if parent_sha:
        raw_status = _git(
            root,
            ["diff", "--name-status", "-M", "-z", parent_sha, sha, "--"],
            text=False,
        )
        raw_numstat = _git(
            root,
            ["diff", "--numstat", "-z", "--find-renames", parent_sha, sha, "--"],
            text=False,
        )
        diff = str(_git(root, ["diff", "--unified=0", parent_sha, sha, "--"]))
    else:
        raw_status = _git(
            root,
            ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-M", "-z", sha],
            text=False,
        )
        raw_numstat = _git(
            root,
            ["show", "--format=", "--numstat", "-z", "--find-renames", sha, "--"],
            text=False,
        )
        diff = str(_git(root, ["show", "--format=", "--unified=0", sha, "--"]))
    files = _parse_name_status_z(raw_status)
    counts = _parse_numstat_z(raw_numstat)
    for item in files:
        item["additions"], item["deletions"] = counts.get(item["path"], (0, 0))

    ranges = _parse_unified_diff(diff)
    symbols = _map_symbols(root, files, ranges)
    resolved_summary = summary.strip() or body or title
    commit = {
        "sha": sha,
        "parent_sha": parent_sha,
        "author_name": author_name,
        "author_email": author_email.lower(),
        "authored_at": authored_at,
        "message": message,
        "branch": branch,
    }
    author_developer = {
        "external_id": author_email.strip().lower() or author_name.strip(),
        "display_name": author_name.strip() or developer["display_name"],
        "email": author_email.strip().lower(),
    }
    return _common_capsule(
        repository_key=repository_key,
        developer=author_developer,
        title=title,
        summary=resolved_summary,
        intent=intent or body,
        approach=approach,
        outcome=outcome or f"Committed {len(files)} file(s) at {sha[:12]}.",
        status=status,
        agent_name=agent_name,
        session_id=session_id,
        branch=branch,
        base_sha=commit["parent_sha"],
        head_sha=sha,
        external_id=f"commit:{sha}",
        files=files,
        symbols=symbols,
        decisions=decisions,
        open_questions=open_questions,
        tests=tests,
        commit=commit,
    )


def list_commit_refs(
    repo_root: Path,
    revision_range: str,
    *,
    max_commits: int = 100,
) -> list[str]:
    """Return oldest-first commit SHAs for a Git revision or revision range."""
    revision = revision_range.strip()
    if not revision:
        raise ValueError("A Git revision range is required.")
    bounded = max(1, min(int(max_commits), 1000))
    output = str(
        _git(repo_root.resolve(), ["rev-list", "--reverse", f"--max-count={bounded}", revision])
    )
    commits = [line.strip() for line in output.splitlines() if line.strip()]
    if not commits:
        raise ValueError(f"No commits found for revision range: {revision}")
    return commits


def capture_worktree(
    repo_root: Path,
    repository_key: str,
    developer: dict[str, str],
    *,
    base: str = "HEAD",
    title: str = "Working-tree handoff",
    summary: str,
    intent: str = "",
    approach: str = "",
    outcome: str = "",
    status: str = "in_progress",
    agent_name: str = "",
    session_id: str = "",
    decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    tests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture staged and unstaged work before it has a commit."""
    root = repo_root.resolve()
    head = str(_git(root, ["rev-parse", "--verify", "HEAD"], check=False)).strip()
    branch = str(_git(root, ["branch", "--show-current"], check=False)).strip()
    if not head:
        # An unborn branch has no valid diff base. Every cached or untracked
        # path is new, so fingerprint the working copy without reading source
        # into the shared payload. This enables handoffs before the first commit.
        raw_paths = _git(
            root,
            ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            text=False,
        )
        files: list[dict[str, Any]] = []
        fingerprints: list[str] = []
        seen_paths: set[str] = set()
        for raw_path in raw_paths.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="replace")
            if path in seen_paths or path == ".code-review-graph" or path.startswith(
                ".code-review-graph/"
            ):
                continue
            seen_paths.add(path)
            content_hash, additions = _file_fingerprint(root / path)
            files.append(
                {
                    "path": path,
                    "old_path": "",
                    "change_type": "added",
                    "additions": additions,
                    "deletions": 0,
                }
            )
            fingerprints.append(f"{path}\0{content_hash}")
        if not files:
            raise ValueError("The working tree has no staged, unstaged, or untracked changes.")
        ranges = {
            item["path"]: [(1, max(1, int(item["additions"])))] for item in files
        }
        symbols = _map_symbols(root, files, ranges)
        fingerprint = hashlib.sha256(
            (developer["external_id"] + "\0" + "\0".join(fingerprints)).encode("utf-8")
        ).hexdigest()
        return _common_capsule(
            repository_key=repository_key,
            developer=developer,
            title=title,
            summary=summary,
            intent=intent,
            approach=approach,
            outcome=outcome,
            status=status,
            agent_name=agent_name,
            session_id=session_id or f"worktree:{fingerprint}",
            branch=branch,
            base_sha="",
            head_sha="",
            external_id=f"worktree:{fingerprint}",
            files=files,
            symbols=symbols,
            decisions=decisions,
            open_questions=open_questions,
            tests=tests,
            commit=None,
        )
    raw_status = _git(
        root,
        ["diff", "--name-status", "-M", "-z", base, "--"],
        text=False,
    )
    files = _parse_name_status_z(raw_status)
    raw_numstat = _git(
        root,
        ["diff", "--numstat", "-z", "--find-renames", base, "--"],
        text=False,
    )
    counts = _parse_numstat_z(raw_numstat)
    for item in files:
        item["additions"], item["deletions"] = counts.get(item["path"], (0, 0))
    tracked_paths = {item["path"] for item in files}
    untracked_raw = _git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        text=False,
    )
    untracked_fingerprints: list[str] = []
    for raw_path in untracked_raw.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="replace")
        if path == ".code-review-graph" or path.startswith(".code-review-graph/"):
            continue
        if path in tracked_paths:
            continue
        source_path = root / path
        content_hash, additions = _file_fingerprint(source_path)
        files.append(
            {
                "path": path,
                "old_path": "",
                "change_type": "added",
                "additions": additions,
                "deletions": 0,
            }
        )
        untracked_fingerprints.append(f"{path}\0{content_hash}")
    diff = str(_git(root, ["diff", "--unified=0", base, "--"]))
    ranges = _parse_unified_diff(diff)
    for item in files:
        if item["path"] not in ranges and item["change_type"] == "added":
            ranges[item["path"]] = [(1, max(1, int(item["additions"])))]
    symbols = _map_symbols(root, files, ranges)
    if not files:
        raise ValueError("The working tree has no staged, unstaged, or untracked changes.")
    fingerprint = hashlib.sha256(
        (developer["external_id"] + "\0" + diff + "\0" + "\0".join(untracked_fingerprints)).encode(
            "utf-8"
        )
    ).hexdigest()
    return _common_capsule(
        repository_key=repository_key,
        developer=developer,
        title=title,
        summary=summary,
        intent=intent,
        approach=approach,
        outcome=outcome,
        status=status,
        agent_name=agent_name,
        session_id=session_id or f"worktree:{fingerprint}",
        branch=branch,
        base_sha=str(_git(root, ["rev-parse", base])).strip(),
        head_sha=head,
        external_id=f"worktree:{fingerprint}",
        files=files,
        symbols=symbols,
        decisions=decisions,
        open_questions=open_questions,
        tests=tests,
        commit=None,
    )


def parse_test_specs(values: list[str] | None) -> list[dict[str, str]]:
    """Parse repeatable ``name=status`` CLI values into test records."""
    result: list[dict[str, str]] = []
    for value in values or []:
        name, separator, status = value.partition("=")
        result.append(
            {
                "name": name.strip(),
                "status": status.strip() if separator else "unknown",
                "command": "",
                "details": "",
            }
        )
    return result


def environment_agent_name(default: str = "") -> str:
    """Read an explicitly supplied agent name without guessing the client."""
    return os.environ.get("CRG_AGENT_NAME", default).strip()
