"""Tests for the `code-review-graph doctor` health checklist."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph import doctor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=Test", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip env that would otherwise leak the dev machine into checks."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    return monkeypatch


class TestCheckResult:
    def test_check_result_glyphs(self):
        ok = doctor.CheckResult(name="x", ok=True, message="m")
        bad = doctor.CheckResult(name="y", ok=False, message="m")
        assert ok.glyph == "✓"
        assert bad.glyph == "✗"


class TestGraphCheck:
    def test_unbuilt_repo_flags_missing_graph(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = doctor.check_graph_db(repo)
        assert result.ok is False
        assert result.critical is True
        # The hint must point the user at the build command.
        assert "build" in (result.hint or "").lower()

    def test_built_repo_passes_graph_check(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        result = doctor.check_graph_db(repo)
        assert result.ok is True
        assert "node" in result.message.lower()


class TestFreshnessCheck:
    def test_stale_graph_warns_but_is_not_critical(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        # Advance HEAD so the stored sha is now stale.
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hello ' + name\n", encoding="utf-8"
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "second")

        result = doctor.check_freshness(repo)
        assert result.ok is False
        # Staleness is a warning, never a hard failure.
        assert result.critical is False
        assert "update" in (result.hint or "").lower()

    def test_fresh_graph_passes_freshness(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        result = doctor.check_freshness(repo)
        assert result.ok is True


class TestServerSmoke:
    def test_server_import_reports_tool_count(self, tmp_path):
        result = doctor.check_server_boot(tmp_path)
        assert result.ok is True
        # The message should surface a positive tool count.
        assert "tool" in result.message.lower()


class TestMcpConfigCheck:
    def test_missing_config_is_non_critical_with_install_hint(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = doctor.check_mcp_config(repo)
        # No config files at all -> not OK but never blocks (install is the fix).
        assert result.critical is False
        assert "install" in (result.hint or "").lower()

    def test_present_config_passes(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        result = doctor.check_mcp_config(repo)
        assert result.ok is True
        assert ".mcp.json" in result.message


class TestEmbeddingsCheck:
    def test_no_embeddings_notes_keyword_fallback(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        result = doctor.check_embeddings(repo)
        # Embeddings are optional; absence is never critical.
        assert result.critical is False
        assert "keyword" in result.message.lower()


class TestRunDoctor:
    def test_unbuilt_repo_returns_nonzero_exit(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        results, exit_code = doctor.run_doctor(repo)
        assert exit_code != 0
        # At least one critical check must have failed.
        assert any(r.critical and not r.ok for r in results)

    def test_built_repo_returns_zero_exit(self, tmp_path, isolated_env):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text(
            "def greet(name):\n    return 'hi ' + name\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "initial")
        (repo / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        results, exit_code = doctor.run_doctor(repo)
        assert exit_code == 0
        # No critical check failed.
        assert not any(r.critical and not r.ok for r in results)


class TestDoctorCliWiring:
    def test_cli_doctor_subcommand_invokes_handler(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        argv = ["code-review-graph", "doctor", "--repo", str(repo)]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.doctor.run_doctor") as mock_run:
                mock_run.return_value = (
                    [doctor.CheckResult(name="graph", ok=True, message="ok")],
                    0,
                )
                from code_review_graph import cli
                with pytest.raises(SystemExit) as exc:
                    cli.main()
                assert exc.value.code == 0
                mock_run.assert_called_once()

    def test_cli_doctor_exits_nonzero_on_critical_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        argv = ["code-review-graph", "doctor", "--repo", str(repo)]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.doctor.run_doctor") as mock_run:
                mock_run.return_value = (
                    [doctor.CheckResult(
                        name="graph", ok=False, critical=True,
                        message="missing", hint="run build",
                    )],
                    1,
                )
                from code_review_graph import cli
                with pytest.raises(SystemExit) as exc:
                    cli.main()
                assert exc.value.code == 1


class TestPrintReport:
    def test_print_report_emits_glyphs_and_summary(self, capsys):
        results = [
            doctor.CheckResult(name="graph", ok=True, message="42 nodes"),
            doctor.CheckResult(
                name="freshness", ok=False, critical=False,
                message="stale", hint="run update",
            ),
        ]
        doctor.print_report(results, repo_root=Path("/tmp/x"))
        out = capsys.readouterr().out
        assert "✓" in out
        assert "✗" in out
        assert "run update" in out
        # One-line summary at the end.
        assert "passed" in out.lower() or "healthy" in out.lower() or "issue" in out.lower()


class TestGitHeadSha:
    def test_current_head_sha_uses_safe_subprocess(self, tmp_path):
        """The freshness git call must not use shell and must pass stdin=DEVNULL."""
        repo = tmp_path / "repo"
        repo.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
            sha = doctor._current_head_sha(repo)
        assert sha == "abc123"
        _, kwargs = mock_run.call_args
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert "shell" not in kwargs or kwargs["shell"] is False
