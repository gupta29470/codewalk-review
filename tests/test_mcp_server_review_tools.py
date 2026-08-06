"""Tests for mcp.server: review MCP tools (thin-wrapper behavior)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codewalk.mcp import server


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


def _extract_session_id(run_review_output: str) -> str:
    # Output starts with "# Review Session: `<id>`" (or "# Re-Review Session: `<id>`").
    first_line = run_review_output.splitlines()[0]
    return first_line.split("`")[1]


def _finding(file_path: str = "a.py", title: str = "Bug") -> dict[str, object]:
    return {
        "severity": "error",
        "category": "bug",
        "file_path": file_path,
        "line_number": 1,
        "title": title,
        "explanation": "explanation text",
    }


def test_run_review_no_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    assert "No changes found" in result


def test_run_review_without_target_asks_for_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_review(repo_path=str(repo))
    assert "which branch" in result.lower()
    assert "Review Session" not in result


def test_run_review_no_changes_staged_description(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_review(staged=True, repo_path=str(repo))
    assert "staged changes" in result


def test_run_review_no_changes_target_branch_description(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    _git(repo, "branch", "-m", "main")
    result = server.codewalk_run_review(target_branch="main", repo_path=str(repo))
    assert "working tree vs `main`" in result


def test_run_review_no_changes_commit_description(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    _git(repo, "commit", "--allow-empty", "-q", "-m", "empty commit")
    log = _git(repo, "log", "--format=%H")
    sha = log.stdout.splitlines()[0]
    result = server.codewalk_run_review(commit=sha, repo_path=str(repo))
    assert f"commit `{sha}`" in result


def test_run_review_bad_repo_path_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = server.codewalk_run_review(repo_path=str(missing))
    assert result.startswith("\u274c")


def test_full_review_lifecycle_via_mcp_tools(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    assert "Review Session" in start
    session_id = _extract_session_id(start)

    submit = server.codewalk_submit_batch_findings(
        session_id, [_finding("a.py")], repo_path=str(repo)
    )
    assert "Saved 1 finding" in submit

    next_batch = server.codewalk_review_next_batch(session_id, repo_path=str(repo))
    assert "All batches reviewed" in next_batch

    summary = server.codewalk_get_review_summary(session_id, repo_path=str(repo))
    assert "Review Summary" in summary
    assert "Bug" in summary


def test_multi_batch_review_flow(tmp_path: Path) -> None:
    # Each file's *diff* (not just its content) must be large, since batch
    # token estimates are driven by added+removed line counts. Rewriting the
    # whole file guarantees a large diff that pushes past the 50k/batch budget.
    old_content = "\n".join(f"line_{i} = {i}" for i in range(2000))
    new_content = "\n".join(f"line_{i} = {i * 2}" for i in range(2000))
    files = {f"f{i}.py": old_content for i in range(6)}
    repo = _init_repo(tmp_path, files)
    for i in range(6):
        (repo / f"f{i}.py").write_text(new_content, encoding="utf-8")

    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    assert "batch(es)" in start
    session_id = _extract_session_id(start)

    batches_seen = 1
    while True:
        server.codewalk_submit_batch_findings(session_id, [], repo_path=str(repo))
        next_batch = server.codewalk_review_next_batch(session_id, repo_path=str(repo))
        if "All batches reviewed" in next_batch:
            break
        assert "Batch" in next_batch
        batches_seen += 1

    assert batches_seen >= 2


def test_review_next_batch_unknown_session_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_review_next_batch("does-not-exist", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_submit_batch_findings_unknown_session_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_submit_batch_findings("does-not-exist", [], repo_path=str(repo))
    assert result.startswith("\u274c")


def test_submit_batch_findings_malformed_findings_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)

    malformed = {"severity": "not-a-real-severity", "file_path": "a.py"}
    result = server.codewalk_submit_batch_findings(session_id, [malformed], repo_path=str(repo))
    assert result.startswith("\u274c")


def test_get_review_summary_unknown_session_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_review_summary("does-not-exist", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_get_review_details_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)

    result = server.codewalk_get_review_details(session_id, repo_path=str(repo))
    assert session_id in result
    assert "Status: active" in result


def test_get_review_details_unknown_session_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_review_details("does-not-exist", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_re_review_no_previous_session_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = server.codewalk_re_review(target_branch="current", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_re_review_without_target_asks_for_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_re_review(repo_path=str(repo))
    assert "which branch" in result.lower()


def test_re_review_hides_rejected_findings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)
    server.codewalk_submit_batch_findings(session_id, [_finding("a.py")], repo_path=str(repo))

    summary = server.codewalk_get_review_summary(session_id, repo_path=str(repo))
    assert "Bug" in summary

    # Simulate the host editing llm_findings.json to reject the finding.
    session_dirs = list((repo / ".codewalk" / "review_session").iterdir())
    findings_path = next(d for d in session_dirs if d.is_dir()) / "llm_findings.json"
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    for item in data:
        item["user_verdict"] = "rejected"
    findings_path.write_text(json.dumps(data), encoding="utf-8")

    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    re_review_out = server.codewalk_re_review(target_branch="current", repo_path=str(repo))
    assert "previously rejected" in re_review_out


def test_accept_and_verify_fix_no_findings_yet(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)
    server.codewalk_submit_batch_findings(session_id, [], repo_path=str(repo))

    result = server.codewalk_accept_and_verify_fix(session_id, repo_path=str(repo))
    assert result.startswith("\u274c")
    assert "Run a review first" in result


def test_accept_and_verify_fix_all_undecided(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)
    server.codewalk_submit_batch_findings(session_id, [_finding("a.py")], repo_path=str(repo))

    result = server.codewalk_accept_and_verify_fix(session_id, repo_path=str(repo))
    assert "No accepted findings" in result


def test_accept_and_verify_fix_mixed_verdicts(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)
    server.codewalk_submit_batch_findings(
        session_id,
        [_finding("a.py", "Accepted bug"), _finding("b.py", "Rejected bug")],
        repo_path=str(repo),
    )

    session_dirs = [d for d in (repo / ".codewalk" / "review_session").iterdir() if d.is_dir()]
    findings_path = session_dirs[0] / "llm_findings.json"
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data[0]["user_verdict"] = "accepted"
    data[1]["user_verdict"] = "rejected"
    findings_path.write_text(json.dumps(data), encoding="utf-8")

    result = server.codewalk_accept_and_verify_fix(session_id, repo_path=str(repo))
    assert "Accepted bug" in result
    assert "Rejected bug" not in result
    assert "1 rejected" in result


def test_accept_and_verify_fix_finding_missing_optional_fields(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    start = server.codewalk_run_review(target_branch="current", repo_path=str(repo))
    session_id = _extract_session_id(start)

    finding = _finding("a.py")
    del finding["line_number"]
    server.codewalk_submit_batch_findings(session_id, [finding], repo_path=str(repo))

    session_dirs = [d for d in (repo / ".codewalk" / "review_session").iterdir() if d.is_dir()]
    findings_path = session_dirs[0] / "llm_findings.json"
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data[0]["user_verdict"] = "accepted"
    findings_path.write_text(json.dumps(data), encoding="utf-8")

    result = server.codewalk_accept_and_verify_fix(session_id, repo_path=str(repo))
    assert "None" not in result
    assert "null" not in result
    assert "Current code" not in result


def test_run_static_analysis_path_traversal_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_static_analysis(["../../etc/passwd"], repo_path=str(repo))
    assert result.startswith("\u274c")


def test_run_tests_path_traversal_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_tests(["../../etc/passwd"], repo_path=str(repo))
    assert result.startswith("\u274c")


def test_run_static_analysis_no_files_defaults_to_python(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_run_static_analysis([], repo_path=str(repo))
    assert "\u274c" not in result or "Skipped" in result


def test_run_tests_no_command_configured_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.rb": "puts 1\n"})
    result = server.codewalk_run_tests(["a.rb"], repo_path=str(repo))
    assert "No test command configured" in result
