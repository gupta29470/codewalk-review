"""Cross-cutting hardening & edge-case tests (Phase 11).

Most edge cases from the plan's Phase 11 checklist already have dedicated
coverage in their owning module's test file (path traversal in
test_paths.py, symlink loops and non-UTF8 filenames in
test_ingestion_scanner.py, malformed codewalk.yaml in
test_codewalk_config.py, corrupted state files at the unit level in
test_graph_store.py / test_review_stack_detect.py / test_review_session_store.py,
oversized-diff batching in test_review_batching.py). This file covers the
remaining gaps: end-to-end behavior through the MCP layer, concurrency, and
scenarios that don't have a natural home in any single module's test file.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from codewalk.analysis.code_parser import parse_file
from codewalk.errors import GraphCorruptedError, ParseError
from codewalk.graph.graph_store import GraphStore
from codewalk.ingestion.scanner import scan_repo
from codewalk.mcp import server
from codewalk.review.diff_parser import get_parsed_diff
from codewalk.review.renderers.markdown import render_findings_markdown
from codewalk.review.report import Category, Finding, Severity
from codewalk.workspace import Workspace


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    )


def _init_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


# ── Path traversal via MCP tool arguments ─────────────────────────────


def test_run_static_analysis_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    result = server.codewalk_run_static_analysis([str(outside)], repo_path=str(repo))
    assert result.startswith("\u274c")


def test_run_tests_rejects_dotdot_traversal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_tests(["../a.py"], repo_path=str(repo))
    assert result.startswith("\u274c")


# ── Perf smoke: monorepo with 1000+ files ─────────────────────────────


def test_monorepo_with_many_files_does_not_hang(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(1200):
        sub = repo / f"pkg{i % 20}"
        sub.mkdir(exist_ok=True)
        (sub / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")

    scan_result = scan_repo(repo)
    assert len(scan_result.files) >= 1200

    ws = Workspace.build(repo)
    stats = ws.graph_store.get_stats()
    assert stats.files >= 1200


# ── Corrupted on-disk state surfaces typed errors / graceful fallback ──


def test_corrupted_graph_duckdb_raises_typed_error_not_traceback(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    codewalk_dir = repo / ".codewalk"
    codewalk_dir.mkdir()
    (codewalk_dir / "graph.duckdb").write_bytes(b"not a real duckdb file")

    with pytest.raises(GraphCorruptedError):
        GraphStore(codewalk_dir / "graph.duckdb")


def test_analyze_codebase_corrupted_graph_returns_error_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    codewalk_dir = repo / ".codewalk"
    codewalk_dir.mkdir()
    (codewalk_dir / "graph.duckdb").write_bytes(b"not a real duckdb file")

    result = server.codewalk_analyze_codebase(repo_path=str(repo))
    assert result.startswith("\u274c")


def test_run_review_corrupted_stack_context_does_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    stack_dir = repo / ".codewalk"
    stack_dir.mkdir()
    (stack_dir / "stack_context.json").write_text("{not valid json", encoding="utf-8")

    result = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    assert "Review Session" in result


def test_get_review_summary_corrupted_llm_findings_does_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = start.splitlines()[0].split("`")[1]

    session_dirs = [d for d in (repo / ".codewalk" / "review_session").iterdir() if d.is_dir()]
    findings_path = session_dirs[0] / "llm_findings.json"
    findings_path.write_text("{not valid json", encoding="utf-8")

    result = server.codewalk_get_review_summary(session_id, repo_path=str(repo))
    assert result.startswith("\u274c") is False
    assert "Review Summary" in result


# ── Malformed codewalk.yaml doesn't crash the MCP layer ───────────────


def test_analyze_codebase_malformed_yaml_warns_uses_defaults(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "codewalk.yaml").write_text(
        "indexing:\n  exclude: 'not-a-list'\nunknown_key: 123\n", encoding="utf-8"
    )
    result = server.codewalk_analyze_codebase(repo_path=str(repo))
    assert "Built graph" in result


# ── Concurrent MCP tool calls against the same repo ───────────────────


def test_concurrent_graph_builds_no_corruption(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {f"f{i}.py": f"x{i} = {i}\n" for i in range(10)})

    errors: list[Exception] = []
    results: list[str] = []

    def _build() -> None:
        try:
            results.append(server.codewalk_analyze_codebase(repo_path=str(repo)))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_build) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all("Built graph" in r or "Refreshed graph" in r for r in results)


def test_concurrent_review_sessions_same_repo_get_distinct_ids(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {f"f{i}.py": f"x{i} = {i}\n" for i in range(6)})
    for i in range(6):
        (repo / f"f{i}.py").write_text(f"x{i} = {i + 100}\n", encoding="utf-8")

    session_ids: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _start() -> None:
        try:
            out = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
            session_id = out.splitlines()[0].split("`")[1]
            with lock:
                session_ids.append(session_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_start) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(session_ids) == 3
    assert len(set(session_ids)) == 3  # every session got a distinct ID


# ── XSS/script-like content in findings rendered as inert text ───────


def test_finding_with_script_like_content_rendered_as_inert_text() -> None:
    finding = Finding(
        severity=Severity.ERROR,
        category=Category.SECURITY,
        file_path="a.py",
        line_number=1,
        title="<script>alert(1)</script>",
        explanation="{{7*7}} ${7*7} <img src=x onerror=alert(1)>",
    )
    markdown = render_findings_markdown([finding])
    # The content must survive verbatim as inert text -- never executed,
    # never silently stripped or template-expanded.
    assert "<script>alert(1)</script>" in markdown
    assert "{{7*7}}" in markdown
    assert "49" not in markdown  # would appear if {{7*7}} were template-evaluated


def test_finding_with_script_like_content_survives_json_round_trip(tmp_path: Path) -> None:
    finding = Finding(
        severity=Severity.ERROR,
        category=Category.SECURITY,
        file_path="a.py",
        line_number=1,
        title="<script>alert(1)</script>",
        explanation="plain text",
    )
    data = finding.to_dict()
    serialized = json.dumps(data)
    restored = Finding.model_validate(json.loads(serialized))
    assert restored.title == "<script>alert(1)</script>"


# ── CRLF line endings in diff parsing ──────────────────────────────────


def test_diff_parsing_handles_crlf_line_endings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "core.autocrlf", "false")

    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")

    (repo / "a.py").write_bytes(b"x = 1\r\ny = 3\r\n")

    from codewalk.review.diff_parser import get_diff

    diff_text = get_diff(repo_path=str(repo))
    diff_files = get_parsed_diff(diff_text)
    assert len(diff_files) == 1
    assert diff_files[0].added_lines >= 1


# ── Non-UTF8 file content in parsers ───────────────────────────────────


def test_parse_file_python_invalid_utf8_content_raises_typed_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.py"
    bad_file.write_bytes(b"x = 1\n\xff\xfe invalid bytes\n")
    with pytest.raises(ParseError):
        parse_file(bad_file, "python")


def test_parse_file_tree_sitter_invalid_utf8_content_does_not_crash(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.go"
    bad_file.write_bytes(b"package main\n\xff\xfe func broken() {}\n")
    # Tree-sitter parses raw bytes; invalid UTF-8 degrades via errors="replace"
    # rather than raising.
    symbols = parse_file(bad_file, "go")
    assert isinstance(symbols, list)


# ── stdout is reserved for the MCP stdio protocol framing ─────────────


def test_mcp_tools_never_write_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path, {"a.py": "def helper():\n    return 1\n"})
    (repo / "a.py").write_text("def helper():\n    return 2\n", encoding="utf-8")

    server.codewalk_analyze_codebase(repo_path=str(repo))
    server.codewalk_get_overview(repo_path=str(repo))
    server.codewalk_explain_function("helper", repo_path=str(repo))
    server.codewalk_run_review(repo_path=str(repo))
    server.codewalk_run_review(repo_path=str(tmp_path / "does-not-exist"))

    captured = capsys.readouterr()
    assert captured.out == ""
