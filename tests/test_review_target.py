"""Tests for review target resolution: ask when unclear, current vs named base."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codewalk.mcp import server
from codewalk.review.diff_parser import get_diff
from codewalk.review.target import (
    format_ask_for_review_target,
    is_current_branch_alias,
    needs_review_target,
    resolve_diff_target_branch,
)


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
    (repo / "base.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "-m", "main")
    return repo


def test_needs_review_target_when_nothing_specified() -> None:
    assert needs_review_target(None, staged=False, commit=None) is True


def test_does_not_need_target_when_current_alias() -> None:
    assert needs_review_target("current", staged=False, commit=None) is False


def test_does_not_need_target_when_named_branch() -> None:
    assert needs_review_target("main", staged=False, commit=None) is False


def test_does_not_need_target_when_staged_only() -> None:
    assert needs_review_target(None, staged=True, commit=None) is False


def test_does_not_need_target_when_commit_specified() -> None:
    assert needs_review_target(None, staged=False, commit="abc123") is False


def test_current_aliases() -> None:
    assert is_current_branch_alias("current")
    assert is_current_branch_alias("CURRENT")
    assert is_current_branch_alias("current-branch")
    assert not is_current_branch_alias("main")
    assert not is_current_branch_alias(None)


def test_resolve_diff_target_branch_current_becomes_none() -> None:
    assert resolve_diff_target_branch("current") is None
    assert resolve_diff_target_branch("main") == "main"


def test_format_ask_for_review_target_mentions_current_and_named(git_repo: Path) -> None:
    message = format_ask_for_review_target(git_repo)
    assert "which branch" in message.lower()
    assert "current" in message
    assert "main" in message
    assert "Do NOT assume" in message or "do not assume" in message.lower()


def test_run_review_without_branch_asks_instead_of_defaulting(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 2\n", encoding="utf-8")
    result = server.codewalk_run_review(repo_path=str(git_repo))
    assert "Review Session" not in result
    assert "which branch" in result.lower()
    assert "current" in result


def test_run_review_with_current_reviews_local_changes(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 2\n", encoding="utf-8")
    (git_repo / "wip.py").write_text("w = 1\n", encoding="utf-8")
    result = server.codewalk_run_review(target_branch="current", repo_path=str(git_repo))
    assert "Review Session" in result
    assert "base.py" in result
    assert "wip.py" in result


def test_get_diff_target_branch_includes_uncommitted_and_untracked(git_repo: Path) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feature")
    (git_repo / "feature.py").write_text("b = 1\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "feature commit")

    (git_repo / "feature.py").write_text("b = 2\n", encoding="utf-8")
    (git_repo / "untracked.py").write_text("u = 1\n", encoding="utf-8")

    diff = get_diff(target_branch="main", repo_path=str(git_repo))
    assert "feature.py" in diff
    assert "untracked.py" in diff
    assert "+b = 2" in diff


def test_get_diff_current_alias_matches_local_changes(git_repo: Path) -> None:
    (git_repo / "base.py").write_text("a = 9\n", encoding="utf-8")
    local = get_diff(repo_path=str(git_repo))
    via_alias = get_diff(target_branch="current", repo_path=str(git_repo))
    assert via_alias == local
