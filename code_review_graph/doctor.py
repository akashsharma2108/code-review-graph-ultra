"""Health checklist for a code-review-graph install (`doctor` command).

Runs a series of fast, read-only checks and prints a clean ``✓``/``✗`` line
per check with an actionable next-step hint.  The orchestrator returns a
non-zero exit code when a *critical* check fails (no graph, no git, server
won't import) so the command can gate CI and install scripts; warnings such
as a stale graph or missing embeddings never fail the exit code.

This generalizes the single-purpose ``scripts/diagnose_pypi_connectivity.py``
pattern (clear PASS/FAIL lines, exit-code-driven) into a reusable checklist.
The connectivity diagnostic is intentionally left untouched.

Checks performed:

1. graph.db exists and has nodes (critical; hint: run ``build``)
2. freshness — stored ``git_head_sha`` vs current HEAD (warning; hint ``update``)
3. MCP config present for the repo (warning; hint: run ``install``)
4. MCP server import/boot smoke — tool count > 0 (critical)
5. platform-native hooks installed (warning; hint: run ``install``)
6. embeddings present or note keyword-search fallback (informational)

If a graph exists, the closing summary surfaces the latest ``detect-changes``
Token Savings number as the "see your savings" proof.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Short, hard cap so a wedged git never hangs `doctor`.  Honors the same
# env override the rest of the codebase uses for git timeouts.
_GIT_TIMEOUT = int(os.environ.get("CRG_GIT_TIMEOUT", "10"))


@dataclass
class CheckResult:
    """The outcome of a single health check.

    Attributes:
        name: Short, stable identifier for the check (used in the printed line).
        ok: True when the check passed.
        message: One-line human-readable status.
        critical: When True, a failure (``ok is False``) makes ``doctor`` exit
            non-zero.  Warnings (``critical is False``) are surfaced but never
            fail the exit code.
        hint: Optional actionable next-step shown when the check is not ok.
    """

    name: str
    ok: bool
    message: str = ""
    critical: bool = False
    hint: str | None = None
    # Optional structured extras a caller may want (e.g. node counts).
    extra: dict = field(default_factory=dict)

    @property
    def glyph(self) -> str:
        return "✓" if self.ok else "✗"


# ---------------------------------------------------------------------------
# Safe git helper (list args, no shell, stdin closed, bounded timeout)
# ---------------------------------------------------------------------------


def _current_head_sha(repo_root: Path) -> str:
    """Return the current ``HEAD`` sha via a safe git subprocess, or ``""``.

    Mirrors :func:`code_review_graph.incremental._git_branch_info` semantics —
    list-form args, ``shell=False`` (default), ``stdin=subprocess.DEVNULL`` so
    git can never block on a prompt, and a bounded timeout.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("git rev-parse HEAD failed in %s: %s", repo_root, exc)
        return ""
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_graph_db(repo_root: Path) -> CheckResult:
    """Check that a graph database exists and contains nodes (critical)."""
    from .incremental import get_db_path

    db_path = get_db_path(repo_root)
    if not db_path.exists():
        return CheckResult(
            name="graph",
            ok=False,
            critical=True,
            message=f"No graph database at {db_path}",
            hint="Run `code-review-graph build` to create it.",
        )

    from .graph import GraphStore

    store = GraphStore(db_path)
    try:
        stats = store.get_stats()
    finally:
        store.close()

    if stats.total_nodes <= 0:
        return CheckResult(
            name="graph",
            ok=False,
            critical=True,
            message="Graph database exists but contains 0 nodes",
            hint="Run `code-review-graph build` to (re)parse your codebase.",
        )

    langs = ", ".join(stats.languages[:5]) if stats.languages else "—"
    return CheckResult(
        name="graph",
        ok=True,
        message=(
            f"{stats.total_nodes:,} nodes, {stats.total_edges:,} edges, "
            f"{stats.files_count:,} files ({langs})"
        ),
        extra={"total_nodes": stats.total_nodes},
    )


def check_freshness(repo_root: Path) -> CheckResult:
    """Compare stored ``git_head_sha`` to current HEAD (warning only)."""
    from .incremental import detect_vcs, get_db_path

    db_path = get_db_path(repo_root)
    if not db_path.exists():
        # The graph check already flags this as critical; here we just skip.
        return CheckResult(
            name="freshness",
            ok=False,
            critical=False,
            message="No graph to compare — build first",
            hint="Run `code-review-graph build`.",
        )

    if detect_vcs(repo_root) != "git":
        return CheckResult(
            name="freshness",
            ok=True,
            message="Not a git repo — freshness check skipped",
        )

    from .graph import GraphStore

    store = GraphStore(db_path)
    try:
        stored_sha = store.get_metadata("git_head_sha") or ""
    finally:
        store.close()

    current_sha = _current_head_sha(repo_root)

    if not stored_sha:
        return CheckResult(
            name="freshness",
            ok=False,
            critical=False,
            message="Graph has no recorded git HEAD",
            hint="Run `code-review-graph build` to record the current commit.",
        )
    if not current_sha:
        # git is unavailable or detached in a way we can't read — don't punish.
        return CheckResult(
            name="freshness",
            ok=True,
            message="Could not read current HEAD — assuming fresh",
        )

    if stored_sha == current_sha:
        return CheckResult(
            name="freshness",
            ok=True,
            message=f"Graph matches current HEAD ({current_sha[:12]})",
        )
    return CheckResult(
        name="freshness",
        ok=False,
        critical=False,
        message=(
            f"Graph built at {stored_sha[:12]}, HEAD is now {current_sha[:12]}"
        ),
        hint="Run `code-review-graph update` to refresh the graph.",
    )


def check_mcp_config(repo_root: Path) -> CheckResult:
    """Check that at least one MCP config file is present (warning only).

    Reuses :data:`code_review_graph.skills.PLATFORMS` so the set of config
    paths stays in lock-step with what ``install`` writes.  Only repo-local
    config paths are considered authoritative here (``.mcp.json``,
    ``.cursor/mcp.json``, ``.kiro/...``, etc.) — user-level config under
    ``$HOME`` is not specific to this checkout and would produce false
    positives.
    """
    from .skills import PLATFORMS

    repo_root = repo_root.resolve()
    found: list[str] = []
    for plat in PLATFORMS.values():
        try:
            config_path = Path(plat["config_path"](repo_root)).resolve()
        except (OSError, TypeError):
            continue
        # Only count config files that live inside the repo tree.
        try:
            config_path.relative_to(repo_root)
        except ValueError:
            continue
        if config_path.exists():
            found.append(config_path.name)

    if not found:
        return CheckResult(
            name="mcp-config",
            ok=False,
            critical=False,
            message="No repo-local MCP config found",
            hint="Run `code-review-graph install` to configure your AI tools.",
        )

    # De-dup while preserving order (several platforms can share .mcp.json).
    seen: dict[str, None] = {}
    for name in found:
        seen.setdefault(name, None)
    return CheckResult(
        name="mcp-config",
        ok=True,
        message=f"MCP config present: {', '.join(seen)}",
    )


def check_serve_command() -> CheckResult:
    """Check that the detected serve command resolves to a runnable launcher."""
    from .skills import _detect_serve_command

    command, args = _detect_serve_command()
    return CheckResult(
        name="serve-cmd",
        ok=True,
        message=f"Serve command: {command} {' '.join(args)}",
    )


def check_server_boot(repo_root: Path) -> CheckResult:
    """Import the MCP server module and confirm tool count > 0 (critical).

    Kept fast: importing ``code_review_graph.main`` registers the FastMCP
    tools as a side effect, so we can count them without booting a transport.
    """
    try:
        from . import main as server_main
    except Exception as exc:  # noqa: BLE001 — import can fail many ways
        return CheckResult(
            name="server",
            ok=False,
            critical=True,
            message=f"MCP server failed to import: {exc}",
            hint="Reinstall: `pip install --force-reinstall code-review-graph`.",
        )

    count = _count_registered_tools(server_main)
    if count <= 0:
        return CheckResult(
            name="server",
            ok=False,
            critical=True,
            message="MCP server imported but registered 0 tools",
            hint="Reinstall: `pip install --force-reinstall code-review-graph`.",
        )
    return CheckResult(
        name="server",
        ok=True,
        message=f"MCP server imports cleanly with {count} tools",
        extra={"tool_count": count},
    )


def _count_registered_tools(server_main) -> int:  # type: ignore[no-untyped-def]
    """Count registered FastMCP tools without booting the event loop.

    FastMCP >=3 exposes an async ``list_tools``.  We run it on a short-lived
    loop; on any failure we fall back to 0 so the caller flags the problem.
    """
    import asyncio

    mcp = getattr(server_main, "mcp", None)
    if mcp is None:
        return 0
    list_tools = getattr(mcp, "list_tools", None)
    if list_tools is None:
        return 0

    def _runner() -> int:
        return len(asyncio.run(list_tools()))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running — safe to use asyncio.run directly.
        try:
            return _runner()
        except Exception as exc:  # noqa: BLE001
            logger.debug("list_tools failed: %s", exc)
            return 0

    # Already inside a loop (e.g. called from async test): use a worker thread.
    import concurrent.futures

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()
    except Exception as exc:  # noqa: BLE001
        logger.debug("list_tools (threaded) failed: %s", exc)
        return 0


def check_hooks(repo_root: Path) -> CheckResult:
    """Check whether platform-native or git hooks are installed (warning)."""
    found: list[str] = []

    # Git pre-commit hook installed by install_git_hook (marker string).
    pre_commit = repo_root / ".git" / "hooks" / "pre-commit"
    try:
        if pre_commit.exists() and "code-review-graph detect-changes" in (
            pre_commit.read_text(encoding="utf-8", errors="replace")
        ):
            found.append("git pre-commit")
    except OSError:
        pass

    # Claude / Qoder settings.json hooks.
    for plat in ("claude", "qoder"):
        settings = repo_root / f".{plat}" / "settings.json"
        try:
            if settings.exists() and "code-review-graph" in (
                settings.read_text(encoding="utf-8", errors="replace")
            ):
                found.append(f"{plat} settings")
        except OSError:
            pass

    if not found:
        return CheckResult(
            name="hooks",
            ok=False,
            critical=False,
            message="No code-review-graph hooks detected",
            hint="Run `code-review-graph install` to auto-update on changes.",
        )
    return CheckResult(
        name="hooks",
        ok=True,
        message=f"Hooks installed: {', '.join(found)}",
    )


def check_embeddings(repo_root: Path) -> CheckResult:
    """Check whether vector embeddings are present (informational only).

    Reads the ``embeddings`` table directly with a read-only sqlite query so
    we never load a model provider (which would be slow and could require a
    network call).  Absence is never an error — search degrades gracefully to
    keyword (FTS5) matching.
    """
    from .incremental import get_db_path

    db_path = get_db_path(repo_root)
    count = 0
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM embeddings"
                ).fetchone()
                count = int(row[0]) if row else 0
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # No embeddings table yet — that's the common, fine case.
            logger.debug("embeddings count query failed: %s", exc)
            count = 0

    if count > 0:
        return CheckResult(
            name="embeddings",
            ok=True,
            message=f"{count:,} embeddings present — semantic search active",
        )
    return CheckResult(
        name="embeddings",
        ok=True,  # not having embeddings is a fine, fully-supported state
        critical=False,
        message=(
            "No embeddings — semantic search falls back to keyword (FTS5) "
            "matching"
        ),
        hint="Optional: `code-review-graph embed --provider local`.",
    )


# ---------------------------------------------------------------------------
# Token-savings proof (closing line)
# ---------------------------------------------------------------------------


def latest_token_savings(repo_root: Path) -> str | None:
    """Return a one-line "see your savings" proof, or ``None``.

    Runs the same read-only ``detect-changes`` analysis the CLI uses and
    reports the estimated saved tokens/percent for the current change set.
    Best-effort: any failure (no graph, no changes, git error) returns
    ``None`` so the closing line is simply omitted.
    """
    try:
        from .changes import analyze_changes
        from .context_savings import (
            attach_context_savings,
            estimate_file_tokens,
            format_context_savings,
        )
        from .graph import GraphStore
        from .incremental import (
            get_changed_files,
            get_db_path,
            get_staged_and_unstaged,
        )

        db_path = get_db_path(repo_root)
        if not db_path.exists():
            return None

        changed = get_changed_files(repo_root, "HEAD~1")
        if not changed:
            changed = get_staged_and_unstaged(repo_root)
        if not changed:
            return None

        store = GraphStore(db_path)
        try:
            result = analyze_changes(
                store, changed, repo_root=str(repo_root), base="HEAD~1"
            )
        finally:
            store.close()

        original_tokens = estimate_file_tokens(repo_root, changed)
        attach_context_savings(result, original_tokens=original_tokens)
        return format_context_savings(result.get("context_savings"))
    except Exception as exc:  # noqa: BLE001 — proof line is purely best-effort
        logger.debug("latest_token_savings failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Orchestrator + report
# ---------------------------------------------------------------------------


def run_doctor(repo_root: Path) -> tuple[list[CheckResult], int]:
    """Run all checks and return ``(results, exit_code)``.

    Exit code is ``1`` when any *critical* check failed, else ``0``.
    """
    results: list[CheckResult] = [
        check_graph_db(repo_root),
        check_freshness(repo_root),
        check_mcp_config(repo_root),
        check_serve_command(),
        check_server_boot(repo_root),
        check_hooks(repo_root),
        check_embeddings(repo_root),
    ]
    exit_code = 1 if any(r.critical and not r.ok for r in results) else 0
    return results, exit_code


def print_report(results: list[CheckResult], *, repo_root: Path) -> None:
    """Print the ``✓``/``✗`` checklist, hints, and a one-line summary."""
    print(f"code-review-graph doctor — {repo_root}")
    print()
    for r in results:
        print(f"  {r.glyph} {r.name}: {r.message}")
        if not r.ok and r.hint:
            print(f"      → {r.hint}")

    critical_failures = [r for r in results if r.critical and not r.ok]
    warnings = [r for r in results if not r.critical and not r.ok]

    print()
    if critical_failures:
        names = ", ".join(r.name for r in critical_failures)
        print(f"Result: NOT healthy — {len(critical_failures)} critical "
              f"issue(s): {names}")
    elif warnings:
        print(f"Result: healthy with {len(warnings)} warning(s) — "
              f"see hints above")
    else:
        print("Result: healthy — all checks passed")

    # See-your-savings proof when a graph exists.
    proof = latest_token_savings(repo_root)
    if proof:
        print()
        print(f"See your savings: {proof}")
