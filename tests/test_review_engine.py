"""Tests for review.engine: full session lifecycle orchestration."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from codewalk.errors import InvalidFindingError, SessionNotFoundError
from codewalk.review import engine
from codewalk.review.session import SessionStatus
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


def _make_finding_dict(file_path: str = "a.py", title: str = "Bug") -> dict[str, object]:
    return {
        "severity": "error",
        "category": "bug",
        "file_path": file_path,
        "line_number": 1,
        "title": title,
        "explanation": "explanation text",
    }


def test_start_review_no_changes_returns_clean_result_not_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = engine.start_review(repo)
    assert result.has_changes is False
    assert result.session is None
    assert result.first_batch is None


def test_start_review_happy_path_creates_session_and_first_batch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    result = engine.start_review(repo)
    assert result.has_changes is True
    assert result.session is not None
    assert result.session.status == SessionStatus.ACTIVE
    assert result.total_files == 1
    assert result.total_batches >= 1
    assert result.first_batch is not None
    assert result.first_batch.batch_index == 0
    assert "a.py" in result.first_batch.file_paths
    assert "a.py" in result.first_batch.context


def test_full_lifecycle_start_next_submit_summary(tmp_path: Path) -> None:
    repo = _init_repo(
        tmp_path,
        {f"f{i}.py": f"x{i} = {i}\n" for i in range(6)},
    )
    for i in range(6):
        (repo / f"f{i}.py").write_text(f"x{i} = {i + 100}\n", encoding="utf-8")

    # Force small batches so we exercise next_batch across multiple batches.
    result = engine.start_review(repo, max_tokens_per_batch=1)
    assert result.has_changes
    assert result.session is not None
    session_id = result.session.session_id
    assert result.total_batches >= 2

    engine.submit_findings(repo, session_id, [_make_finding_dict(result.first_batch.file_paths[0])])

    batches_seen = 1
    while True:
        batch = engine.next_batch(repo, session_id)
        if batch is None:
            break
        batches_seen += 1
        engine.submit_findings(repo, session_id, [])

    assert batches_seen == result.total_batches

    summary = engine.get_summary(repo, session_id)
    assert summary.total_files == 6
    assert len(summary.llm_findings) == 1


def test_next_batch_after_all_batches_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    assert engine.next_batch(repo, session_id) is None


def test_next_batch_unknown_session_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    with pytest.raises(SessionNotFoundError):
        engine.next_batch(repo, "does-not-exist")


def test_submit_findings_unknown_session_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    with pytest.raises(SessionNotFoundError):
        engine.submit_findings(repo, "does-not-exist", [])


def test_get_summary_unknown_session_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    with pytest.raises(SessionNotFoundError):
        engine.get_summary(repo, "does-not-exist")


def test_get_review_details_unknown_session_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    with pytest.raises(SessionNotFoundError):
        engine.get_review_details(repo, "does-not-exist")


def test_submit_findings_invalid_severity_raises_clear_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    bad_finding = _make_finding_dict("a.py")
    bad_finding["severity"] = "catastrophic"
    with pytest.raises(InvalidFindingError):
        engine.submit_findings(repo, session_id, [bad_finding])


def test_submit_findings_negative_line_number_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    bad_finding = _make_finding_dict("a.py")
    bad_finding["line_number"] = -5
    with pytest.raises(InvalidFindingError):
        engine.submit_findings(repo, session_id, [bad_finding])


def test_submit_findings_file_path_outside_batch_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    hallucinated = _make_finding_dict("not_in_this_batch.py")
    with pytest.raises(InvalidFindingError):
        engine.submit_findings(repo, session_id, [hallucinated])


def test_submit_findings_line_number_far_beyond_file_length_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    bad_finding = _make_finding_dict("a.py")
    bad_finding["line_number"] = 999_999
    with pytest.raises(InvalidFindingError):
        engine.submit_findings(repo, session_id, [bad_finding])


def test_submit_findings_empty_list_is_valid_no_notes_required(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    submit_result = engine.submit_findings(repo, session_id, [])
    assert submit_result.saved_count == 0
    assert submit_result.running_total == 0


def test_submit_findings_duplicate_ids_deduped(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    finding = _make_finding_dict("a.py")
    submit_result = engine.submit_findings(repo, session_id, [finding, dict(finding)])
    assert submit_result.saved_count == 1


def test_submit_findings_stable_ids_across_calls(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    session_id = result.session.session_id

    engine.submit_findings(repo, session_id, [_make_finding_dict("a.py", title="Same bug")])
    summary = engine.get_summary(repo, session_id)
    first_id = summary.llm_findings[0].id

    # A second, independent submission of the "same" finding gets the same ID.
    repo2 = _init_repo(tmp_path / "other", {"a.py": "x = 1\n"})
    (repo2 / "a.py").write_text("x = 2\n", encoding="utf-8")
    result2 = engine.start_review(repo2)
    engine.submit_findings(
        repo2, result2.session.session_id, [_make_finding_dict("a.py", title="Same bug")]
    )
    summary2 = engine.get_summary(repo2, result2.session.session_id)
    assert summary2.llm_findings[0].id == first_id


def test_re_review_no_previous_session_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(SessionNotFoundError):
        engine.re_review(repo)


def test_re_review_hides_previously_rejected_findings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    first = engine.start_review(repo)
    engine.submit_findings(
        repo, first.session.session_id, [_make_finding_dict("a.py", title="Old bug")]
    )
    summary = engine.get_summary(repo, first.session.session_id)
    finding_id = summary.llm_findings[0].id

    # Mark the finding as user-rejected directly in llm_findings.json (simulating host-edited JSON).
    folder = first.session.folder_name
    findings_path = repo / ".codewalk" / "review_session" / folder / "llm_findings.json"
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    for item in data:
        if item["id"] == finding_id:
            item["user_verdict"] = "rejected"
    findings_path.write_text(json.dumps(data), encoding="utf-8")

    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    second = engine.re_review(repo)
    assert second.has_changes
    assert second.rejected_count == 1

    engine.submit_findings(
        repo, second.session.session_id, [_make_finding_dict("a.py", title="Old bug")]
    )
    summary2 = engine.get_summary(repo, second.session.session_id)
    assert summary2.rejected_filtered_count == 1
    assert all(f.id != finding_id for f in summary2.llm_findings)


def test_re_review_no_changes_returns_clean_result(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    first = engine.start_review(repo)
    engine.submit_findings(repo, first.session.session_id, [])

    # Commit so there's nothing left to diff.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    result = engine.re_review(repo)
    assert result.has_changes is False


def test_get_review_details_returns_counts(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = engine.start_review(repo)
    engine.submit_findings(repo, result.session.session_id, [_make_finding_dict("a.py")])

    details = engine.get_review_details(repo, result.session.session_id)
    assert details.session.session_id == result.session.session_id
    assert details.llm_findings_count == 1


def test_concurrent_submit_calls_do_not_corrupt_findings_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {f"f{i}.py": f"x{i} = {i}\n" for i in range(3)})
    for i in range(3):
        (repo / f"f{i}.py").write_text(f"x{i} = {i + 50}\n", encoding="utf-8")

    result = engine.start_review(repo, max_tokens_per_batch=10_000_000)
    session_id = result.session.session_id

    errors: list[Exception] = []

    def _submit(idx: int) -> None:
        try:
            engine.submit_findings(
                repo, session_id, [_make_finding_dict("f0.py", title=f"Concurrent {idx}")]
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_submit, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    folder = result.session.folder_name
    findings_path = repo / ".codewalk" / "review_session" / folder / "llm_findings.json"
    # The file must always be valid, parseable JSON -- never a torn/partial write.
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)


def test_start_review_accepts_explicit_workspace(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    ws = Workspace.build(repo)
    result = engine.start_review(repo, workspace=ws)
    assert result.has_changes


def test_start_review_ignores_codewalk_internal_dir_as_untracked_change(tmp_path: Path) -> None:
    """`.codewalk/graph.duckdb` is untracked but must never count as a user change."""
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    Workspace.build(repo)  # creates .codewalk/graph.duckdb, left untracked in the repo
    result = engine.start_review(repo)
    assert result.has_changes is False
