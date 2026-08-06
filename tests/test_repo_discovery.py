"""Tests for codewalk.repo_discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.errors import RepoNotConfiguredError
from codewalk.repo_discovery import find_repo_root, resolve_repo_root


def test_find_repo_root_at_git_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert find_repo_root(tmp_path) == tmp_path.resolve()


def test_find_repo_root_walks_up_from_nested_subdir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "pkg" / "sub"
    nested.mkdir(parents=True)

    assert find_repo_root(nested) == tmp_path.resolve()


def test_find_repo_root_falls_back_to_start_dir_when_no_git(tmp_path: Path) -> None:
    non_git_dir = tmp_path / "not_a_repo"
    non_git_dir.mkdir()

    assert find_repo_root(non_git_dir) == non_git_dir.resolve()


def test_find_repo_root_git_marker_can_be_a_file(tmp_path: Path) -> None:
    """Worktrees/submodules use a `.git` *file* (containing `gitdir: ...`), not a directory."""
    (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/example\n", encoding="utf-8")
    assert find_repo_root(tmp_path) == tmp_path.resolve()


def test_find_repo_root_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert find_repo_root() == tmp_path.resolve()


def test_find_repo_root_raises_for_nonexistent_start_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(RepoNotConfiguredError):
        find_repo_root(missing)


def test_resolve_repo_root_honors_explicit_path(tmp_path: Path) -> None:
    git_repo = tmp_path / "git_repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()

    explicit_target = tmp_path / "other_dir"
    explicit_target.mkdir()

    # Even though start_dir points at a git repo, the explicit path wins.
    result = resolve_repo_root(explicit_repo_path=explicit_target, start_dir=git_repo)
    assert result == explicit_target.resolve()


def test_resolve_repo_root_raises_for_nonexistent_explicit_path(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(RepoNotConfiguredError):
        resolve_repo_root(explicit_repo_path=missing)


def test_resolve_repo_root_falls_back_to_discovery_without_explicit_path(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert resolve_repo_root(start_dir=tmp_path) == tmp_path.resolve()


def test_resolve_repo_root_rejects_explicit_path_that_is_a_file(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(RepoNotConfiguredError):
        resolve_repo_root(explicit_repo_path=a_file)
