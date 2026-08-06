"""Repo root discovery.

The repo root is identified by the nearest enclosing `.git` marker (directory
or file, so git worktrees/submodules resolve correctly). If no `.git` marker
is found anywhere above the start directory, discovery falls back to the
start directory itself rather than raising -- codewalk must remain usable in
non-git sandboxes and tests.
"""

from __future__ import annotations

from pathlib import Path

from codewalk.errors import RepoNotConfiguredError


def find_repo_root(start_dir: Path | str | None = None) -> Path:
    """Walk up from `start_dir` looking for the nearest `.git` marker.

    Args:
        start_dir: Directory to start from. Defaults to the current working
            directory.

    Returns:
        The nearest ancestor (including `start_dir`) containing a `.git`
        marker, or `start_dir` itself if none is found.

    Raises:
        RepoNotConfiguredError: if `start_dir` does not exist.
    """
    start = Path(start_dir).resolve() if start_dir is not None else Path.cwd().resolve()
    if not start.is_dir():
        raise RepoNotConfiguredError(f"start_dir does not exist or is not a directory: {start}")

    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            return parent
    return start


def resolve_repo_root(
    explicit_repo_path: Path | str | None = None,
    start_dir: Path | str | None = None,
) -> Path:
    """Resolve the repo root, honoring an explicit override if given.

    Args:
        explicit_repo_path: If provided, used as-is (validated to exist);
            discovery is skipped entirely.
        start_dir: Passed to `find_repo_root()` when no explicit path is given.

    Returns:
        The resolved repo root.

    Raises:
        RepoNotConfiguredError: if `explicit_repo_path` is given but does not
            exist, or if `start_dir` does not exist.
    """
    if explicit_repo_path is not None:
        path = Path(explicit_repo_path).resolve()
        if not path.is_dir():
            raise RepoNotConfiguredError(f"repo_path does not exist or is not a directory: {path}")
        return path
    return find_repo_root(start_dir)
