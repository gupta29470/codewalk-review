"""Tests for codewalk.paths -- path-traversal guards and .codewalk/ layout helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.errors import PathTraversalError
from codewalk.paths import (
    codewalk_dir,
    ensure_codewalk_dir,
    graph_db_path,
    resolve_within_repo,
    review_session_dir,
    rubrics_override_dir,
    stack_context_path,
)


def test_resolve_within_repo_happy_path_relative(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")

    resolved = resolve_within_repo(tmp_path, "src/main.py")

    assert resolved == (tmp_path / "src" / "main.py").resolve()


def test_resolve_within_repo_root_itself(tmp_path: Path) -> None:
    assert resolve_within_repo(tmp_path, ".") == tmp_path.resolve()


def test_resolve_within_repo_rejects_dotdot_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within_repo(tmp_path, "../../etc/passwd")


def test_resolve_within_repo_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    with pytest.raises(PathTraversalError):
        resolve_within_repo(tmp_path, str(outside))


def test_resolve_within_repo_allows_absolute_path_inside_repo(tmp_path: Path) -> None:
    inside = tmp_path / "config.yaml"
    inside.write_text("key: value\n")

    resolved = resolve_within_repo(tmp_path, str(inside))

    assert resolved == inside.resolve()


def test_resolve_within_repo_rejects_symlink_escape(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside_dir = tmp_path / "outside"
    repo_root.mkdir()
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("do not read\n")

    link = repo_root / "escape_link"
    link.symlink_to(outside_dir)

    with pytest.raises(PathTraversalError):
        resolve_within_repo(repo_root, "escape_link/secret.txt")


def test_resolve_within_repo_allows_symlink_pointing_inside_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    real_dir = repo_root / "real"
    real_dir.mkdir()
    (real_dir / "file.txt").write_text("ok\n")

    link = repo_root / "link"
    link.symlink_to(real_dir)

    resolved = resolve_within_repo(repo_root, "link/file.txt")

    assert resolved == (real_dir / "file.txt").resolve()


def test_resolve_within_repo_nonexistent_repo_root_still_resolves(tmp_path: Path) -> None:
    """repo_root need not exist yet (e.g. before the first analyze call)."""
    missing_root = tmp_path / "not_created_yet"
    resolved = resolve_within_repo(missing_root, "some/file.py")
    assert resolved == (missing_root / "some" / "file.py").resolve()


def test_codewalk_dir_and_ensure_codewalk_dir(tmp_path: Path) -> None:
    expected = tmp_path.resolve() / ".codewalk"
    assert codewalk_dir(tmp_path) == expected
    assert not expected.exists()

    created = ensure_codewalk_dir(tmp_path)
    assert created == expected
    assert created.is_dir()

    # Calling again on an already-existing directory must not raise.
    ensure_codewalk_dir(tmp_path)


def test_layout_helpers_return_expected_subpaths(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert graph_db_path(root) == root / ".codewalk" / "graph.duckdb"
    assert stack_context_path(root) == root / ".codewalk" / "stack_context.json"
    expected_session_dir = root / ".codewalk" / "review_session" / "my-session"
    assert review_session_dir(root, "my-session") == expected_session_dir
    assert rubrics_override_dir(root) == root / ".codewalk" / "rubrics"
