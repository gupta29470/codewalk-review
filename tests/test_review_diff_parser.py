"""Tests for review.diff_parser: git diff generation and unified-diff parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codewalk.errors import InvalidDiffError
from codewalk.review.diff_parser import get_diff, get_parsed_diff


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    return repo


def _commit_all(repo: Path, message: str = "initial") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_get_diff_empty_repo_no_commits_returns_empty(git_repo: Path) -> None:
    diff = get_diff(repo_path=str(git_repo))
    assert diff == ""


def test_get_diff_untracked_only_new_file(git_repo: Path) -> None:
    (git_repo / "new_file.py").write_text("print('hello')\n", encoding="utf-8")
    diff = get_diff(repo_path=str(git_repo))
    assert "new_file.py" in diff
    assert "+print('hello')" in diff


def test_get_diff_default_includes_staged_unstaged_and_untracked(git_repo: Path) -> None:
    (git_repo / "committed.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo)

    (git_repo / "committed.py").write_text("x = 2\n", encoding="utf-8")
    (git_repo / "staged.py").write_text("y = 1\n", encoding="utf-8")
    _git(git_repo, "add", "staged.py")
    (git_repo / "untracked.py").write_text("z = 1\n", encoding="utf-8")

    diff = get_diff(repo_path=str(git_repo))
    assert "committed.py" in diff
    assert "staged.py" in diff
    assert "untracked.py" in diff


def test_get_diff_staged_only_excludes_unstaged_and_untracked(git_repo: Path) -> None:
    (git_repo / "committed.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo)

    (git_repo / "committed.py").write_text("x = 2\n", encoding="utf-8")
    (git_repo / "staged.py").write_text("y = 1\n", encoding="utf-8")
    _git(git_repo, "add", "staged.py")
    (git_repo / "untracked.py").write_text("z = 1\n", encoding="utf-8")

    diff = get_diff(staged=True, repo_path=str(git_repo))
    assert "staged.py" in diff
    assert "committed.py" not in diff
    assert "untracked.py" not in diff


def test_get_diff_target_branch_includes_branch_commits(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    _git(git_repo, "branch", "-m", "main")
    _git(git_repo, "checkout", "-q", "-b", "feature")

    (git_repo / "feature.py").write_text("b = 1\n", encoding="utf-8")
    _commit_all(git_repo, "feature commit")

    diff = get_diff(target_branch="main", repo_path=str(git_repo))
    assert "feature.py" in diff


def test_get_diff_target_branch_includes_uncommitted_changes(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    _git(git_repo, "branch", "-m", "main")
    _git(git_repo, "checkout", "-q", "-b", "feature")

    (git_repo / "base.py").write_text("a = 2\n", encoding="utf-8")

    diff = get_diff(target_branch="main", repo_path=str(git_repo))
    assert "base.py" in diff
    assert "+a = 2" in diff


def test_get_diff_target_branch_includes_untracked(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    _git(git_repo, "branch", "-m", "main")
    _git(git_repo, "checkout", "-q", "-b", "feature")

    (git_repo / "untracked.py").write_text("u = 1\n", encoding="utf-8")

    diff = get_diff(target_branch="main", repo_path=str(git_repo))
    assert "untracked.py" in diff


def test_get_diff_target_branch_ignores_base_drift(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    _git(git_repo, "branch", "-m", "main")
    _git(git_repo, "checkout", "-q", "-b", "feature")
    (git_repo / "feature.py").write_text("b = 1\n", encoding="utf-8")
    _commit_all(git_repo, "feature commit")

    # Base moves ahead after the branch diverged.
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "main_only.py").write_text("m = 1\n", encoding="utf-8")
    _commit_all(git_repo, "main moves ahead")
    _git(git_repo, "checkout", "-q", "feature")

    diff = get_diff(target_branch="main", repo_path=str(git_repo))
    assert "feature.py" in diff
    assert "main_only.py" not in diff


def test_get_diff_current_alias_matches_default(git_repo: Path) -> None:
    (git_repo / "committed.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo)

    (git_repo / "committed.py").write_text("x = 2\n", encoding="utf-8")
    (git_repo / "staged.py").write_text("y = 1\n", encoding="utf-8")
    _git(git_repo, "add", "staged.py")
    (git_repo / "untracked.py").write_text("z = 1\n", encoding="utf-8")

    assert get_diff(target_branch="current", repo_path=str(git_repo)) == get_diff(
        repo_path=str(git_repo)
    )


def test_get_diff_binary_file_does_not_crash(git_repo: Path) -> None:
    (git_repo / "data.bin").write_bytes(bytes(range(256)))
    diff = get_diff(repo_path=str(git_repo))
    # Should not raise; binary content may or may not appear depending on git.
    assert isinstance(diff, str)


def test_get_parsed_diff_empty_text_returns_empty_list() -> None:
    assert get_parsed_diff("") == []
    assert get_parsed_diff("   \n  ") == []


def test_get_parsed_diff_new_file(git_repo: Path) -> None:
    (git_repo / "new_file.py").write_text("line1\nline2\n", encoding="utf-8")
    diff_text = get_diff(repo_path=str(git_repo))
    diff_files = get_parsed_diff(diff_text)

    assert len(diff_files) == 1
    assert diff_files[0].file_path == "new_file.py"
    assert diff_files[0].is_new_file is True
    assert diff_files[0].added_lines == 2
    assert diff_files[0].language == "python"


def test_get_parsed_diff_modified_file_hunks(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    _commit_all(git_repo)
    (git_repo / "app.py").write_text("a = 1\nb = 99\nc = 3\n", encoding="utf-8")

    diff_text = get_diff(repo_path=str(git_repo))
    diff_files = get_parsed_diff(diff_text)

    assert len(diff_files) == 1
    df = diff_files[0]
    assert df.added_lines == 1
    assert df.removed_lines == 1
    assert len(df.hunks) == 1
    change_types = {line.change_type for line in df.hunks[0].lines}
    assert "added" in change_types
    assert "removed" in change_types
    assert "context" in change_types


def test_get_parsed_diff_deleted_file(git_repo: Path) -> None:
    (git_repo / "gone.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    (git_repo / "gone.py").unlink()

    diff_text = get_diff(repo_path=str(git_repo))
    diff_files = get_parsed_diff(diff_text)
    assert len(diff_files) == 1
    assert diff_files[0].is_deleted is True


def test_get_parsed_diff_binary_file_skipped(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo)
    (git_repo / "data.bin").write_bytes(bytes(range(256)) * 4)
    _git(git_repo, "add", "-A")

    diff_text = get_diff(staged=True, repo_path=str(git_repo))
    diff_files = get_parsed_diff(diff_text)
    # Binary file must not appear as a parsed DiffFile.
    assert all(f.file_path != "data.bin" for f in diff_files)


def test_get_parsed_diff_invalid_diff_text_raises_typed_error() -> None:
    malformed = "--- a/x\n+++ b/x\n@@ -1,5 +1,5 @@\n+only one line here\n"
    with pytest.raises(InvalidDiffError):
        get_parsed_diff(malformed)


def test_get_diff_nonexistent_repo_path_raises_typed_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(InvalidDiffError):
        get_diff(repo_path=str(missing))


def test_get_diff_git_timeout_raises_typed_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    with pytest.raises(InvalidDiffError, match="timed out"):
        get_diff(repo_path=str(git_repo))


def test_synthetic_diff_skips_untracked_file_over_size_limit(git_repo: Path) -> None:
    from codewalk.review import diff_parser

    big_file = git_repo / "big.py"
    big_file.write_text("x = 1\n" * 400_000, encoding="utf-8")
    assert big_file.stat().st_size > diff_parser._MAX_UNTRACKED_FILE_SIZE

    diff = get_diff(repo_path=str(git_repo))
    assert "big.py" not in diff


def test_synthetic_diff_skips_binary_untracked_file(git_repo: Path) -> None:
    (git_repo / "data.bin").write_bytes(b"\x00\x01\x02binary")
    diff = get_diff(repo_path=str(git_repo))
    assert "data.bin" not in diff


def test_synthetic_diff_skips_empty_untracked_file(git_repo: Path) -> None:
    (git_repo / "empty.py").write_text("", encoding="utf-8")
    diff = get_diff(repo_path=str(git_repo))
    assert "empty.py" not in diff


def test_synthetic_diff_skips_symlink(git_repo: Path) -> None:
    target = git_repo / "real.py"
    target.write_text("x = 1\n", encoding="utf-8")
    link = git_repo / "link.py"
    link.symlink_to(target)

    diff = get_diff(repo_path=str(git_repo))
    assert "link.py" not in diff
    # The real (non-symlink) file is a separate untracked file and should appear.
    assert "real.py" in diff


def test_get_diff_commit_mode_with_parent(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo, "first")
    (git_repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _commit_all(git_repo, "second")

    log = _git(git_repo, "log", "--format=%H")
    second_sha = log.stdout.splitlines()[0]

    diff = get_diff(commit=second_sha, repo_path=str(git_repo))
    assert "a.py" in diff


def test_get_diff_commit_mode_without_parent_uses_show(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo, "only commit")

    log = _git(git_repo, "log", "--format=%H")
    only_sha = log.stdout.strip()

    diff = get_diff(commit=only_sha, repo_path=str(git_repo))
    assert "a.py" in diff


def test_get_diff_since_commit_mode(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(git_repo, "base")
    log = _git(git_repo, "log", "--format=%H")
    base_sha = log.stdout.strip()

    (git_repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _commit_all(git_repo, "next")

    diff = get_diff(since_commit=base_sha, repo_path=str(git_repo))
    assert "a.py" in diff


def test_get_parsed_diff_skips_file_with_replacement_char_in_hunk() -> None:
    diff_text = "--- a/weird.py\n+++ b/weird.py\n@@ -1,1 +1,1 @@\n-old\n+new\ufffdcontent\n"
    diff_files = get_parsed_diff(diff_text)
    assert all(f.file_path != "weird.py" for f in diff_files)
