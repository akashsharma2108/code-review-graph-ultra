"""Central persistence for collaborative code and agent provenance.

The existing :mod:`code_review_graph.graph` database describes one current
working tree.  This module deliberately uses a separate schema: it stores
repository-relative, temporal work records that can be shared by many
developers and many agent clients.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .team_protocol import escape_like

TEAM_SCHEMA_VERSION = 1
MAX_TEXT = 100_000


def utc_now() -> str:
    """Return an RFC 3339-compatible UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def generate_team_token() -> str:
    """Generate a high-entropy bearer token suitable for team API access."""
    return f"crg_team_{secrets.token_urlsafe(32)}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _text(value: Any, *, required: bool = False, max_len: int = MAX_TEXT) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError("A required text value is missing")
    if len(result) > max_len:
        raise ValueError(f"Text value exceeds the {max_len} character limit")
    return result


def normalize_repo_path(value: str) -> str:
    """Normalize and validate a repository-relative path.

    Central records must remain portable across developer checkouts.  Absolute
    paths and parent traversal are therefore rejected at the storage boundary.
    """
    raw = _text(value, required=True, max_len=4096).replace("\\", "/")
    if len(raw) >= 2 and raw[1] == ":":
        raise ValueError(f"Repository path must be relative: {value}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Repository path must be relative: {value}")
    normalized = str(path)
    if normalized in ("", "."):
        raise ValueError("Repository path cannot be empty")
    return normalized


def normalize_remote_url(value: str) -> str:
    """Remove credentials, query parameters, and fragments from a remote URL."""
    raw = _text(value, max_len=2000).replace("\\", "/")
    if not raw:
        return ""
    if "://" not in raw and "@" in raw.split(":", 1)[0] and ":" in raw:
        host, path = raw.split(":", 1)
        raw = f"ssh://{host.split('@', 1)[-1]}/{path}"
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return ""
    if "://" not in raw:
        return raw
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    if not hostname:
        raise ValueError("Repository remote URL must contain a hostname")
    return urlunsplit((parsed.scheme.lower(), hostname + port, parsed.path, "", ""))


def normalize_symbol_key(value: str, *, required: bool = True) -> str:
    """Validate that a symbol/file endpoint cannot contain a local absolute path."""
    raw = _text(value, required=required, max_len=5000).replace("\\", "/")
    if not raw:
        return ""
    path_part, separator, suffix = raw.partition("::")
    path_like = "/" in path_part or "." in PurePosixPath(path_part).name or bool(separator)
    if path_like:
        normalized = normalize_repo_path(path_part)
        if separator:
            clean_suffix = _text(suffix, required=True, max_len=4000)
            return f"{normalized}::{clean_suffix}"
        return normalized
    if len(path_part) >= 2 and path_part[1] == ":":
        raise ValueError(f"Symbol key must be portable: {value}")
    if ".." in PurePosixPath(path_part).parts:
        raise ValueError(f"Symbol key must be portable: {value}")
    return raw


def _normalized_impact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    for name in ("callers", "callees", "tests"):
        endpoints = result.get(name)
        if endpoints is None:
            continue
        if not isinstance(endpoints, list):
            raise ValueError(f"Symbol impact {name} must be a list")
        result[name] = [normalize_symbol_key(endpoint) for endpoint in endpoints]
    return result


_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    remote_url TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, external_id),
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS developers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, external_id),
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL,
    developer_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT '',
    agent_version TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repository_id, external_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id),
    FOREIGN KEY (developer_id) REFERENCES developers(id)
);

CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL,
    sha TEXT NOT NULL,
    parent_sha TEXT NOT NULL DEFAULT '',
    developer_id INTEGER,
    author_name TEXT NOT NULL DEFAULT '',
    author_email TEXT NOT NULL DEFAULT '',
    authored_at TEXT,
    message TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (repository_id, sha),
    FOREIGN KEY (repository_id) REFERENCES repositories(id),
    FOREIGN KEY (developer_id) REFERENCES developers(id)
);

CREATE TABLE IF NOT EXISTS capsules (
    id TEXT PRIMARY KEY,
    repository_id INTEGER NOT NULL,
    developer_id INTEGER NOT NULL,
    session_id INTEGER,
    commit_id INTEGER,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    approach TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    agent_name TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    base_sha TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repository_id, external_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id),
    FOREIGN KEY (developer_id) REFERENCES developers(id),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY (commit_id) REFERENCES commits(id)
);

CREATE TABLE IF NOT EXISTS capsule_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_id TEXT NOT NULL,
    path TEXT NOT NULL,
    old_path TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL DEFAULT 'modified',
    additions INTEGER NOT NULL DEFAULT 0,
    deletions INTEGER NOT NULL DEFAULT 0,
    UNIQUE (capsule_id, path),
    FOREIGN KEY (capsule_id) REFERENCES capsules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capsule_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_id TEXT NOT NULL,
    symbol_key TEXT NOT NULL,
    qualified_name TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    line_start INTEGER,
    line_end INTEGER,
    change_type TEXT NOT NULL DEFAULT 'modified',
    impact_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (capsule_id, symbol_key),
    FOREIGN KEY (capsule_id) REFERENCES capsules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capsule_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (capsule_id) REFERENCES capsules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capsule_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY (capsule_id) REFERENCES capsules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capsule_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (capsule_id) REFERENCES capsules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS team_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    repository_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (organization_id) REFERENCES organizations(id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id)
);

CREATE INDEX IF NOT EXISTS idx_team_events_repo_seq
    ON team_events(repository_id, seq);
CREATE INDEX IF NOT EXISTS idx_capsules_repo_time
    ON capsules(repository_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_capsule_symbols_key
    ON capsule_symbols(symbol_key);
CREATE INDEX IF NOT EXISTS idx_commits_repo_sha
    ON commits(repository_id, sha);
CREATE INDEX IF NOT EXISTS idx_developers_org_email
    ON developers(organization_id, email);
"""


class TeamStore:
    """SQLite store used by the central team-sync service."""

    def __init__(self, db_path: str | Path) -> None:
        resolved_path = Path(db_path).expanduser()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved_path)
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            resolved_path.chmod(0o600)
        except OSError:
            pass
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO team_metadata (key, value) VALUES (?, ?)",
            ("schema_version", str(TEAM_SCHEMA_VERSION)),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TeamStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def bootstrap(
        self,
        organization_slug: str,
        organization_name: str,
        token: str,
        token_name: str = "bootstrap",  # nosec B107 - token label, not a credential
    ) -> dict[str, Any]:
        """Create or reuse an organization and add an access token."""
        slug = _text(organization_slug, required=True, max_len=100).lower()
        name = _text(organization_name, required=True, max_len=200)
        if len(token) < 24:
            raise ValueError("Team access tokens must contain at least 24 characters")
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO organizations (slug, name, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(slug) DO UPDATE SET name=excluded.name",
                    (slug, name, now),
                )
                org = self._conn.execute(
                    "SELECT * FROM organizations WHERE slug=?",
                    (slug,),
                ).fetchone()
                assert org is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO api_tokens "
                    "(organization_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                    (
                        org["id"],
                        _text(token_name, required=True, max_len=100),
                        _token_hash(token),
                        now,
                    ),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return {"id": org["id"], "slug": org["slug"], "name": org["name"]}

    def create_token(self, organization_slug: str, name: str) -> str:
        """Create and persist a new token, returning its plaintext once."""
        token = generate_team_token()
        with self._lock:
            org = self._conn.execute(
                "SELECT id FROM organizations WHERE slug=?",
                (organization_slug.lower(),),
            ).fetchone()
            if org is None:
                raise ValueError(f"Unknown organization: {organization_slug}")
            self._conn.execute(
                "INSERT INTO api_tokens (organization_id, name, token_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    org["id"],
                    _text(name, required=True, max_len=100),
                    _token_hash(token),
                    utc_now(),
                ),
            )
        return token

    def revoke_token(self, organization_slug: str, name: str) -> bool:
        """Revoke every active token with a given organization-local name."""
        now = utc_now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE api_tokens SET revoked_at=? WHERE revoked_at IS NULL AND name=? "
                "AND organization_id=(SELECT id FROM organizations WHERE slug=?)",
                (
                    now,
                    _text(name, required=True, max_len=100),
                    _text(organization_slug, required=True, max_len=100).lower(),
                ),
            )
        return cursor.rowcount > 0

    def has_tokens(self) -> bool:
        """Return whether the server has at least one active access token."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM api_tokens WHERE revoked_at IS NULL LIMIT 1"
            ).fetchone()
        return row is not None

    def authenticate(self, token: str) -> dict[str, Any] | None:
        """Return the token's organization using constant-time hash comparison."""
        candidate = _token_hash(token)
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.token_hash, o.id, o.slug, o.name "
                "FROM api_tokens t JOIN organizations o ON o.id=t.organization_id "
                "WHERE t.revoked_at IS NULL"
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(row["token_hash"], candidate):
                return {"id": row["id"], "slug": row["slug"], "name": row["name"]}
        return None

    def register_repository(
        self,
        organization_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        external_id = _text(payload.get("external_id"), required=True, max_len=300)
        name = _text(payload.get("name"), required=True, max_len=300)
        remote_url = normalize_remote_url(payload.get("remote_url", ""))
        branch = _text(payload.get("default_branch"), max_len=300)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        now = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO repositories "
                "(organization_id, external_id, name, remote_url, default_branch, "
                "metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(organization_id, external_id) DO UPDATE SET "
                "name=excluded.name, remote_url=excluded.remote_url, "
                "default_branch=excluded.default_branch, metadata_json=excluded.metadata_json, "
                "updated_at=excluded.updated_at",
                (
                    organization_id,
                    external_id,
                    name,
                    remote_url,
                    branch,
                    _json(metadata),
                    now,
                    now,
                ),
            )
            row = self._repository_row(organization_id, external_id)
            assert row is not None
            return self._repository_dict(row)

    def list_repositories(self, organization_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM repositories WHERE organization_id=? ORDER BY name",
                (organization_id,),
            ).fetchall()
        return [self._repository_dict(row) for row in rows]

    def publish_capsule(
        self,
        organization_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently publish a complete work capsule and one sync event."""
        repository_key = _text(
            payload.get("repository_key"),
            required=True,
            max_len=300,
        )
        with self._lock:
            repo = self._repository_row(organization_id, repository_key)
        if repo is None:
            raise ValueError(f"Repository is not registered: {repository_key}")

        developer_data = payload.get("developer")
        if not isinstance(developer_data, dict):
            raise ValueError("Capsule developer must be an object")
        capsule_data = payload.get("capsule")
        if not isinstance(capsule_data, dict):
            raise ValueError("Capsule body must be an object")

        canonical_payload = dict(payload)
        content_hash = hashlib.sha256(_json(canonical_payload).encode("utf-8")).hexdigest()
        external_id = _text(
            capsule_data.get("external_id"),
            required=True,
            max_len=500,
        )
        now = utc_now()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                developer_id = self._upsert_developer(
                    organization_id,
                    developer_data,
                    now,
                )
                session_id = self._upsert_session(
                    repo["id"],
                    developer_id,
                    payload.get("session"),
                    now,
                )
                commit_id = self._upsert_commit(
                    repo["id"],
                    developer_id,
                    payload.get("commit"),
                    now,
                )

                existing = self._conn.execute(
                    "SELECT id, content_hash FROM capsules WHERE repository_id=? AND external_id=?",
                    (repo["id"], external_id),
                ).fetchone()
                if existing is not None and existing["content_hash"] == content_hash:
                    self._conn.commit()
                    result = self.get_capsule(organization_id, existing["id"])
                    assert result is not None
                    result["event_seq"] = None
                    result["unchanged"] = True
                    return result

                capsule_id = existing["id"] if existing is not None else str(uuid.uuid4())
                title = _text(capsule_data.get("title"), required=True, max_len=1000)
                summary = _text(capsule_data.get("summary"), required=True)
                status = _text(capsule_data.get("status") or "completed", max_len=50)
                if status not in {
                    "in_progress",
                    "completed",
                    "blocked",
                    "failed",
                    "abandoned",
                }:
                    raise ValueError(f"Unsupported capsule status: {status}")
                values = (
                    capsule_id,
                    repo["id"],
                    developer_id,
                    session_id,
                    commit_id,
                    external_id,
                    title,
                    summary,
                    _text(capsule_data.get("intent")),
                    _text(capsule_data.get("approach")),
                    _text(capsule_data.get("outcome")),
                    status,
                    _text(capsule_data.get("agent_name"), max_len=200),
                    _text(capsule_data.get("branch"), max_len=300),
                    _text(capsule_data.get("base_sha"), max_len=128),
                    _text(capsule_data.get("head_sha"), max_len=128),
                    capsule_data.get("started_at"),
                    capsule_data.get("ended_at"),
                    _json(capsule_data.get("metadata") or {}),
                    content_hash,
                    now,
                    now,
                )
                self._conn.execute(
                    "INSERT INTO capsules "
                    "(id, repository_id, developer_id, session_id, commit_id, external_id, "
                    "title, summary, intent, approach, outcome, status, agent_name, branch, "
                    "base_sha, head_sha, started_at, ended_at, metadata_json, content_hash, "
                    "created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(repository_id, external_id) DO UPDATE SET "
                    "developer_id=excluded.developer_id, session_id=excluded.session_id, "
                    "commit_id=excluded.commit_id, title=excluded.title, summary=excluded.summary, "
                    "intent=excluded.intent, approach=excluded.approach, outcome=excluded.outcome, "
                    "status=excluded.status, agent_name=excluded.agent_name, "
                    "branch=excluded.branch, "
                    "base_sha=excluded.base_sha, head_sha=excluded.head_sha, "
                    "started_at=excluded.started_at, ended_at=excluded.ended_at, "
                    "metadata_json=excluded.metadata_json, content_hash=excluded.content_hash, "
                    "updated_at=excluded.updated_at",
                    values,
                )
                self._replace_capsule_children(capsule_id, payload)
                full = self._get_capsule_by_id(capsule_id)
                self._conn.execute(
                    "INSERT INTO team_events "
                    "(organization_id, repository_id, event_type, entity_id, "
                    "payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (organization_id, repo["id"], "capsule.upserted", capsule_id, _json(full), now),
                )
                seq = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        full["event_seq"] = seq
        full["unchanged"] = False
        return full

    def get_capsule(
        self,
        organization_id: int,
        capsule_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT c.id FROM capsules c JOIN repositories r ON r.id=c.repository_id "
                "WHERE c.id=? AND r.organization_id=?",
                (capsule_id, organization_id),
            ).fetchone()
            return self._get_capsule_by_id(row["id"]) if row else None

    def events(
        self,
        organization_id: int,
        repository_key: str,
        after: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            repo = self._repository_row(organization_id, repository_key)
            if repo is None:
                raise ValueError(f"Repository is not registered: {repository_key}")
            rows = self._conn.execute(
                "SELECT * FROM team_events WHERE organization_id=? AND repository_id=? "
                "AND seq>? ORDER BY seq LIMIT ?",
                (organization_id, repo["id"], max(0, int(after)), bounded),
            ).fetchall()
        events = [
            {
                "seq": row["seq"],
                "type": row["event_type"],
                "entity_id": row["entity_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return {
            "repository_key": repository_key,
            "events": events,
            "cursor": events[-1]["seq"] if events else max(0, int(after)),
            "has_more": len(events) == bounded,
        }

    def context(
        self,
        organization_id: int,
        repository_key: str,
        *,
        developer: str = "",
        symbol: str = "",
        commit: str = "",
        since: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        with self._lock:
            repo = self._repository_row(organization_id, repository_key)
            if repo is None:
                raise ValueError(f"Repository is not registered: {repository_key}")
            conditions = ["c.repository_id=?"]
            params: list[Any] = [repo["id"]]
            if developer:
                like = f"%{escape_like(developer)}%"
                conditions.append(
                    "EXISTS (SELECT 1 FROM developers d WHERE d.id=c.developer_id "
                    "AND (d.external_id LIKE ? ESCAPE '\\' "
                    "OR d.display_name LIKE ? ESCAPE '\\' "
                    "OR d.email LIKE ? ESCAPE '\\'))"
                )
                params.extend([like, like, like])
            if symbol:
                like = f"%{escape_like(symbol)}%"
                conditions.append(
                    "EXISTS (SELECT 1 FROM capsule_symbols s WHERE s.capsule_id=c.id "
                    "AND (s.symbol_key LIKE ? ESCAPE '\\' "
                    "OR s.qualified_name LIKE ? ESCAPE '\\' "
                    "OR s.file_path LIKE ? ESCAPE '\\'))"
                )
                params.extend([like, like, like])
            if commit:
                conditions.append(
                    "EXISTS (SELECT 1 FROM commits co WHERE co.id=c.commit_id "
                    "AND co.sha LIKE ? ESCAPE '\\')"
                )
                params.append(f"{escape_like(commit)}%")
            if since:
                conditions.append("c.updated_at>=?")
                params.append(since)
            params.append(max(1, min(int(limit), 100)))
            rows = self._conn.execute(
                "SELECT c.id FROM capsules c WHERE "
                + " AND ".join(conditions)
                + " ORDER BY COALESCE(c.ended_at, c.updated_at) DESC LIMIT ?",
                params,
            ).fetchall()
            capsules = [self._get_capsule_by_id(row["id"]) for row in rows]
        return {
            "repository": self._repository_dict(repo),
            "filters": {
                "developer": developer,
                "symbol": symbol,
                "commit": commit,
                "since": since,
            },
            "capsules": capsules,
            "count": len(capsules),
        }

    def activity(
        self,
        organization_id: int,
        repository_key: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        with self._lock:
            repo = self._repository_row(organization_id, repository_key)
            if repo is None:
                raise ValueError(f"Repository is not registered: {repository_key}")
            rows = self._conn.execute(
                "SELECT d.external_id, d.display_name, d.email, COUNT(c.id) capsule_count, "
                "MAX(c.updated_at) last_activity "
                "FROM developers d JOIN capsules c ON c.developer_id=d.id "
                "WHERE c.repository_id=? GROUP BY d.id "
                "ORDER BY last_activity DESC LIMIT ?",
                (repo["id"], max(1, min(int(limit), 100))),
            ).fetchall()
            recent = self.context(
                organization_id,
                repository_key,
                limit=max(1, min(int(limit), 100)),
            )["capsules"]
        return {
            "repository": self._repository_dict(repo),
            "developers": [dict(row) for row in rows],
            "recent_capsules": recent,
        }

    def _repository_row(self, organization_id: int, external_id: str):
        return self._conn.execute(
            "SELECT * FROM repositories WHERE organization_id=? AND external_id=?",
            (organization_id, external_id),
        ).fetchone()

    @staticmethod
    def _repository_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "external_id": row["external_id"],
            "name": row["name"],
            "remote_url": row["remote_url"],
            "default_branch": row["default_branch"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _upsert_developer(
        self,
        organization_id: int,
        data: dict[str, Any],
        now: str,
    ) -> int:
        external_id = _text(data.get("external_id"), required=True, max_len=300)
        display_name = _text(
            data.get("display_name") or external_id,
            required=True,
            max_len=300,
        )
        email = _text(data.get("email"), max_len=500).lower()
        self._conn.execute(
            "INSERT INTO developers "
            "(organization_id, external_id, display_name, email, metadata_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(organization_id, external_id) DO UPDATE SET "
            "display_name=excluded.display_name, email=excluded.email, "
            "metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
            (
                organization_id,
                external_id,
                display_name,
                email,
                _json(data.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM developers WHERE organization_id=? AND external_id=?",
            (organization_id, external_id),
        ).fetchone()
        assert row is not None
        return row["id"]

    def _upsert_session(
        self,
        repository_id: int,
        developer_id: int,
        data: Any,
        now: str,
    ) -> int | None:
        if not isinstance(data, dict) or not data.get("external_id"):
            return None
        external_id = _text(data["external_id"], required=True, max_len=500)
        self._conn.execute(
            "INSERT INTO agent_sessions "
            "(repository_id, developer_id, external_id, agent_name, agent_version, branch, "
            "summary, started_at, ended_at, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repository_id, external_id) DO UPDATE SET "
            "developer_id=excluded.developer_id, agent_name=excluded.agent_name, "
            "agent_version=excluded.agent_version, branch=excluded.branch, "
            "summary=excluded.summary, "
            "started_at=COALESCE(agent_sessions.started_at, excluded.started_at), "
            "ended_at=excluded.ended_at, metadata_json=excluded.metadata_json, "
            "updated_at=excluded.updated_at",
            (
                repository_id,
                developer_id,
                external_id,
                _text(data.get("agent_name"), max_len=200),
                _text(data.get("agent_version"), max_len=200),
                _text(data.get("branch"), max_len=300),
                _text(data.get("summary")),
                data.get("started_at"),
                data.get("ended_at"),
                _json(data.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM agent_sessions WHERE repository_id=? AND external_id=?",
            (repository_id, external_id),
        ).fetchone()
        assert row is not None
        return row["id"]

    def _upsert_commit(
        self,
        repository_id: int,
        developer_id: int,
        data: Any,
        now: str,
    ) -> int | None:
        if not isinstance(data, dict) or not data.get("sha"):
            return None
        sha = _text(data["sha"], required=True, max_len=128)
        self._conn.execute(
            "INSERT INTO commits "
            "(repository_id, sha, parent_sha, developer_id, author_name, author_email, "
            "authored_at, message, branch, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repository_id, sha) DO UPDATE SET "
            "parent_sha=excluded.parent_sha, developer_id=excluded.developer_id, "
            "author_name=excluded.author_name, author_email=excluded.author_email, "
            "authored_at=excluded.authored_at, message=excluded.message, branch=excluded.branch",
            (
                repository_id,
                sha,
                _text(data.get("parent_sha"), max_len=128),
                developer_id,
                _text(data.get("author_name"), max_len=300),
                _text(data.get("author_email"), max_len=500).lower(),
                data.get("authored_at"),
                _text(data.get("message")),
                _text(data.get("branch"), max_len=300),
                now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM commits WHERE repository_id=? AND sha=?",
            (repository_id, sha),
        ).fetchone()
        assert row is not None
        return row["id"]

    def _replace_capsule_children(
        self,
        capsule_id: str,
        payload: dict[str, Any],
    ) -> None:
        for table in (
            "capsule_files",
            "capsule_symbols",
            "capsule_decisions",
            "capsule_questions",
            "capsule_tests",
        ):
            self._conn.execute(f"DELETE FROM {table} WHERE capsule_id=?", (capsule_id,))  # nosec B608

        for file_data in payload.get("files") or []:
            if not isinstance(file_data, dict):
                continue
            self._conn.execute(
                "INSERT INTO capsule_files "
                "(capsule_id, path, old_path, change_type, additions, deletions) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    capsule_id,
                    normalize_repo_path(file_data.get("path", "")),
                    normalize_repo_path(file_data["old_path"]) if file_data.get("old_path") else "",
                    _text(file_data.get("change_type") or "modified", max_len=50),
                    max(0, int(file_data.get("additions") or 0)),
                    max(0, int(file_data.get("deletions") or 0)),
                ),
            )
        for symbol in payload.get("symbols") or []:
            if not isinstance(symbol, dict):
                continue
            path = normalize_repo_path(symbol.get("file_path", ""))
            key = normalize_symbol_key(symbol.get("symbol_key", ""))
            self._conn.execute(
                "INSERT INTO capsule_symbols "
                "(capsule_id, symbol_key, qualified_name, file_path, kind, line_start, "
                "line_end, change_type, impact_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capsule_id,
                    key,
                    normalize_symbol_key(symbol.get("qualified_name") or key),
                    path,
                    _text(symbol.get("kind"), max_len=100),
                    symbol.get("line_start"),
                    symbol.get("line_end"),
                    _text(symbol.get("change_type") or "modified", max_len=50),
                    _json(_normalized_impact(symbol.get("impact"))),
                ),
            )
        for decision in payload.get("decisions") or []:
            data = decision if isinstance(decision, dict) else {"summary": decision}
            self._conn.execute(
                "INSERT INTO capsule_decisions "
                "(capsule_id, summary, rationale, alternatives_json) VALUES (?, ?, ?, ?)",
                (
                    capsule_id,
                    _text(data.get("summary"), required=True),
                    _text(data.get("rationale")),
                    _json(data.get("alternatives") or []),
                ),
            )
        for question in payload.get("open_questions") or []:
            data = question if isinstance(question, dict) else {"question": question}
            self._conn.execute(
                "INSERT INTO capsule_questions (capsule_id, question, status) VALUES (?, ?, ?)",
                (
                    capsule_id,
                    _text(data.get("question"), required=True),
                    _text(data.get("status") or "open", max_len=50),
                ),
            )
        for test in payload.get("tests") or []:
            data = test if isinstance(test, dict) else {"name": test, "status": "unknown"}
            self._conn.execute(
                "INSERT INTO capsule_tests "
                "(capsule_id, name, status, command, details) VALUES (?, ?, ?, ?, ?)",
                (
                    capsule_id,
                    _text(data.get("name"), required=True, max_len=1000),
                    _text(data.get("status") or "unknown", max_len=50),
                    _text(data.get("command"), max_len=5000),
                    _text(data.get("details")),
                ),
            )

    def _get_capsule_by_id(self, capsule_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT c.*, r.external_id repository_key, r.name repository_name, "
            "d.external_id developer_external_id, d.display_name, d.email, "
            "s.external_id session_external_id, s.agent_version, "
            "co.sha commit_sha, co.parent_sha, co.author_name, co.author_email, "
            "co.authored_at, co.message commit_message "
            "FROM capsules c JOIN repositories r ON r.id=c.repository_id "
            "JOIN developers d ON d.id=c.developer_id "
            "LEFT JOIN agent_sessions s ON s.id=c.session_id "
            "LEFT JOIN commits co ON co.id=c.commit_id WHERE c.id=?",
            (capsule_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown capsule: {capsule_id}")
        result: dict[str, Any] = {
            "id": row["id"],
            "external_id": row["external_id"],
            "repository_key": row["repository_key"],
            "repository_name": row["repository_name"],
            "developer": {
                "external_id": row["developer_external_id"],
                "display_name": row["display_name"],
                "email": row["email"],
            },
            "title": row["title"],
            "summary": row["summary"],
            "intent": row["intent"],
            "approach": row["approach"],
            "outcome": row["outcome"],
            "status": row["status"],
            "agent_name": row["agent_name"],
            "branch": row["branch"],
            "base_sha": row["base_sha"],
            "head_sha": row["head_sha"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["session_external_id"]:
            result["session"] = {
                "external_id": row["session_external_id"],
                "agent_name": row["agent_name"],
                "agent_version": row["agent_version"],
            }
        if row["commit_sha"]:
            result["commit"] = {
                "sha": row["commit_sha"],
                "parent_sha": row["parent_sha"],
                "author_name": row["author_name"],
                "author_email": row["author_email"],
                "authored_at": row["authored_at"],
                "message": row["commit_message"],
            }
        result["files"] = [
            dict(item)
            for item in self._conn.execute(
                "SELECT path, old_path, change_type, additions, deletions "
                "FROM capsule_files WHERE capsule_id=? ORDER BY path",
                (capsule_id,),
            ).fetchall()
        ]
        result["symbols"] = [
            {
                **{
                    key: item[key]
                    for key in (
                        "symbol_key",
                        "qualified_name",
                        "file_path",
                        "kind",
                        "line_start",
                        "line_end",
                        "change_type",
                    )
                },
                "impact": json.loads(item["impact_json"]),
            }
            for item in self._conn.execute(
                "SELECT * FROM capsule_symbols WHERE capsule_id=? ORDER BY file_path, line_start",
                (capsule_id,),
            ).fetchall()
        ]
        result["decisions"] = [
            {
                "summary": item["summary"],
                "rationale": item["rationale"],
                "alternatives": json.loads(item["alternatives_json"]),
            }
            for item in self._conn.execute(
                "SELECT * FROM capsule_decisions WHERE capsule_id=? ORDER BY id",
                (capsule_id,),
            ).fetchall()
        ]
        result["open_questions"] = [
            dict(item)
            for item in self._conn.execute(
                "SELECT question, status FROM capsule_questions WHERE capsule_id=? ORDER BY id",
                (capsule_id,),
            ).fetchall()
        ]
        result["tests"] = [
            dict(item)
            for item in self._conn.execute(
                "SELECT name, status, command, details FROM capsule_tests "
                "WHERE capsule_id=? ORDER BY id",
                (capsule_id,),
            ).fetchall()
        ]
        return result
