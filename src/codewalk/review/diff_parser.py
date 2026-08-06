"""Git diff generation and parsing for the review engine.

``get_diff`` shells out to git to produce raw unified-diff text (staged,
unstaged, untracked, branch-relative, or a specific commit). ``get_parsed_diff``
turns that text into structured ``DiffFile``/``DiffHunk``/``ChangedLine``
objects using ``unidiff``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from codewalk.errors import InvalidDiffError
from codewalk.ingestion.scanner import detect_language
from codewalk.log import get_logger
from codewalk.review.target import resolve_diff_target_branch

if TYPE_CHECKING:
    from unidiff.patch import Hunk, Line, PatchedFile

logger = get_logger(__name__)

_MAX_UNTRACKED_FILE_SIZE = 1024 * 1024  # 1MB
_BINARY_CHECK_BYTES = 8192  # first 8KB
_GIT_TIMEOUT_SECONDS = 60


@dataclass
class ChangedLine:
    """A single line from a unified diff."""

    line_number: int
    content: str
    change_type: str  # "added" | "removed" | "context"


@dataclass
class DiffHunk:
    """One ``@@...@@`` block -- a contiguous section of changes within a file."""

    start_line: int
    end_line: int
    lines: list[ChangedLine] = field(default_factory=list)
    source_start: int = 0
    source_length: int = 0


@dataclass
class DiffFile:
    """One changed file in the diff."""

    file_path: str
    language: str
    hunks: list[DiffHunk] = field(default_factory=list)
    is_new_file: bool = False
    is_deleted: bool = False
    added_lines: int = 0
    removed_lines: int = 0


def _run_git(
    args: list[str],
    repo_path: str | None,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise InvalidDiffError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidDiffError(f"git command timed out: git {' '.join(args)}") from exc


def _has_head(repo_path: str | None) -> bool:
    """Return True if the repo has at least one commit (HEAD exists)."""
    result = _run_git(["rev-parse", "--verify", "HEAD"], repo_path, timeout=10)
    return result.returncode == 0


def _merge_base(repo_path: str | None, target: str) -> str | None:
    """Return the merge-base of HEAD and target, or None if unavailable."""
    result = _run_git(["merge-base", target, "HEAD"], repo_path, timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def _read_untracked_file_lines(full_path: Path) -> list[str] | None:
    """Return the file's lines if it is eligible for a synthetic diff, else None.

    Skips symlinks, non-files, files over the size limit, binary content, and
    files that fail UTF-8 decode.
    """
    if full_path.is_symlink() or not full_path.is_file():
        return None

    try:
        size = full_path.stat().st_size
    except OSError:
        return None
    if size > _MAX_UNTRACKED_FILE_SIZE:
        return None

    try:
        with full_path.open("rb") as fh:
            head = fh.read(_BINARY_CHECK_BYTES)
    except OSError:
        return None
    if b"\x00" in head:
        return None

    try:
        content = full_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None

    lines = content.splitlines()
    return lines or None


def _synthetic_diff_for_file(rel_path: str, lines: list[str]) -> str:
    diff_lines = [
        f"diff --git a/{rel_path} b/{rel_path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{rel_path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    diff_lines.extend(f"+{line}" for line in lines)
    return "\n".join(diff_lines)


def _synthetic_untracked_diff(repo_path: str | None) -> str:
    """Build a synthetic unified diff for untracked (new, unstaged) files.

    Skips binary files, symlinks, files over 1MB, and files that fail UTF-8
    decode, so ``get_parsed_diff`` never has to deal with unparseable content.
    """
    result = _run_git(
        ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"],
        repo_path,
        timeout=30,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0 or not stdout.strip():
        return ""

    base = Path(repo_path) if repo_path else Path.cwd()
    parts: list[str] = []

    for raw_rel_path in stdout.strip().splitlines():
        rel_path = raw_rel_path.strip()
        if not rel_path:
            continue

        lines = _read_untracked_file_lines(base / rel_path)
        if lines is None:
            continue
        parts.append(_synthetic_diff_for_file(rel_path, lines))

    if not parts:
        return ""
    return "\n" + "\n".join(parts) + "\n"


def get_diff(
    staged: bool = False,
    target_branch: str | None = None,
    commit: str | None = None,
    since_commit: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Return raw unified diff text.

    By default returns ALL local changes: staged + unstaged + untracked
    files. Priority when multiple options are given: ``commit`` >
    ``since_commit`` > ``staged`` > ``target_branch`` > default.

    Args:
        staged: Diff only staged changes (``git diff --staged``). No
            untracked files.
        target_branch: Diff from the merge-base of this base and HEAD
            through the working tree (commits on the current branch since
            divergence, plus uncommitted changes), with untracked files
            appended. Pass ``"current"`` (or an alias) for local changes on
            this branch only (same as omitting ``target_branch``).
        commit: Diff a specific commit against its parent (or show it in
            full if it has no parent). No untracked files.
        since_commit: Diff from ``since_commit`` to the working tree, plus
            untracked files.
        repo_path: Working directory to run git in.
    """
    cmd: list[str]
    append_untracked = True
    resolved_target = resolve_diff_target_branch(target_branch)

    if commit:
        append_untracked = False
        parent_check = _run_git(["rev-parse", "--verify", f"{commit}~1"], repo_path, timeout=10)
        has_parent = parent_check.returncode == 0
        cmd = (
            ["diff", "--unified=5", f"{commit}~1", commit]
            if has_parent
            else ["show", "--format=", "-p", commit]
        )
    elif since_commit:
        cmd = ["diff", "--unified=5", since_commit]
    elif staged:
        append_untracked = False
        cmd = ["diff", "--unified=5", "--staged"]
    elif resolved_target:
        # Diff from the merge-base through the working tree: branch commits
        # since divergence + uncommitted edits, without base-tip drift.
        base = _merge_base(repo_path, resolved_target) or resolved_target
        cmd = ["diff", "--unified=5", base]
    elif _has_head(repo_path):
        cmd = ["diff", "--unified=5", "HEAD"]
    else:
        # Empty repo / no commits yet -- show staged files only.
        cmd = ["diff", "--unified=5", "--cached"]

    result = _run_git(cmd, repo_path)
    # errors="replace" so binary content in a deleted-binary-file diff can't crash decode.
    diff_output = result.stdout.decode("utf-8", errors="replace")

    if append_untracked:
        diff_output += _synthetic_untracked_diff(repo_path)

    return diff_output


def _is_binary_patched_file(patched_file: PatchedFile) -> bool:
    if patched_file.is_binary_file:
        return True
    return any("\ufffd" in line.value for hunk in patched_file for line in hunk)


def _changed_line_from(line: Line) -> ChangedLine:
    if line.is_added:
        change_type, line_no = "added", line.target_line_no
    elif line.is_removed:
        change_type, line_no = "removed", line.source_line_no
    else:
        change_type, line_no = "context", line.target_line_no
    return ChangedLine(line_number=line_no or 0, content=line.value, change_type=change_type)


def _parse_hunk(hunk: Hunk) -> DiffHunk:
    lines = [_changed_line_from(line) for line in hunk]
    return DiffHunk(
        start_line=hunk.target_start,
        end_line=hunk.target_start + hunk.target_length,
        lines=lines,
        source_start=hunk.source_start,
        source_length=hunk.source_length,
    )


def get_parsed_diff(diff_text: str) -> list[DiffFile]:
    """Parse raw unified diff text into structured ``DiffFile`` objects.

    Skips files whose content contains the Unicode replacement character
    (binary content that survived a lossy UTF-8 decode).
    """
    from unidiff import PatchSet
    from unidiff.errors import UnidiffParseError

    if not diff_text.strip():
        return []

    try:
        patch = PatchSet(diff_text)
    except UnidiffParseError as exc:
        raise InvalidDiffError(f"could not parse diff: {exc}") from exc

    diff_files: list[DiffFile] = []
    skipped: list[str] = []

    for patched_file in patch:
        if _is_binary_patched_file(patched_file):
            skipped.append(patched_file.path)
            continue

        hunks = [_parse_hunk(hunk) for hunk in patched_file]
        diff_files.append(
            DiffFile(
                file_path=patched_file.path,
                language=detect_language(Path(patched_file.path)),
                hunks=hunks,
                is_new_file=patched_file.is_added_file,
                is_deleted=patched_file.is_removed_file,
                added_lines=patched_file.added,
                removed_lines=patched_file.removed,
            )
        )

    if skipped:
        logger.info("skipped %d binary/non-UTF-8 file(s) in diff: %s", len(skipped), skipped)

    return diff_files
