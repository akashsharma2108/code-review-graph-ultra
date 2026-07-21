"""Authenticated HTTP service for sharing code-review-graph work capsules."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .team_protocol import MAX_REQUEST_BYTES
from .team_store import TeamStore

logger = logging.getLogger(__name__)


class RequestTooLargeError(ValueError):
    """Raised when an HTTP request exceeds the protocol payload limit."""


def is_loopback_host(host: str) -> bool:
    """Return whether a bind address only accepts local connections."""
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


class TeamHTTPServer(ThreadingHTTPServer):
    """Threading server carrying the shared :class:`TeamStore`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: TeamStore) -> None:
        self.team_store = store
        super().__init__(address, TeamRequestHandler)


class TeamRequestHandler(BaseHTTPRequestHandler):
    """Versioned JSON API with bearer-token organization isolation."""

    server_version = "CRGTeam/1"
    sys_version = ""
    # Store access is serialized inside TeamStore, so a slow or stalled client
    # only occupies its own handler thread and is dropped after this timeout.
    timeout = 30.0

    @property
    def store(self) -> TeamStore:
        return self.server.team_store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info(
            "team-api %s %s %s",
            self.client_address[0],
            self.command,
            urlsplit(self.path).path,
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/health":
            self._send(HTTPStatus.OK, {"status": "ok", "service": "crg-team", "version": 1})
            return
        organization = self._authenticate()
        if organization is None:
            return
        query = parse_qs(parsed.query, keep_blank_values=False)
        try:
            if parsed.path == "/v1/repositories":
                result = {"repositories": self.store.list_repositories(organization["id"])}
            elif parsed.path == "/v1/events":
                result = self.store.events(
                    organization["id"],
                    self._query(query, "repository", required=True),
                    after=self._query_int(query, "after", 0),
                    limit=self._query_int(query, "limit", 500),
                )
            elif parsed.path == "/v1/context":
                result = self.store.context(
                    organization["id"],
                    self._query(query, "repository", required=True),
                    developer=self._query(query, "developer"),
                    symbol=self._query(query, "symbol"),
                    commit=self._query(query, "commit"),
                    since=self._query(query, "since"),
                    limit=self._query_int(query, "limit", 20),
                )
            elif parsed.path == "/v1/activity":
                result = self.store.activity(
                    organization["id"],
                    self._query(query, "repository", required=True),
                    limit=self._query_int(query, "limit", 20),
                )
            elif parsed.path.startswith("/v1/capsules/"):
                capsule_id = parsed.path.removeprefix("/v1/capsules/")
                result = self.store.get_capsule(organization["id"], capsule_id)
                if result is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "Capsule not found"})
                    return
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
                return
            self._send(HTTPStatus.OK, result)
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled team API GET failure")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def do_POST(self) -> None:  # noqa: N802
        organization = self._authenticate()
        if organization is None:
            return
        try:
            payload = self._read_json()
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/repositories":
                result = self.store.register_repository(organization["id"], payload)
                result["organization"] = organization["slug"]
                status = HTTPStatus.OK
            elif parsed.path == "/v1/capsules":
                result = self.store.publish_capsule(organization["id"], payload)
                status = HTTPStatus.OK
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
                return
            self._send(status, result)
        except RequestTooLargeError as exc:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("Unhandled team API POST failure")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def _authenticate(self) -> dict[str, Any] | None:
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"error": "A bearer token is required"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return None
        organization = self.store.authenticate(token.strip())
        if organization is None:
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"error": "Invalid or revoked bearer token"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return None
        return organization

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("A valid Content-Length header is required") from exc
        if length <= 0:
            raise ValueError("Request body cannot be empty")
        if length > MAX_REQUEST_BYTES:
            raise RequestTooLargeError(f"Request body exceeds {MAX_REQUEST_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must contain valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request JSON must be an object")
        return payload

    @staticmethod
    def _query(
        query: dict[str, list[str]],
        name: str,
        *,
        required: bool = False,
    ) -> str:
        values = query.get(name) or []
        value = values[0].strip() if values else ""
        if required and not value:
            raise ValueError(f"Missing query parameter: {name}")
        return value

    @classmethod
    def _query_int(
        cls,
        query: dict[str, list[str]],
        name: str,
        default: int,
    ) -> int:
        raw = cls._query(query, name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"Query parameter {name} must be an integer") from exc

    def _send(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


def build_team_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    bootstrap_token: str = "",
    organization_slug: str = "default",
    organization_name: str = "Default Organization",
) -> TeamHTTPServer:
    """Create a configured server without starting its blocking loop."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = TeamStore(str(db_path))
    try:
        if bootstrap_token:
            store.bootstrap(
                organization_slug,
                organization_name,
                bootstrap_token,
            )
        if not store.has_tokens():
            raise ValueError(
                "The team server has no access tokens. Pass --bootstrap-token "
                "on first start or create one with `team token`."
            )
        return TeamHTTPServer((host, port), store)
    except BaseException:
        store.close()
        raise


def run_team_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    bootstrap_token: str = "",
    organization_slug: str = "default",
    organization_name: str = "Default Organization",
) -> None:
    """Run the central service until interrupted."""
    server = build_team_server(
        db_path,
        host,
        port,
        bootstrap_token=bootstrap_token,
        organization_slug=organization_slug,
        organization_name=organization_name,
    )
    if not is_loopback_host(host):
        logger.warning(
            "Team server is bound to %s and speaks plaintext HTTP: bearer tokens "
            "and capsule data cross the network unencrypted. Bind to 127.0.0.1 "
            "or terminate TLS in a reverse proxy in front of this port.",
            host,
        )
    logger.info("CRG team server listening on http://%s:%d", host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("CRG team server stopped")
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
