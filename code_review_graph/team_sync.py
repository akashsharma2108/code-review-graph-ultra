"""Client configuration, HTTP transport, and offline cache for team sync."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .incremental import get_data_dir
from .team_protocol import MAX_REQUEST_BYTES, escape_like

DEFAULT_TEAM_TIMEOUT = 15.0

# A capsule the server keeps rejecting is dead-lettered instead of retried on
# every lifecycle event forever. Auth and throttling statuses stay retryable
# because fixing the token or waiting resolves them without a payload change.
MAX_OUTBOX_ATTEMPTS = 20
_RETRYABLE_HTTP = {401, 403, 408, 429}


def _is_permanent_rejection(status: int) -> bool:
    return 400 <= status < 500 and status not in _RETRYABLE_HTTP


@dataclass
class TeamConfig:
    """Per-checkout team connection settings kept inside the ignored data dir."""

    server_url: str
    token: str
    repository_key: str
    organization: str = ""
    developer_id: str = ""
    developer_name: str = ""
    developer_email: str = ""
    last_cursor: int = 0

    @classmethod
    def path_for(cls, repo_root: Path) -> Path:
        return get_data_dir(repo_root) / "team.json"

    @classmethod
    def load(cls, repo_root: Path) -> "TeamConfig":
        path = cls.path_for(repo_root)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid team configuration at {path}: {exc}") from exc

        # Environment overrides make CI and container deployments stateless.
        overrides = {
            "server_url": os.environ.get("CRG_TEAM_SERVER"),
            "token": os.environ.get("CRG_TEAM_TOKEN"),
            "repository_key": os.environ.get("CRG_TEAM_REPOSITORY"),
            "developer_id": os.environ.get("CRG_TEAM_DEVELOPER"),
        }
        for key, value in overrides.items():
            if value:
                data[key] = value
        missing = [
            key
            for key in ("server_url", "token", "repository_key")
            if not str(data.get(key) or "").strip()
        ]
        if missing:
            raise ValueError(
                "Team sync is not configured. Run `code-review-graph team init` "
                f"(missing: {', '.join(missing)})."
            )
        return cls(
            server_url=str(data["server_url"]).rstrip("/"),
            token=str(data["token"]),
            repository_key=str(data["repository_key"]),
            organization=str(data.get("organization") or ""),
            developer_id=str(data.get("developer_id") or ""),
            developer_name=str(data.get("developer_name") or ""),
            developer_email=str(data.get("developer_email") or ""),
            last_cursor=max(0, int(data.get("last_cursor") or 0)),
        )

    def save(self, repo_root: Path) -> Path:
        path = self.path_for(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def save_enrollment(self, repo_root: Path) -> Path:
        """Persist auto-enrollment without copying an environment token to disk."""
        path = self.path_for(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("token", None)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def save_cursor(self, repo_root: Path) -> Path:
        """Persist only the cursor without writing environment-supplied secrets."""
        path = self.path_for(repo_root)
        if not path.exists():
            return path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid team configuration at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Invalid team configuration at {path}: expected an object")
        data["last_cursor"] = max(0, int(self.last_cursor))
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path


class TeamAPIError(RuntimeError):
    """A structured error returned by the team API or HTTP transport."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class TeamClient:
    """Small stdlib HTTP client so team sync adds no runtime dependency."""

    def __init__(
        self,
        server_url: str,
        token: str,
        timeout: float = DEFAULT_TEAM_TIMEOUT,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_config(cls, config: TeamConfig) -> "TeamClient":
        return cls(config.server_url, config.token)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health", authenticated=False)

    def register_repository(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/repositories", payload)

    def repositories(self) -> dict[str, Any]:
        return self._request("GET", "/v1/repositories")

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/capsules", payload)

    def events(
        self,
        repository_key: str,
        after: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/events",
            query={
                "repository": repository_key,
                "after": str(max(0, after)),
                "limit": str(limit),
            },
        )

    def context(
        self,
        repository_key: str,
        *,
        developer: str = "",
        symbol: str = "",
        commit: str = "",
        since: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        query = {
            "repository": repository_key,
            "developer": developer,
            "symbol": symbol,
            "commit": commit,
            "since": since,
            "limit": str(limit),
        }
        return self._request("GET", "/v1/context", query=query)

    def activity(self, repository_key: str, limit: int = 20) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/activity",
            query={
                "repository": repository_key,
                "limit": str(limit),
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.server_url}{path}"
        if query:
            clean_query = {key: value for key, value in query.items() if value != ""}
            url += "?" + urllib.parse.urlencode(clean_query)
        body = None
        headers = {"Accept": "application/json", "User-Agent": "code-review-graph/team-v1"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_REQUEST_BYTES:
                raise TeamAPIError(
                    f"Request body exceeds {MAX_REQUEST_BYTES} bytes",
                    status=413,
                )
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                error_data = json.loads(exc.read().decode("utf-8", errors="replace"))
                message = error_data.get("error") or error_data.get("message") or str(exc)
            except (json.JSONDecodeError, AttributeError):
                message = str(exc)
            raise TeamAPIError(message, status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TeamAPIError(f"Team server is unavailable: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TeamAPIError("Team server returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise TeamAPIError("Team server returned an unexpected response")
        return data


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_events (
    seq INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_capsules (
    id TEXT PRIMARY KEY,
    developer_external_id TEXT NOT NULL DEFAULT '',
    developer_name TEXT NOT NULL DEFAULT '',
    developer_email TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_symbols (
    capsule_id TEXT NOT NULL,
    symbol_key TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (capsule_id, symbol_key),
    FOREIGN KEY (capsule_id) REFERENCES cache_capsules(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS outbox (
    external_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_capsules_developer
    ON cache_capsules(developer_external_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_cache_capsules_commit
    ON cache_capsules(commit_sha);
CREATE INDEX IF NOT EXISTS idx_cache_symbols_key
    ON cache_symbols(symbol_key);
"""


class TeamCache:
    """Local read cache that makes team context available while offline."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_CACHE_SCHEMA)
        try:
            self._conn.execute(
                "ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
        except sqlite3.OperationalError:
            pass  # Caches created with the current schema already have it.

    @classmethod
    def for_repo(cls, repo_root: Path) -> "TeamCache":
        return cls(get_data_dir(repo_root) / "team-cache.db")

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def __enter__(self) -> "TeamCache":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def cursor(self) -> int:
        row = self._conn.execute("SELECT value FROM cache_metadata WHERE key='cursor'").fetchone()
        return int(row["value"]) if row else 0

    def metadata(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM cache_metadata WHERE key=?", (key,),
        ).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_or_create_metadata(self, key: str, value: str) -> str:
        """Atomically initialize metadata shared by concurrent hook processes."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO cache_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )
            row = self._conn.execute(
                "SELECT value FROM cache_metadata WHERE key=?", (key,),
            ).fetchone()
        return str(row["value"]) if row else value

    def delete_metadata(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM cache_metadata WHERE key=?", (key,))

    @property
    def outbox_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM outbox WHERE status='pending'"
        ).fetchone()
        return int(row["count"]) if row else 0

    @property
    def dead_letter_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM outbox WHERE status='dead'"
        ).fetchone()
        return int(row["count"]) if row else 0

    def enqueue(self, payload: dict[str, Any]) -> str:
        """Durably stage a capsule before attempting any network request."""
        external_id = str((payload.get("capsule") or {}).get("external_id") or "").strip()
        if not external_id:
            raise ValueError("A capsule external_id is required for durable publication.")
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._conn:
            # New content restarts delivery, including for a dead-lettered id.
            self._conn.execute(
                "INSERT INTO outbox "
                "(external_id, payload_json, attempts, last_error, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, 0, '', 'pending', ?, ?) "
                "ON CONFLICT(external_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, attempts=0, last_error='', "
                "status='pending', updated_at=excluded.updated_at",
                (external_id, encoded, now, now),
            )
        return external_id

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._outbox_rows("pending", limit)

    def dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._outbox_rows("dead", limit)

    def _outbox_rows(self, status: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT external_id, payload_json, attempts, last_error, created_at, updated_at "
            "FROM outbox WHERE status=? ORDER BY created_at, external_id LIMIT ?",
            (status, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [
            {
                "external_id": str(row["external_id"]),
                "payload": json.loads(row["payload_json"]),
                "attempts": int(row["attempts"]),
                "last_error": str(row["last_error"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def mark_sent(self, external_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM outbox WHERE external_id=?", (external_id,))

    def mark_failed(self, external_id: str, error: str, *, permanent: bool = False) -> bool:
        """Record a failure; return True when the entry was dead-lettered."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE outbox SET attempts=attempts+1, last_error=?, updated_at=? "
                "WHERE external_id=?",
                (error[:1000], now, external_id),
            )
            row = self._conn.execute(
                "SELECT attempts FROM outbox WHERE external_id=?",
                (external_id,),
            ).fetchone()
            if row is None:
                return False
            dead = permanent or int(row["attempts"]) >= MAX_OUTBOX_ATTEMPTS
            if dead:
                self._conn.execute(
                    "UPDATE outbox SET status='dead' WHERE external_id=?",
                    (external_id,),
                )
        return dead

    def apply_events(self, events: list[dict[str, Any]]) -> int:
        cursor = self.cursor
        with self._conn:
            for event in events:
                seq = int(event.get("seq") or 0)
                if seq <= 0:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache_events "
                    "(seq, event_type, entity_id, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        seq,
                        str(event.get("type") or ""),
                        str(event.get("entity_id") or ""),
                        json.dumps(payload),
                        str(event.get("created_at") or ""),
                    ),
                )
                if event.get("type") == "capsule.upserted":
                    self.upsert_capsule(payload, commit=False)
                cursor = max(cursor, seq)
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_metadata (key, value) VALUES ('cursor', ?)",
                (str(cursor),),
            )
        return cursor

    def upsert_capsule(self, capsule: dict[str, Any], *, commit: bool = True) -> None:
        capsule_id = str(capsule.get("id") or "")
        if not capsule_id:
            return
        developer = capsule.get("developer") or {}
        commit_data = capsule.get("commit") or {}
        self._conn.execute(
            "INSERT INTO cache_capsules "
            "(id, developer_external_id, developer_name, developer_email, commit_sha, "
            "updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "developer_external_id=excluded.developer_external_id, "
            "developer_name=excluded.developer_name, developer_email=excluded.developer_email, "
            "commit_sha=excluded.commit_sha, updated_at=excluded.updated_at, "
            "payload_json=excluded.payload_json",
            (
                capsule_id,
                str(developer.get("external_id") or ""),
                str(developer.get("display_name") or ""),
                str(developer.get("email") or ""),
                str(commit_data.get("sha") or ""),
                str(capsule.get("updated_at") or ""),
                json.dumps(capsule),
            ),
        )
        self._conn.execute("DELETE FROM cache_symbols WHERE capsule_id=?", (capsule_id,))
        for symbol in capsule.get("symbols") or []:
            if not isinstance(symbol, dict) or not symbol.get("symbol_key"):
                continue
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_symbols (capsule_id, symbol_key, file_path) "
                "VALUES (?, ?, ?)",
                (capsule_id, str(symbol["symbol_key"]), str(symbol.get("file_path") or "")),
            )
        if commit:
            self._conn.commit()

    def query(
        self,
        *,
        developer: str = "",
        symbol: str = "",
        commit: str = "",
        since: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if developer:
            like = f"%{escape_like(developer)}%"
            conditions.append(
                "(developer_external_id LIKE ? ESCAPE '\\' "
                "OR developer_name LIKE ? ESCAPE '\\' "
                "OR developer_email LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        if symbol:
            like = f"%{escape_like(symbol)}%"
            conditions.append(
                "EXISTS (SELECT 1 FROM cache_symbols s WHERE s.capsule_id=cache_capsules.id "
                "AND (s.symbol_key LIKE ? ESCAPE '\\' OR s.file_path LIKE ? ESCAPE '\\'))"
            )
            params.extend([like, like])
        if commit:
            conditions.append("commit_sha LIKE ? ESCAPE '\\'")
            params.append(f"{escape_like(commit)}%")
        if since:
            conditions.append("updated_at>=?")
            params.append(since)
        params.append(max(1, min(int(limit), 100)))
        rows = self._conn.execute(
            "SELECT payload_json FROM cache_capsules WHERE "
            + " AND ".join(conditions)
            + " ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]


def sync_events(
    repo_root: Path,
    config: TeamConfig,
    limit: int = 500,
    *,
    timeout: float = DEFAULT_TEAM_TIMEOUT,
) -> dict[str, Any]:
    """Download all available events after the local cursor."""
    client = TeamClient(config.server_url, config.token, timeout=timeout)
    total = 0
    with TeamCache.for_repo(repo_root) as cache:
        cursor = max(config.last_cursor, cache.cursor)
        for _ in range(100):
            response = client.events(config.repository_key, after=cursor, limit=limit)
            events = response.get("events") or []
            cursor = cache.apply_events(events)
            total += len(events)
            if not response.get("has_more") or not events:
                break
        config.last_cursor = cursor
        config.save_cursor(repo_root)
    return {"events_received": total, "cursor": cursor}


def publish_and_cache(
    repo_root: Path,
    config: TeamConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with TeamCache.for_repo(repo_root) as cache:
        external_id = cache.enqueue(payload)
    try:
        result = TeamClient.from_config(config).publish(payload)
    except TeamAPIError as exc:
        with TeamCache.for_repo(repo_root) as cache:
            cache.mark_failed(
                external_id,
                str(exc),
                permanent=_is_permanent_rejection(exc.status),
            )
        raise
    with TeamCache.for_repo(repo_root) as cache:
        cache.upsert_capsule(result)
        event_seq = result.get("event_seq")
        if isinstance(event_seq, int):
            cache.apply_events(
                [
                    {
                        "seq": event_seq,
                        "type": "capsule.upserted",
                        "entity_id": result.get("id", ""),
                        "payload": result,
                        "created_at": result.get("updated_at", ""),
                    }
                ]
            )
            config.last_cursor = max(config.last_cursor, event_seq)
            config.save_cursor(repo_root)
        cache.mark_sent(external_id)
    return result


def flush_outbox(
    repo_root: Path,
    config: TeamConfig,
    *,
    limit: int = 100,
    timeout: float = DEFAULT_TEAM_TIMEOUT,
) -> dict[str, Any]:
    """Publish queued capsules in order, retaining every failed item for retry."""
    client = TeamClient(config.server_url, config.token, timeout=timeout)
    sent = 0
    failed = 0
    dead = 0
    errors: list[str] = []
    with TeamCache.for_repo(repo_root) as cache:
        pending = cache.pending(limit=limit)
        for item in pending:
            external_id = item["external_id"]
            try:
                result = client.publish(item["payload"])
            except TeamAPIError as exc:
                dead += int(
                    cache.mark_failed(
                        external_id,
                        str(exc),
                        permanent=_is_permanent_rejection(exc.status),
                    )
                )
                failed += 1
                errors.append(str(exc))
                # An unavailable server will fail the remaining entries too. HTTP
                # validation failures are isolated, so continue past those.
                if exc.status == 0:
                    break
                continue
            cache.upsert_capsule(result)
            event_seq = result.get("event_seq")
            if isinstance(event_seq, int):
                cache.apply_events(
                    [
                        {
                            "seq": event_seq,
                            "type": "capsule.upserted",
                            "entity_id": result.get("id", ""),
                            "payload": result,
                            "created_at": result.get("updated_at", ""),
                        }
                    ]
                )
                config.last_cursor = max(config.last_cursor, event_seq)
            cache.mark_sent(external_id)
            sent += 1
        remaining = cache.outbox_count
        dead_total = cache.dead_letter_count
    config.save_cursor(repo_root)
    return {
        "attempted": sent + failed,
        "sent": sent,
        "failed": failed,
        "dead_lettered": dead,
        "dead_letters": dead_total,
        "remaining": remaining,
        "errors": errors[:5],
    }


def context_with_fallback(
    repo_root: Path,
    config: TeamConfig,
    *,
    developer: str = "",
    symbol: str = "",
    commit: str = "",
    since: str = "",
    limit: int = 20,
    offline: bool = False,
) -> dict[str, Any]:
    """Query the server, caching results, or transparently use offline data."""
    warning = ""
    if not offline:
        try:
            result = TeamClient.from_config(config).context(
                config.repository_key,
                developer=developer,
                symbol=symbol,
                commit=commit,
                since=since,
                limit=limit,
            )
            with TeamCache.for_repo(repo_root) as cache:
                for capsule in result.get("capsules") or []:
                    cache.upsert_capsule(capsule, commit=False)
                cache.commit()
            result["source"] = "server"
            return result
        except TeamAPIError as exc:
            warning = str(exc)
    with TeamCache.for_repo(repo_root) as cache:
        capsules = cache.query(
            developer=developer,
            symbol=symbol,
            commit=commit,
            since=since,
            limit=limit,
        )
    return {
        "repository_key": config.repository_key,
        "capsules": capsules,
        "count": len(capsules),
        "source": "offline-cache",
        "warning": warning,
    }
