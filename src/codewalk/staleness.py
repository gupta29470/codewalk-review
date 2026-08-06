"""Staleness detection: graph snapshot vs. repo, and this install vs. GitHub.

Two distinct, unrelated checks live here:

1. Graph-snapshot staleness -- the graph DB is a snapshot of repo structure
   taken at the last build/refresh. Query tools read that snapshot for speed
   (never re-parsing on every call); this answers "has the repo changed since
   then?" cheaply (a single `git rev-parse HEAD`, no rescan) so tool output
   can carry a soft warning rather than silently serving stale structural data.

2. GitHub-behind-remote staleness -- checks whether this codewalk *install's*
   local HEAD is behind its configured upstream on GitHub (`git fetch` +
   `rev-list --count`), and prepends a banner to MCP tool output when it is.
   Deliberately local-only: no server, no tokens, no index versioning --
   unlike upstream's cloud staleness system, which needs a hosted index and a
   `/version` endpoint this repo doesn't have.
"""

from __future__ import annotations

import functools
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codewalk.ingestion.scanner import ScannedFile
from codewalk.log import get_logger
from codewalk.paths import fingerprint_path

logger = get_logger(__name__)

_GIT_HEAD_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RepoFingerprint:
    """A lightweight snapshot marker recorded at build/refresh time."""

    git_head: str | None
    file_count: int
    total_size_bytes: int


def current_git_head(repo_root: Path | str) -> str | None:
    """Current `git rev-parse HEAD`, or None if not a git repo / no commits yet.

    Never raises: missing git binary, not a repo, no commits, or a timeout all
    degrade to None (staleness becomes "unknown", not a false positive).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_HEAD_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def compute_fingerprint(repo_root: Path | str, files: list[ScannedFile]) -> RepoFingerprint:
    """Compute a fingerprint from already-scanned files (no extra filesystem I/O
    beyond a single, cheap `git rev-parse HEAD` call)."""
    return RepoFingerprint(
        git_head=current_git_head(repo_root),
        file_count=len(files),
        total_size_bytes=sum(f.size_bytes for f in files),
    )


def is_stale(fingerprint: RepoFingerprint, repo_root: Path | str) -> bool:
    """True if the repo's current git HEAD differs from the fingerprint's.

    If either side has no git HEAD (not a git repo, or no commits at build
    time or now), staleness is unknown -- this returns False rather than a
    false positive.
    """
    current = current_git_head(repo_root)
    if fingerprint.git_head is None or current is None:
        return False
    return current != fingerprint.git_head


def format_staleness_warning(fingerprint: RepoFingerprint, repo_root: Path | str) -> str | None:
    """A one-line warning if stale, else None."""
    current = current_git_head(repo_root)
    if fingerprint.git_head is None or current is None or current == fingerprint.git_head:
        return None
    built_short = fingerprint.git_head[:7]
    current_short = current[:7]
    return (
        f"\u26a0\ufe0f graph snapshot is behind HEAD (built at {built_short}, now at "
        f"{current_short}) -- run codewalk_refresh_analysis for up-to-date structural data."
    )


def save_fingerprint(repo_root: Path | str, fingerprint: RepoFingerprint) -> None:
    """Persist `fingerprint` to `.codewalk/fingerprint.json` atomically."""
    path = fingerprint_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(asdict(fingerprint)), encoding="utf-8")
    tmp_path.replace(path)


def load_fingerprint(repo_root: Path | str) -> RepoFingerprint | None:
    """Load a previously-saved fingerprint. Never raises: returns None if
    missing, unreadable, or malformed."""
    path = fingerprint_path(repo_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RepoFingerprint(
            git_head=data["git_head"],
            file_count=data["file_count"],
            total_size_bytes=data["total_size_bytes"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("could not load fingerprint at %s: %s", path, exc)
        return None


# ─── GitHub-behind-remote staleness (this install, not the target repo) ───

_GITHUB_CACHE_TTL_SEC = 300  # avoid a `git fetch` network round-trip on every tool call
_GITHUB_GIT_TIMEOUT_SEC = 5

_github_cache: tuple[float, dict[str, Any] | None] | None = None


def _install_root() -> Path | None:
    """Repo root of this codewalk install (where the running code lives)."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "src" / "codewalk").is_dir():
        return candidate
    return None


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GITHUB_GIT_TIMEOUT_SEC,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _check_behind_github(root: Path | None = None) -> dict[str, Any] | None:
    if root is None:
        root = _install_root()
    if root is None or not (root / ".git").exists():
        return None

    # Current branch's upstream, e.g. "origin/master". Skip silently for a
    # detached HEAD or a branch with no configured upstream.
    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    if not upstream:
        return None

    local_sha = _run_git(["rev-parse", "HEAD"], root)
    if not local_sha:
        return None

    # The only network call here -- must never hang or crash the MCP process.
    if _run_git(["fetch", "--quiet"], root) is None:
        return None

    remote_sha = _run_git(["rev-parse", upstream], root)
    if not remote_sha or remote_sha == local_sha:
        return None

    behind_raw = _run_git(["rev-list", "--count", f"HEAD..{upstream}"], root)
    behind_count = int(behind_raw) if behind_raw and behind_raw.isdigit() else 0
    if not behind_count:
        return None

    return {
        "upstream": upstream,
        "local_sha": local_sha[:7],
        "remote_sha": remote_sha[:7],
        "behind_count": behind_count,
    }


def _cached_github_check() -> dict[str, Any] | None:
    global _github_cache
    now = time.monotonic()
    if _github_cache is not None and (now - _github_cache[0]) < _GITHUB_CACHE_TTL_SEC:
        return _github_cache[1]
    try:
        value = _check_behind_github()
    except Exception:
        value = None
    _github_cache = (now, value)
    return value


def github_staleness_banner() -> str:
    """One-line banner if this install is behind its GitHub remote, else ''."""
    status = _cached_github_check()
    if not status:
        return ""
    plural = "s" if status["behind_count"] != 1 else ""
    return (
        f"\U0001f195 This codewalk install is {status['behind_count']} commit{plural} behind "
        f"`{status['upstream']}` on GitHub ({status['local_sha']} \u2192 {status['remote_sha']}). "
        f"Run `git pull` in the codewalk install directory, then restart the MCP server."
    )


def _wrap_tool_fn(fn: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(fn, "_codewalk_github_staleness_wrapped", False):
        return fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if isinstance(result, str):
            banner = github_staleness_banner()
            if banner:
                return f"{banner}\n\n---\n\n{result}"
        return result

    wrapper._codewalk_github_staleness_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def install_github_staleness_wrappers(tool_manager: Any) -> None:
    """Wrap every registered MCP tool to prepend a GitHub-behind banner when stale."""
    for tool in tool_manager.list_tools():
        tool.fn = _wrap_tool_fn(tool.fn)
