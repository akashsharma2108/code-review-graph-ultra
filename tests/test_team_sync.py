"""End-to-end coverage for the collaborative team-sync subsystem."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.skills import install_git_hook
from code_review_graph.team_automation import (
    _automatic_worktree_payload,
    ensure_team_config,
    run_auto_event,
)
from code_review_graph.team_capture import (
    canonical_remote_url,
    capture_commit,
    capture_worktree,
    repository_identity,
)
from code_review_graph.team_protocol import MAX_REQUEST_BYTES
from code_review_graph.team_server import build_team_server
from code_review_graph.team_store import TeamStore, generate_team_token, normalize_repo_path
from code_review_graph.team_sync import (
    TeamAPIError,
    TeamCache,
    TeamClient,
    TeamConfig,
    flush_outbox,
    publish_and_cache,
)
from code_review_graph.tools.team_tools import (
    get_developer_context_func,
    publish_commit_range_func,
    publish_work_capsule_func,
    sync_team_context_func,
)


def _repository() -> dict:
    return {
        "external_id": "repo:test",
        "name": "test-repository",
        "remote_url": "https://example.com/acme/test-repository",
        "default_branch": "main",
    }


def _capsule(*, summary: str = "Implemented the parser") -> dict:
    return {
        "repository_key": "repo:test",
        "developer": {
            "external_id": "dev@example.com",
            "display_name": "Developer One",
            "email": "dev@example.com",
        },
        "session": {
            "external_id": "session-1",
            "agent_name": "codex",
            "branch": "feature/parser",
            "summary": summary,
        },
        "commit": {
            "sha": "a" * 40,
            "parent_sha": "b" * 40,
            "author_name": "Developer One",
            "author_email": "dev@example.com",
            "authored_at": "2026-07-20T10:00:00+00:00",
            "message": "Implement parser",
            "branch": "feature/parser",
        },
        "capsule": {
            "external_id": f"commit:{'a' * 40}",
            "title": "Implement parser",
            "summary": summary,
            "intent": "Parse team context",
            "approach": "Added a temporal model",
            "outcome": "Parser is available",
            "status": "completed",
            "agent_name": "codex",
            "branch": "feature/parser",
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "metadata": {"capture_version": 1},
        },
        "files": [
            {
                "path": "src/parser.py",
                "old_path": "",
                "change_type": "modified",
                "additions": 12,
                "deletions": 2,
            }
        ],
        "symbols": [
            {
                "symbol_key": "src/parser.py::Parser.parse",
                "qualified_name": "src/parser.py::Parser.parse",
                "file_path": "src/parser.py",
                "kind": "Function",
                "line_start": 10,
                "line_end": 30,
                "change_type": "modified",
                "impact": {"callers": ["src/api.py::parse_request"]},
            }
        ],
        "decisions": [
            {
                "summary": "Keep source code out of the central database",
                "rationale": "The graph context is sufficient and more portable",
                "alternatives": ["Upload complete patches"],
            }
        ],
        "open_questions": [{"question": "Should old capsules expire?", "status": "open"}],
        "tests": [{"name": "pytest", "status": "passed", "command": "pytest"}],
    }


def test_store_publishes_idempotently_and_queries_all_dimensions(tmp_path):
    token = generate_team_token()
    with TeamStore(tmp_path / "nested" / "team.db") as store:
        org = store.bootstrap("acme", "Acme", token)
        store.register_repository(org["id"], _repository())

        first = store.publish_capsule(org["id"], _capsule())
        duplicate = store.publish_capsule(org["id"], _capsule())

        assert first["event_seq"] > 0
        assert duplicate["unchanged"] is True
        assert duplicate["event_seq"] is None
        assert first["files"][0]["path"] == "src/parser.py"
        assert first["decisions"][0]["summary"].startswith("Keep source")
        assert store.context(org["id"], "repo:test", developer="Developer One")["count"] == 1
        assert store.context(org["id"], "repo:test", symbol="Parser.parse")["count"] == 1
        assert store.context(org["id"], "repo:test", commit="aaaa")["count"] == 1
        assert store.events(org["id"], "repo:test", after=0)["cursor"] == first["event_seq"]

        updated = store.publish_capsule(
            org["id"], _capsule(summary="Implemented and documented the parser")
        )
        assert updated["id"] == first["id"]
        assert updated["event_seq"] > first["event_seq"]
        assert len(store.events(org["id"], "repo:test", after=0)["events"]) == 2


def test_store_rejects_nonportable_paths_and_isolates_organizations(tmp_path):
    with pytest.raises(ValueError, match="relative"):
        normalize_repo_path("/Users/alice/project/secret.py")
    with pytest.raises(ValueError, match="relative"):
        normalize_repo_path("../secret.py")

    with TeamStore(tmp_path / "team.db") as store:
        first = store.bootstrap("first", "First", generate_team_token())
        second = store.bootstrap("second", "Second", generate_team_token())
        repository = _repository()
        repository["remote_url"] = "https://alice:secret@example.com/acme/repo.git?token=x"
        registered = store.register_repository(first["id"], repository)
        assert registered["remote_url"] == "https://example.com/acme/repo.git"
        unsafe = _capsule()
        unsafe["symbols"][0]["impact"]["callers"] = ["/Users/alice/project/api.py::run"]
        with pytest.raises(ValueError, match="relative"):
            store.publish_capsule(first["id"], unsafe)
        store.publish_capsule(first["id"], _capsule())

        with pytest.raises(ValueError, match="not registered"):
            store.context(second["id"], "repo:test")


def test_store_keeps_multiple_repositories_separate_inside_one_organization(tmp_path):
    token = generate_team_token()
    with TeamStore(tmp_path / "team.db") as store:
        organization = store.bootstrap("acme", "Acme", token)
        first_repo = _repository()
        second_repo = {
            **_repository(),
            "external_id": "repo:dashboard",
            "name": "dashboard",
            "remote_url": "https://example.com/acme/dashboard",
        }
        store.register_repository(organization["id"], first_repo)
        store.register_repository(organization["id"], second_repo)
        first_capsule = _capsule(summary="API work")
        second_capsule = _capsule(summary="Dashboard work")
        second_capsule["repository_key"] = "repo:dashboard"
        store.publish_capsule(organization["id"], first_capsule)
        store.publish_capsule(organization["id"], second_capsule)

        api = store.context(organization["id"], "repo:test")
        dashboard = store.context(organization["id"], "repo:dashboard")
        assert [item["summary"] for item in api["capsules"]] == ["API work"]
        assert [item["summary"] for item in dashboard["capsules"]] == ["Dashboard work"]
        assert len(store.list_repositories(organization["id"])) == 2


def test_store_like_filters_match_wildcard_characters_literally(tmp_path):
    token = generate_team_token()
    with TeamStore(tmp_path / "team.db") as store:
        org = store.bootstrap("acme", "Acme", token)
        store.register_repository(org["id"], _repository())

        underscore = _capsule(summary="Underscore work")
        underscore["developer"] = {
            "external_id": "dev_one@example.com",
            "display_name": "Dev_One",
            "email": "dev_one@example.com",
        }
        underscore["capsule"]["external_id"] = "commit:" + "c" * 40
        underscore["symbols"][0]["symbol_key"] = "src/a.py::load_data"
        underscore["symbols"][0]["qualified_name"] = "src/a.py::load_data"
        lookalike = _capsule(summary="Lookalike work")
        lookalike["developer"] = {
            "external_id": "devXone@example.com",
            "display_name": "DevXOne",
            "email": "devXone@example.com",
        }
        lookalike["capsule"]["external_id"] = "commit:" + "d" * 40
        lookalike["session"]["external_id"] = "session-2"
        lookalike["symbols"][0]["symbol_key"] = "src/a.py::loadXdata"
        lookalike["symbols"][0]["qualified_name"] = "src/a.py::loadXdata"
        store.publish_capsule(org["id"], underscore)
        store.publish_capsule(org["id"], lookalike)

        assert store.context(org["id"], "repo:test", developer="dev_one")["count"] == 1
        assert store.context(org["id"], "repo:test", symbol="load_data")["count"] == 1
        assert store.context(org["id"], "repo:test", developer="dev%")["count"] == 0
        assert store.context(org["id"], "repo:test", symbol="%")["count"] == 0


def test_cache_like_filters_match_wildcard_characters_literally(tmp_path):
    with TeamCache(tmp_path / "team-cache.db") as cache:
        cache.upsert_capsule(
            {
                "id": "capsule-underscore",
                "updated_at": "2026-07-20T10:00:00+00:00",
                "developer": {"external_id": "dev_one@example.com"},
                "symbols": [{"symbol_key": "src/a.py::load_data", "file_path": "src/a.py"}],
            }
        )
        cache.upsert_capsule(
            {
                "id": "capsule-lookalike",
                "updated_at": "2026-07-20T11:00:00+00:00",
                "developer": {"external_id": "devXone@example.com"},
                "symbols": [{"symbol_key": "src/a.py::loadXdata", "file_path": "src/a.py"}],
            }
        )
        assert [c["id"] for c in cache.query(developer="dev_one")] == ["capsule-underscore"]
        assert [c["id"] for c in cache.query(symbol="load_data")] == ["capsule-underscore"]
        assert cache.query(developer="%") == []


def test_environment_only_config_does_not_persist_secret_on_cursor_update(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CRG_TEAM_SERVER", "https://context.example.com")
    monkeypatch.setenv("CRG_TEAM_TOKEN", "environment-secret-token")
    monkeypatch.setenv("CRG_TEAM_REPOSITORY", "repo:test")
    config = TeamConfig.load(repo)
    config.last_cursor = 42
    path = config.save_cursor(repo)
    assert not path.exists()


def test_durable_outbox_survives_failure_and_flushes_later(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = TeamConfig("https://team.invalid", "secret-token", "repo:test")
    payload = _capsule()

    with patch.object(TeamClient, "publish", side_effect=TeamAPIError("offline")):
        with pytest.raises(TeamAPIError, match="offline"):
            publish_and_cache(repo, config, payload)
    with TeamCache.for_repo(repo) as cache:
        pending = cache.pending()
        assert cache.outbox_count == 1
        assert pending[0]["attempts"] == 1

    published = {
        "id": "capsule-1",
        "title": "Implement parser",
        "updated_at": "2026-07-20T10:00:00+00:00",
        "event_seq": 7,
        "developer": payload["developer"],
        "commit": payload["commit"],
        "symbols": payload["symbols"],
    }
    with patch.object(TeamClient, "publish", return_value=published):
        result = flush_outbox(repo, config)
    assert result == {
        "attempted": 1,
        "sent": 1,
        "failed": 0,
        "dead_lettered": 0,
        "dead_letters": 0,
        "remaining": 0,
        "errors": [],
    }
    with TeamCache.for_repo(repo) as cache:
        assert cache.outbox_count == 0
        assert cache.query(commit="aaaa")[0]["id"] == "capsule-1"


def test_outbox_dead_letters_permanent_rejections_instead_of_retrying(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = TeamConfig("https://team.invalid", "secret-token", "repo:test")
    with TeamCache.for_repo(repo) as cache:
        cache.enqueue(_capsule())

    rejection = TeamAPIError("Capsule body must be an object", status=400)
    with patch.object(TeamClient, "publish", side_effect=rejection) as publish:
        first = flush_outbox(repo, config)
        second = flush_outbox(repo, config)
    assert first["failed"] == 1
    assert first["dead_lettered"] == 1
    assert first["remaining"] == 0
    assert second["attempted"] == 0
    assert publish.call_count == 1
    with TeamCache.for_repo(repo) as cache:
        assert cache.outbox_count == 0
        assert cache.dead_letter_count == 1
        assert cache.dead_letters()[0]["last_error"].startswith("Capsule body")
        # New content for the same capsule id restarts delivery.
        cache.enqueue(_capsule(summary="Fixed payload"))
        assert cache.outbox_count == 1
        assert cache.dead_letter_count == 0


def test_outbox_caps_transient_retries_then_dead_letters(tmp_path):
    from code_review_graph.team_sync import MAX_OUTBOX_ATTEMPTS

    repo = tmp_path / "repo"
    repo.mkdir()
    with TeamCache.for_repo(repo) as cache:
        external_id = cache.enqueue(_capsule())
        for attempt in range(1, MAX_OUTBOX_ATTEMPTS + 1):
            dead = cache.mark_failed(external_id, "HTTP 503: try again")
            assert dead is (attempt >= MAX_OUTBOX_ATTEMPTS)
        assert cache.outbox_count == 0
        assert cache.dead_letter_count == 1


def test_outbox_accepts_concurrent_hook_writers_without_losing_entries(tmp_path):
    cache_path = tmp_path / "team-cache.db"

    def enqueue(index: int) -> None:
        payload = _capsule(summary=f"Concurrent capsule {index}")
        payload["capsule"]["external_id"] = f"commit:{index:040x}"
        with TeamCache(cache_path) as cache:
            cache.enqueue(payload)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(enqueue, range(40)))
    with TeamCache(cache_path) as cache:
        assert cache.outbox_count == 40
        assert {item["external_id"] for item in cache.pending(limit=100)} == {
            f"commit:{index:040x}" for index in range(40)
        }

    def initialize_checkout(index: int) -> str:
        with TeamCache(cache_path) as cache:
            return cache.get_or_create_metadata("auto.checkout_id", f"checkout-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        checkout_ids = set(executor.map(initialize_checkout, range(40)))
    assert len(checkout_ids) == 1


def test_auto_enrollment_from_environment_never_persists_token(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    monkeypatch.setenv("CRG_TEAM_SERVER", "https://team.example.com")
    monkeypatch.setenv("CRG_TEAM_TOKEN", "environment-only-secret")
    monkeypatch.setenv("CRG_TEAM_REPOSITORY", "repo:test")
    registered = {**_repository(), "organization": "acme"}
    with patch.object(TeamClient, "register_repository", return_value=registered):
        config, warnings = ensure_team_config(repo)
    assert warnings == []
    assert config is not None and config.organization == "acme"
    saved = json.loads(TeamConfig.path_for(repo).read_text(encoding="utf-8"))
    assert saved["repository_key"] == "repo:test"
    assert "token" not in saved
    assert "environment-only-secret" not in TeamConfig.path_for(repo).read_text()


def test_auto_change_coalesces_offline_checkpoints_and_never_raises(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Initial")
    TeamConfig(
        "https://team.invalid",
        "secret-token",
        "repo:test",
        organization="acme",
        developer_id="dev@example.com",
    ).save(repo)
    monkeypatch.setenv("CRG_TEAM_CHECKPOINT_SECONDS", "3600")

    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    with patch.object(TeamClient, "publish", side_effect=TeamAPIError("offline")) as publish:
        first = run_auto_event("change", repo_root=repo, agent_name="codex")
        (repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        second = run_auto_event("change", repo_root=repo, agent_name="codex")
    assert first["status"] == "degraded"
    assert second["status"] == "ok"
    assert publish.call_count == 1
    with TeamCache.for_repo(repo) as cache:
        assert cache.outbox_count == 1
        pending = cache.pending()[0]["payload"]
    assert pending["capsule"]["external_id"].startswith("auto-worktree:")
    assert pending["capsule"]["metadata"]["automatic"] is True
    assert pending["capsule"]["summary"].startswith("Automatic checkpoint of 1")


def test_automatic_checkpoint_payload_is_stable_for_unchanged_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Initial")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    config = TeamConfig(
        "https://team.invalid",
        "secret-token",
        "repo:test",
        organization="acme",
        developer_id="dev@example.com",
    )
    config.save(repo)

    first = _automatic_worktree_payload(repo, config, "codex")
    second = _automatic_worktree_payload(repo, config, "codex")
    # Identical payload bytes mean an identical server content hash, so an
    # unchanged tree republished by a later hook produces no new team event.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_auto_post_commit_enqueues_then_publishes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Initial")
    TeamConfig(
        "https://team.example.com",
        "secret-token",
        "repo:test",
        organization="acme",
        developer_id="dev@example.com",
    ).save(repo)

    def publish(payload):
        return {
            "id": "capsule-commit",
            "title": payload["capsule"]["title"],
            "updated_at": "2026-07-20T10:00:00+00:00",
            "event_seq": 1,
            "developer": payload["developer"],
            "commit": payload["commit"],
            "symbols": payload["symbols"],
        }

    with patch.object(TeamClient, "publish", side_effect=publish):
        result = run_auto_event("post-commit", repo_root=repo, agent_name="codex")
    assert result["status"] == "ok"
    assert result["queued"] == 1
    assert result["publication"]["sent"] == 1
    assert result["outbox_remaining"] == 0


def test_auto_post_commit_captures_only_head_not_entire_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    for index in range(3):
        (repo / "app.py").write_text(f"value = {index}\n", encoding="utf-8")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", f"Commit {index}")
    head = _git(repo, "rev-parse", "HEAD")
    TeamConfig(
        "https://team.example.com",
        "secret-token",
        "repo:test",
        organization="acme",
        developer_id="dev@example.com",
    ).save(repo)
    published_shas = []

    def publish(payload):
        published_shas.append(payload["commit"]["sha"])
        return {
            "id": f"capsule-{payload['commit']['sha']}",
            "title": payload["capsule"]["title"],
            "updated_at": "2026-07-20T10:00:00+00:00",
            "event_seq": len(published_shas),
            "developer": payload["developer"],
            "commit": payload["commit"],
            "symbols": payload["symbols"],
        }

    with patch.object(TeamClient, "publish", side_effect=publish):
        result = run_auto_event("post-commit", repo_root=repo, agent_name="git")
    assert result["queued"] == 1
    assert result["publication"]["sent"] == 1
    assert published_shas == [head]


def test_auto_ci_range_publishes_each_selected_commit_oldest_first(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    shas = []
    for index in range(4):
        (repo / "app.py").write_text(f"value = {index}\n", encoding="utf-8")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", f"Commit {index}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    TeamConfig(
        "https://team.example.com",
        "secret-token",
        "repo:test",
        organization="acme",
        developer_id="dev@example.com",
    ).save(repo)
    published_shas = []

    def publish(payload):
        published_shas.append(payload["commit"]["sha"])
        return {
            "id": f"capsule-{payload['commit']['sha']}",
            "title": payload["capsule"]["title"],
            "updated_at": "2026-07-20T10:00:00+00:00",
            "event_seq": len(published_shas),
            "developer": payload["developer"],
            "commit": payload["commit"],
            "symbols": payload["symbols"],
        }

    with patch.object(TeamClient, "publish", side_effect=publish):
        result = run_auto_event(
            "ci", repo_root=repo, revision_range="HEAD~2..HEAD", agent_name="ci",
        )
    assert result["queued"] == 2
    assert result["publication"]["sent"] == 2
    assert published_shas == shas[-2:]


def test_http_client_auth_publish_context_and_offline_cache(tmp_path):
    token = generate_team_token()
    store = TeamStore(tmp_path / "team.db")
    store.bootstrap("acme", "Acme", token)
    server = build_team_server(tmp_path / "team.db", port=0)
    # build_team_server owns a separate store; close the bootstrap connection.
    store.close()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client = TeamClient(url, token)
    try:
        assert client.health()["service"] == "crg-team"
        registered = client.register_repository(_repository())
        assert registered["external_id"] == "repo:test"
        published = client.publish(_capsule())
        assert published["symbols"][0]["symbol_key"].endswith("Parser.parse")
        context = client.context("repo:test", developer="dev@example.com")
        assert context["count"] == 1
        assert client.activity("repo:test")["developers"][0]["capsule_count"] == 1
        with pytest.raises(TeamAPIError) as exc_info:
            TeamClient(url, "wrong-token-that-is-long-enough").repositories()
        assert exc_info.value.status == 401
        oversized = _capsule(summary="x" * (2 * 1024 * 1024))
        with pytest.raises(TeamAPIError, match="exceeds") as oversized_error:
            client.publish(oversized)
        assert oversized_error.value.status == 413

        # An untrusted client cannot bypass the matching server-side limit.
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        connection.putrequest("POST", "/v1/capsules")
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert "exceeds" in json.loads(response.read())["error"]
        connection.close()

        events = client.events("repo:test")["events"]
        with TeamCache(tmp_path / "cache.db") as cache:
            cursor = cache.apply_events(events)
            assert cursor == published["event_seq"]
            assert cache.query(symbol="Parser.parse")[0]["id"] == published["id"]
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_two_checkouts_publish_sync_and_read_developer_handoff(tmp_path):
    token = generate_team_token()
    server = build_team_server(
        tmp_path / "team.db",
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    TeamClient(url, token).register_repository(_repository())
    first_repo = tmp_path / "first-checkout"
    second_repo = tmp_path / "second-checkout"
    for repo in (first_repo, second_repo):
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Developer One")
        _git(repo, "config", "user.email", "dev@example.com")
        (repo / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        _git(repo, "add", "service.py")
        _git(repo, "commit", "-m", "Implement service")
    TeamConfig(
        server_url=url,
        token=token,
        repository_key="repo:test",
        developer_id="dev@example.com",
        developer_name="Developer One",
        developer_email="dev@example.com",
    ).save(first_repo)
    TeamConfig(
        server_url=url,
        token=token,
        repository_key="repo:test",
        developer_id="reviewer@example.com",
        developer_name="Reviewer",
        developer_email="reviewer@example.com",
    ).save(second_repo)
    try:
        published = publish_work_capsule_func(
            repo_root=str(first_repo),
            summary="Service entry point returns its readiness value",
            intent="Expose a minimal service entry point",
            decisions=["Keep startup synchronous until metrics are available"],
            tests=["unit=passed"],
            agent_name="codex",
        )
        assert published["status"] == "ok"

        (first_repo / "service.py").write_text(
            "def run():\n    return 2\n", encoding="utf-8"
        )
        _git(first_repo, "add", "service.py")
        _git(first_repo, "commit", "-m", "Advance service readiness")
        imported = publish_commit_range_func(
            "HEAD~1..HEAD", repo_root=str(first_repo), agent_name="ci"
        )
        assert imported["status"] == "ok"
        assert imported["processed"] == 1
        assert imported["published"] == 1

        synchronized = sync_team_context_func(repo_root=str(second_repo))
        assert synchronized["events_received"] == 2
        handoff = get_developer_context_func(
            "dev@example.com", repo_root=str(second_repo), offline=True
        )
        assert handoff["source"] == "offline-cache"
        rich = next(capsule for capsule in handoff["capsules"] if capsule["decisions"])
        assert rich["intent"].startswith("Expose a minimal")
        assert rich["decisions"][0]["summary"].startswith("Keep startup")
        assert rich["tests"][0]["status"] == "passed"
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_zero_touch_environment_enrollment_publishes_to_real_server(
    tmp_path, monkeypatch
):
    token = generate_team_token()
    server = build_team_server(
        tmp_path / "team.db",
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Automatic Developer")
    _git(repo, "config", "user.email", "automatic@example.com")
    (repo / "service.py").write_text("def ready():\n    return True\n", encoding="utf-8")
    _git(repo, "add", "service.py")
    _git(repo, "commit", "-m", "Add readiness check")
    monkeypatch.setenv("CRG_TEAM_SERVER", url)
    monkeypatch.setenv("CRG_TEAM_TOKEN", token)
    monkeypatch.setenv("CRG_TEAM_REPOSITORY", "repo:auto")
    try:
        automatic = run_auto_event("post-commit", repo_root=repo, agent_name="codex")
        assert automatic["status"] == "ok"
        assert automatic["publication"]["sent"] == 1
        context = TeamClient(url, token).context("repo:auto", developer="automatic@example.com")
        assert context["count"] == 1
        assert context["capsules"][0]["agent_name"] == "codex"
        saved = TeamConfig.path_for(repo).read_text(encoding="utf-8")
        assert token not in saved
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_zero_touch_closes_live_worktree_when_commit_is_created(tmp_path, monkeypatch):
    token = generate_team_token()
    server = build_team_server(
        tmp_path / "team.db",
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "service.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "service.py")
    _git(repo, "commit", "-m", "Initial")
    monkeypatch.setenv("CRG_TEAM_SERVER", url)
    monkeypatch.setenv("CRG_TEAM_TOKEN", token)
    monkeypatch.setenv("CRG_TEAM_REPOSITORY", "repo:wip-lifecycle")
    try:
        (repo / "service.py").write_text("value = 2\n", encoding="utf-8")
        checkpoint = run_auto_event("change", repo_root=repo, agent_name="codex")
        assert checkpoint["status"] == "ok"
        _git(repo, "add", "service.py")
        _git(repo, "commit", "-m", "Complete service update")
        committed = run_auto_event("post-commit", repo_root=repo, agent_name="codex")
        assert committed["status"] == "ok"
        assert committed["queued"] == 2

        context = TeamClient(url, token).context("repo:wip-lifecycle")
        live_record = next(
            item for item in context["capsules"] if item["external_id"].startswith("auto-worktree:")
        )
        commit_record = next(item for item in context["capsules"] if item.get("commit"))
        assert live_record["status"] == "completed"
        assert live_record["metadata"]["live"] is False
        assert live_record["metadata"]["superseded_by_commit"] == commit_record["commit"]["sha"]
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_two_real_clones_exchange_context_using_only_installed_git_hooks(
    tmp_path, monkeypatch
):
    token = generate_team_token()
    server = build_team_server(
        tmp_path / "team.db",
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    first = tmp_path / "alice"
    second = tmp_path / "bob"
    _git(tmp_path, "clone", str(origin), str(first))
    _git(first, "config", "user.name", "Alice")
    _git(first, "config", "user.email", "alice@example.com")
    (first / "service.py").write_text("def ready():\n    return False\n", encoding="utf-8")
    _git(first, "add", "service.py")
    _git(first, "commit", "-m", "Initial service")
    _git(first, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(origin), str(second))
    _git(second, "config", "user.name", "Bob")
    _git(second, "config", "user.email", "bob@example.com")
    for repo, developer_id, developer_name, developer_email in (
        (first, "alice@example.com", "Alice", "alice@example.com"),
        (second, "bob@example.com", "Bob", "bob@example.com"),
    ):
        TeamConfig(
            url,
            token,
            "repo:hook-e2e",
            developer_id=developer_id,
            developer_name=developer_name,
            developer_email=developer_email,
        ).save(repo)
        install_git_hook(repo)
    monkeypatch.setenv(
        "PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    )
    for name in ("CRG_TEAM_SERVER", "CRG_TEAM_TOKEN", "CRG_TEAM_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
    try:
        # `install` performs this initial enrollment/sync; all development
        # actions after it are ordinary Git commands with no explicit publish.
        assert run_auto_event("session-start", repo_root=first)["status"] == "ok"
        assert run_auto_event("session-start", repo_root=second)["status"] == "ok"
        (first / "service.py").write_text("def ready():\n    return True\n", encoding="utf-8")
        _git(first, "add", "service.py")
        _git(first, "commit", "-m", "Make service ready")
        commit_sha = _git(first, "rev-parse", "HEAD")
        _git(first, "push")
        _git(second, "pull", "--ff-only")

        with TeamCache.for_repo(second) as cache:
            received = cache.query(commit=commit_sha)
        assert len(received) == 1
        assert received[0]["developer"]["external_id"] == "alice@example.com"
        assert received[0]["commit"]["message"] == "Make service ready"
        assert received[0]["symbols"][0]["file_path"] == "service.py"
    finally:
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_real_server_outage_keeps_checkpoint_until_forced_recovery(
    tmp_path, monkeypatch
):
    token = generate_team_token()
    database = tmp_path / "team.db"
    server = build_team_server(
        database,
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Initial")
    TeamClient(url, token).register_repository(
        {
            "external_id": "repo:recovery",
            "name": "recovery",
            "remote_url": "",
            "default_branch": "main",
        }
    )
    TeamConfig(
        url,
        token,
        "repo:recovery",
        organization="acme",
        developer_id="dev@example.com",
        developer_name="Developer One",
        developer_email="dev@example.com",
    ).save(repo)
    server.shutdown()
    server.server_close()
    server.team_store.close()
    thread.join(timeout=2)
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setenv("CRG_TEAM_AUTO_TIMEOUT", "0.25")
    offline = run_auto_event("change", repo_root=repo, agent_name="codex")
    assert offline["status"] == "degraded"
    assert offline["outbox_remaining"] == 1

    recovered_server = build_team_server(database, port=port)
    recovered_thread = threading.Thread(target=recovered_server.serve_forever, daemon=True)
    recovered_thread.start()
    try:
        recovered = run_auto_event("flush", repo_root=repo, agent_name="codex")
        assert recovered["status"] == "ok"
        assert recovered["publication"]["sent"] == 1
        assert recovered["outbox_remaining"] == 0
        context = TeamClient(url, token).context("repo:recovery")
        assert context["count"] == 1
        assert context["capsules"][0]["metadata"]["live"] is True
    finally:
        recovered_server.shutdown()
        recovered_server.server_close()
        recovered_server.team_store.close()
        recovered_thread.join(timeout=2)


def _git(repo, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_slow_client_does_not_block_other_requests(tmp_path):
    token = generate_team_token()
    server = build_team_server(
        tmp_path / "team.db",
        port=0,
        bootstrap_token=token,
        organization_slug="acme",
        organization_name="Acme",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        # Headers promise a body that never arrives, so this handler thread
        # stays blocked in its read. Other requests must still be served.
        stalled.sendall(
            b"POST /v1/capsules HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 1000\r\n"
            b"Authorization: Bearer " + token.encode("ascii") + b"\r\n"
            b"\r\n"
        )
        health = TeamClient(f"http://127.0.0.1:{port}", token, timeout=5).health()
        assert health["status"] == "ok"
    finally:
        stalled.close()
        server.shutdown()
        server.server_close()
        server.team_store.close()
        thread.join(timeout=2)


def test_git_capture_handles_commits_and_untracked_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Add greeting\n\nInitial implementation")
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "graph-data"))
    developer = {
        "external_id": "publisher@example.com",
        "display_name": "Publishing Agent User",
        "email": "publisher@example.com",
    }

    committed = capture_commit(repo, "repo:test", developer)
    assert committed["commit"]["sha"] == _git(repo, "rev-parse", "HEAD")
    assert committed["developer"]["external_id"] == "dev@example.com"
    assert committed["files"][0]["path"] == "app.py"
    assert committed["symbols"][0]["symbol_key"] == "app.py"

    (repo / "app.py").write_text("def greet():\n    return 'hello team'\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Update greeting")
    updated = capture_commit(repo, "repo:test", developer)
    assert updated["commit"]["parent_sha"] == committed["commit"]["sha"]
    assert updated["files"][0]["path"] == "app.py"
    assert updated["files"][0]["additions"] == 1
    assert updated["files"][0]["deletions"] == 1

    (repo / "notes.txt").write_text("handoff details\n", encoding="utf-8")
    working = capture_worktree(repo, "repo:test", developer, summary="Added handoff notes")
    repeated = capture_worktree(repo, "repo:test", developer, summary="Added handoff notes")
    assert working["capsule"]["status"] == "in_progress"
    assert working["developer"]["external_id"] == "publisher@example.com"
    assert working["files"] == [
        {
            "path": "notes.txt",
            "old_path": "",
            "change_type": "added",
            "additions": 1,
            "deletions": 0,
        }
    ]
    assert working["symbols"][0]["symbol_key"] == "notes.txt"
    assert repeated["capsule"]["external_id"] == working["capsule"]["external_id"]
    assert repeated["session"]["external_id"] == working["session"]["external_id"]


def test_worktree_capture_supports_unborn_repository(tmp_path):
    repo = tmp_path / "new-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "first.py").write_text("answer = 42\n", encoding="utf-8")
    _git(repo, "add", "first.py")
    (repo / "notes.md").write_text("Uncommitted plan\n", encoding="utf-8")
    developer = {
        "external_id": "dev@example.com",
        "display_name": "Developer One",
        "email": "dev@example.com",
    }

    payload = capture_worktree(
        repo,
        "repo:new",
        developer,
        summary="Work before the first commit",
        agent_name="codex",
    )
    assert payload["capsule"]["base_sha"] == ""
    assert payload["capsule"]["head_sha"] == ""
    assert {item["path"] for item in payload["files"]} == {"first.py", "notes.md"}
    assert all(item["change_type"] == "added" for item in payload["files"])


def test_worktree_capture_handles_rename_delete_unicode_and_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Developer One")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "old.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "remove.txt").write_text("remove me\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial files")
    _git(repo, "mv", "old.py", "renamed file.py")
    _git(repo, "rm", "remove.txt")
    (repo / "नोट्स.md").write_text("handoff\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("never read through the link\n", encoding="utf-8")
    (repo / "external-link").symlink_to(outside)
    developer = {
        "external_id": "dev@example.com",
        "display_name": "Developer One",
        "email": "dev@example.com",
    }

    payload = capture_worktree(
        repo,
        "repo:test",
        developer,
        summary="Mixed filesystem changes",
        agent_name="codex",
    )
    by_path = {item["path"]: item for item in payload["files"]}
    assert by_path["renamed file.py"]["change_type"] == "renamed"
    assert by_path["renamed file.py"]["old_path"] == "old.py"
    assert by_path["remove.txt"]["change_type"] == "deleted"
    assert by_path["नोट्स.md"]["change_type"] == "added"
    assert by_path["external-link"]["additions"] == 0


def test_same_remote_over_ssh_and_https_has_one_repository_identity(tmp_path):
    https_repo = tmp_path / "https"
    ssh_repo = tmp_path / "ssh"
    for repo, remote in (
        (https_repo, "https://github.com/acme/service.git"),
        (ssh_repo, "git@github.com:acme/service.git"),
    ):
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "remote", "add", "origin", remote)
    assert repository_identity(https_repo)["external_id"] == repository_identity(ssh_repo)[
        "external_id"
    ]


def test_same_developer_branch_in_two_checkouts_gets_distinct_live_records(
    tmp_path, monkeypatch
):
    repos = [tmp_path / "first", tmp_path / "second"]
    for repo in repos:
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Developer One")
        _git(repo, "config", "user.email", "dev@example.com")
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "Initial")
        TeamConfig(
            "https://team.invalid",
            "secret-token",
            "repo:shared",
            organization="acme",
            developer_id="dev@example.com",
        ).save(repo)
        (repo / "app.py").write_text(f"checkout = {repo.name!r}\n", encoding="utf-8")
    monkeypatch.setenv("CRG_TEAM_CHECKPOINT_SECONDS", "0")
    with patch.object(TeamClient, "publish", side_effect=TeamAPIError("offline")):
        results = [run_auto_event("change", repo_root=repo, agent_name="codex") for repo in repos]
    assert all(result["status"] == "degraded" for result in results)
    external_ids = set()
    for repo in repos:
        with TeamCache.for_repo(repo) as cache:
            external_ids.add(cache.pending()[0]["external_id"])
    assert len(external_ids) == 2


def test_remote_canonicalization_removes_secrets_and_transport_noise():
    canonical = canonical_remote_url(
        "https://alice:secret-token@GitHub.com/acme/service.git?token=leak#fragment"
    )
    assert canonical == "https://github.com/acme/service"
    assert "secret-token" not in canonical
    assert "token=leak" not in canonical
    assert canonical_remote_url("git@GitHub.com:acme/service.git") == (
        "ssh://github.com/acme/service"
    )


def test_cli_creates_token_and_forwards_worktree_handoff(tmp_path, capsys):
    db_path = tmp_path / "server" / "team.db"
    with patch.object(sys, "argv", [
        "code-review-graph", "team", "token", "--db", str(db_path),
        "--organization", "acme", "--organization-name", "Acme",
    ]):
        cli.main()
    token_result = json.loads(capsys.readouterr().out)
    assert token_result["status"] == "ok"
    with TeamStore(db_path) as store:
        assert store.authenticate(token_result["token"])["slug"] == "acme"

    with patch.object(sys, "argv", [
        "code-review-graph", "team", "revoke-token", "--db", str(db_path),
        "--organization", "acme", "--name", "cli",
    ]):
        cli.main()
    revoke_result = json.loads(capsys.readouterr().out)
    assert revoke_result["status"] == "ok"
    with TeamStore(db_path) as store:
        assert store.authenticate(token_result["token"]) is None

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    expected = {"status": "ok", "summary": "published", "capsule": {"id": "one"}}
    argv = [
        "code-review-graph", "team", "publish", "--repo", str(repo),
        "--working-tree", "--summary", "Ready for another agent", "--agent", "codex",
        "--decision", "Preserve the wire format", "--test", "unit=passed",
    ]
    with patch.object(sys, "argv", argv):
        with patch(
            "code_review_graph.tools.team_tools.publish_work_capsule_func",
            return_value=expected,
        ) as publish:
            cli.main()
    assert json.loads(capsys.readouterr().out) == expected
    publish.assert_called_once_with(
        repo_root=str(repo),
        commit="HEAD",
        working_tree=True,
        title="Working-tree handoff",
        summary="Ready for another agent",
        intent="",
        approach="",
        outcome="",
        status=None,
        agent_name="codex",
        session_id="",
        decisions=["Preserve the wire format"],
        open_questions=[],
        tests=["unit=passed"],
    )


def test_cli_forwards_zero_touch_lifecycle_event(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    expected = {"status": "ok", "event": "post-merge", "outbox_remaining": 0}
    argv = [
        "code-review-graph", "team", "auto", "--event", "post-merge",
        "--repo", str(repo), "--range", "main..HEAD", "--agent", "codex",
    ]
    with patch.object(sys, "argv", argv):
        with patch(
            "code_review_graph.team_automation.run_auto_event", return_value=expected,
        ) as auto:
            cli.main()
    assert json.loads(capsys.readouterr().out) == expected
    auto.assert_called_once_with(
        "post-merge",
        repo_root=repo.resolve(),
        revision_range="main..HEAD",
        agent_name="codex",
    )
