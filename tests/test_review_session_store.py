"""Tests for review.session_store: atomic persistence of sessions and findings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codewalk.review import session_store
from codewalk.review.report import Category, Confidence, Finding, Severity, Source
from codewalk.review.session import ReviewSession, SessionStatus


def _make_session(repo_root: Path, folder_name: str = "1-Jan-2026-main") -> ReviewSession:
    return ReviewSession(
        session_id=ReviewSession.generate_id(),
        repo_path=str(repo_root),
        target_branch="main",
        commit=None,
        staged=False,
        folder_name=folder_name,
    )


def _make_finding(title: str = "Bug found", file_path: str = "a.py") -> Finding:
    return Finding(
        severity=Severity.ERROR,
        category=Category.BUG,
        file_path=file_path,
        line_number=1,
        title=title,
        explanation="explanation text",
        confidence=Confidence.HIGH,
        source=Source.LLM,
    )


def test_save_and_load_session_round_trip(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session_store.save_session(session)

    loaded = session_store.load_session(tmp_path, session.session_id)
    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.repo_path == session.repo_path
    assert loaded.target_branch == "main"
    assert loaded.status == SessionStatus.ACTIVE


def test_load_session_by_folder_round_trip(tmp_path: Path) -> None:
    session = _make_session(tmp_path, folder_name="my-folder")
    session_store.save_session(session)

    loaded = session_store.load_session_by_folder(tmp_path, "my-folder")
    assert loaded is not None
    assert loaded.session_id == session.session_id


def test_load_session_missing_returns_none(tmp_path: Path) -> None:
    assert session_store.load_session(tmp_path, "nonexistent-id") is None


def test_load_session_by_folder_missing_returns_none(tmp_path: Path) -> None:
    assert session_store.load_session_by_folder(tmp_path, "nonexistent-folder") is None


def test_load_session_falls_back_to_linear_scan_when_index_missing(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session_store.save_session(session)

    # Corrupt/remove the index to force a linear scan.
    index_path = tmp_path / ".codewalk" / "review_session" / "index.json"
    index_path.unlink()

    loaded = session_store.load_session(tmp_path, session.session_id)
    assert loaded is not None
    assert loaded.session_id == session.session_id


def test_load_session_corrupted_json_is_handled_not_crashed(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session_store.save_session(session)

    session_path = tmp_path / ".codewalk" / "review_session" / session.folder_name / "session.json"
    session_path.write_text("{not valid json", encoding="utf-8")

    assert session_store.load_session_by_folder(tmp_path, session.folder_name) is None


def test_save_session_is_atomic_survives_simulated_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _make_session(tmp_path)
    session_store.save_session(session)

    session_dir = tmp_path / ".codewalk" / "review_session" / session.folder_name
    original_path = session_dir / "session.json"
    original_content = original_path.read_text(encoding="utf-8")

    def _boom(self: Path, target: Path) -> None:
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(Path, "replace", _boom)

    session.status = SessionStatus.COMPLETED
    with pytest.raises(OSError, match="simulated crash"):
        session_store.save_session(session)

    monkeypatch.undo()

    # Original file must be untouched.
    assert original_path.read_text(encoding="utf-8") == original_content
    # No leftover .tmp files.
    assert list(session_dir.glob("*.tmp")) == []


def test_find_last_session_returns_most_recent(tmp_path: Path) -> None:
    older = _make_session(tmp_path, folder_name="older")
    session_store.save_session(older)
    newer = _make_session(tmp_path, folder_name="newer")
    session_store.save_session(newer)

    # Force distinct mtimes.
    import os
    import time

    time.sleep(0.01)
    (tmp_path / ".codewalk" / "review_session" / "newer").touch()
    os.utime(tmp_path / ".codewalk" / "review_session" / "newer", None)

    found = session_store.find_last_session(tmp_path)
    assert found is not None


def test_find_last_session_filters_by_branch(tmp_path: Path) -> None:
    main_session = ReviewSession(
        session_id=ReviewSession.generate_id(),
        repo_path=str(tmp_path),
        target_branch="main",
        commit=None,
        staged=False,
        folder_name="main-session",
    )
    session_store.save_session(main_session)

    found = session_store.find_last_session(tmp_path, branch="main")
    assert found is not None
    assert found.target_branch == "main"

    not_found = session_store.find_last_session(tmp_path, branch="does-not-exist")
    assert not_found is None


def test_find_last_session_empty_repo_returns_none(tmp_path: Path) -> None:
    assert session_store.find_last_session(tmp_path) is None


def test_list_sessions(tmp_path: Path) -> None:
    session_store.save_session(_make_session(tmp_path, folder_name="one"))
    session_store.save_session(_make_session(tmp_path, folder_name="two"))
    names = session_store.list_sessions(tmp_path)
    assert set(names) == {"one", "two"}


def test_list_sessions_empty_repo(tmp_path: Path) -> None:
    assert session_store.list_sessions(tmp_path) == []


def test_delete_session(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    session_store.save_session(session)
    assert session_store.delete_session(tmp_path, session.session_id) is True
    assert session_store.load_session(tmp_path, session.session_id) is None


def test_delete_session_nonexistent_returns_false(tmp_path: Path) -> None:
    assert session_store.delete_session(tmp_path, "nope") is False


def test_save_and_load_findings_round_trip(tmp_path: Path) -> None:
    findings = [_make_finding("Bug 1"), _make_finding("Bug 2", file_path="b.py")]
    session_store.save_findings(tmp_path, "folder-a", findings)

    loaded = session_store.load_findings(tmp_path, "folder-a")
    assert len(loaded) == 2
    assert {f.title for f in loaded} == {"Bug 1", "Bug 2"}


def test_load_findings_missing_returns_empty(tmp_path: Path) -> None:
    assert session_store.load_findings(tmp_path, "no-such-folder") == []


def test_save_findings_writes_markdown_companion(tmp_path: Path) -> None:
    findings = [_make_finding()]
    session_store.save_findings(tmp_path, "folder-a", findings)

    md_path = tmp_path / ".codewalk" / "review_session" / "folder-a" / "llm_findings.md"
    assert md_path.exists()
    assert "Bug found" in md_path.read_text(encoding="utf-8")


def test_load_findings_skips_corrupted_entry_not_crash(tmp_path: Path) -> None:
    findings_path = tmp_path / ".codewalk" / "review_session" / "folder-a"
    findings_path.mkdir(parents=True)
    good = _make_finding().to_dict()
    bad = {"severity": "not-a-real-severity"}
    (findings_path / "llm_findings.json").write_text(json.dumps([good, bad]), encoding="utf-8")

    loaded = session_store.load_findings(tmp_path, "folder-a")
    assert len(loaded) == 1


def test_load_findings_corrupted_json_returns_empty(tmp_path: Path) -> None:
    findings_dir = tmp_path / ".codewalk" / "review_session" / "folder-a"
    findings_dir.mkdir(parents=True)
    (findings_dir / "llm_findings.json").write_text("[not json", encoding="utf-8")

    assert session_store.load_findings(tmp_path, "folder-a") == []


def test_append_findings(tmp_path: Path) -> None:
    session_store.save_findings(tmp_path, "folder-a", [_make_finding("First")])
    merged = session_store.append_findings(
        tmp_path, "folder-a", [_make_finding("Second", file_path="b.py")]
    )
    assert {f.title for f in merged} == {"First", "Second"}
    loaded = session_store.load_findings(tmp_path, "folder-a")
    assert {f.title for f in loaded} == {"First", "Second"}


def test_save_and_load_static_findings_round_trip(tmp_path: Path) -> None:
    findings = [_make_finding("Static bug")]
    session_store.save_static_findings(tmp_path, "folder-a", findings)
    loaded = session_store.load_static_findings(tmp_path, "folder-a")
    assert len(loaded) == 1
    assert loaded[0].title == "Static bug"


def test_static_and_llm_findings_are_independent(tmp_path: Path) -> None:
    session_store.save_static_findings(tmp_path, "folder-a", [_make_finding("Static")])
    session_store.save_findings(tmp_path, "folder-a", [_make_finding("LLM")])

    assert [f.title for f in session_store.load_static_findings(tmp_path, "folder-a")] == ["Static"]
    assert [f.title for f in session_store.load_findings(tmp_path, "folder-a")] == ["LLM"]
