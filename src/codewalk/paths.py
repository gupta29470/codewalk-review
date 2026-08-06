"""Path-traversal guards and `.codewalk/` directory layout helpers.

Every MCP tool argument that accepts a file path must be routed through
`resolve_within_repo()` before it touches disk.
"""

from __future__ import annotations

from pathlib import Path

from codewalk.errors import PathTraversalError

CODEWALK_DIR_NAME = ".codewalk"


def resolve_within_repo(repo_root: Path | str, candidate: Path | str) -> Path:
    """Resolve `candidate` against `repo_root` and guarantee it stays inside it.

    `candidate` may be relative (joined onto `repo_root`) or absolute (used
    as-is). Resolution follows symlinks via `Path.resolve()` before the
    containment check, so a symlink inside the repo that points outside it
    is caught too.

    Args:
        repo_root: The repo root that `candidate` must stay within.
        candidate: The path to validate, relative or absolute.

    Returns:
        The resolved, guaranteed-in-repo path.

    Raises:
        PathTraversalError: if the resolved path is not `repo_root` or a
            descendant of it.
    """
    root = Path(repo_root).resolve()
    candidate_path = Path(candidate)
    target = candidate_path if candidate_path.is_absolute() else root / candidate_path
    resolved = target.resolve()

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathTraversalError(
            f"Path '{candidate}' resolves to '{resolved}', which is outside the repo root '{root}'."
        ) from exc
    return resolved


def codewalk_dir(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk` (not guaranteed to exist)."""
    return Path(repo_root).resolve() / CODEWALK_DIR_NAME


def ensure_codewalk_dir(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk`, creating it (and parents) if missing."""
    directory = codewalk_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def graph_db_path(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk/graph.duckdb`."""
    return codewalk_dir(repo_root) / "graph.duckdb"


def fingerprint_path(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk/fingerprint.json` (build-time staleness marker)."""
    return codewalk_dir(repo_root) / "fingerprint.json"


def stack_context_path(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk/stack_context.json`."""
    return codewalk_dir(repo_root) / "stack_context.json"


def review_session_dir(repo_root: Path | str, session_slug: str) -> Path:
    """Return `<repo_root>/.codewalk/review_session/<session_slug>`."""
    return codewalk_dir(repo_root) / "review_session" / session_slug


def rubrics_override_dir(repo_root: Path | str) -> Path:
    """Return `<repo_root>/.codewalk/rubrics` (team rubric overrides)."""
    return codewalk_dir(repo_root) / "rubrics"
