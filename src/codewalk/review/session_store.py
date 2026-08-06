"""Persistence for internal review sessions.

Sessions are stored under::

    <repo_root>/.codewalk/review_session/<folder_name>/

Each session folder contains:

- ``session.json`` -- session metadata (see ``review.session.ReviewSession``)
- ``llm_findings.json`` / ``llm_findings.md`` -- host-LLM-submitted findings
  (JSON is the source of truth; the ``.md`` file is a read-only human-readable
  companion)
- ``static_findings.json`` / ``static_findings.md`` -- deterministic findings
  from static analysis

Plus a top-level ``.codewalk/review_session/index.json`` mapping
``session_id -> folder_name`` for O(1) lookup. All writes are atomic (see
``codewalk.atomic_io.write_json_atomic``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from codewalk.atomic_io import write_json_atomic
from codewalk.log import get_logger
from codewalk.paths import codewalk_dir, review_session_dir
from codewalk.review.renderers.markdown import render_findings_markdown
from codewalk.review.report import Finding
from codewalk.review.session import ReviewSession

logger = get_logger(__name__)

_SESSIONS_SUBDIR = "review_session"


def _sessions_root(repo_root: Path) -> Path:
    return codewalk_dir(repo_root) / _SESSIONS_SUBDIR


def _session_dir(repo_root: Path, folder_name: str) -> Path:
    return review_session_dir(repo_root, folder_name)


def _index_path(repo_root: Path) -> Path:
    return _sessions_root(repo_root) / "index.json"


def _session_folders(repo_root: Path) -> list[Path]:
    root = _sessions_root(repo_root)
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def _load_index(repo_root: Path) -> dict[str, str]:
    index_path = _index_path(repo_root)
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("session index at %s is corrupted, ignoring", index_path)
        return {}
    return data if isinstance(data, dict) else {}


def _update_index(repo_root: Path, session_id: str, folder_name: str) -> None:
    index = _load_index(repo_root)
    index[session_id] = folder_name
    write_json_atomic(_index_path(repo_root), index)


def save_session(session: ReviewSession) -> None:
    """Persist session metadata atomically and update the lookup index."""
    folder_name = session.folder_name or session.session_id
    session_dir = _session_dir(Path(session.repo_path), folder_name)
    write_json_atomic(session_dir / "session.json", session.to_dict())
    _update_index(Path(session.repo_path), session.session_id, folder_name)


def load_session_by_folder(repo_root: Path, folder_name: str) -> ReviewSession | None:
    """Load a persisted session by its descriptive folder name."""
    session_path = _session_dir(repo_root, folder_name) / "session.json"
    if not session_path.exists():
        return None
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("session.json at %s is corrupted", session_path)
        return None
    data.setdefault("folder_name", folder_name)
    try:
        return ReviewSession.from_dict(data)
    except (KeyError, ValueError):
        logger.warning("session.json at %s has an invalid shape", session_path)
        return None


def load_session(repo_root: Path, session_id: str) -> ReviewSession | None:
    """Load a persisted session by its stable session_id.

    Uses the index for O(1) lookup, falling back to a linear scan of all
    session folders if the index is missing, stale, or corrupted.
    """
    index = _load_index(repo_root)
    folder_name = index.get(session_id)
    if folder_name:
        session = load_session_by_folder(repo_root, folder_name)
        if session is not None and session.session_id == session_id:
            return session

    for folder in _session_folders(repo_root):
        session = load_session_by_folder(repo_root, folder.name)
        if session is not None and session.session_id == session_id:
            return session
    return None


def find_last_session(repo_root: Path, branch: str | None = None) -> ReviewSession | None:
    """Find the most recently modified session, optionally filtered by branch."""
    folders = sorted(_session_folders(repo_root), key=lambda p: p.stat().st_mtime, reverse=True)
    for folder in folders:
        session = load_session_by_folder(repo_root, folder.name)
        if session is None:
            continue
        if branch is None or session.current_branch == branch or session.target_branch == branch:
            return session
    return None


def list_sessions(repo_root: Path) -> list[str]:
    """List descriptive folder names of persisted sessions, most recent first."""
    folders = sorted(_session_folders(repo_root), key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.name for f in folders]


def delete_session(repo_root: Path, session_id: str) -> bool:
    """Delete a persisted session's folder. Returns False if it did not exist."""
    session = load_session(repo_root, session_id)
    if session is None:
        return False
    session_dir = _session_dir(repo_root, session.folder_name or session.session_id)
    if not session_dir.exists():
        return False
    shutil.rmtree(session_dir)
    return True


def _save_findings_file(
    repo_root: Path,
    folder_name: str,
    file_stem: str,
    title: str,
    source_label: str,
    findings: list[Finding],
) -> None:
    session_dir = _session_dir(repo_root, folder_name)
    write_json_atomic(session_dir / f"{file_stem}.json", [f.to_dict() for f in findings])
    md_path = session_dir / f"{file_stem}.md"
    md_path.write_text(
        render_findings_markdown(findings, title=title, source_label=source_label),
        encoding="utf-8",
    )


def _load_findings_file(repo_root: Path, folder_name: str, file_stem: str) -> list[Finding]:
    findings_path = _session_dir(repo_root, folder_name) / f"{file_stem}.json"
    if not findings_path.exists():
        return []
    try:
        raw = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("%s is corrupted, treating as empty", findings_path)
        return []
    findings: list[Finding] = []
    for item in raw:
        try:
            findings.append(Finding.model_validate(item))
        except ValueError:
            logger.warning("skipping corrupted finding entry in %s", findings_path)
    return findings


def save_findings(repo_root: Path, folder_name: str, findings: list[Finding]) -> None:
    """Persist/overwrite the host-LLM findings for a session (+ Markdown companion)."""
    _save_findings_file(
        repo_root, folder_name, "llm_findings", "LLM Findings", "review LLM", findings
    )


def load_findings(repo_root: Path, folder_name: str) -> list[Finding]:
    """Load the host-LLM findings for a session."""
    return _load_findings_file(repo_root, folder_name, "llm_findings")


def append_findings(
    repo_root: Path, folder_name: str, new_findings: list[Finding]
) -> list[Finding]:
    """Append findings to a session's llm_findings.json and return the merged list."""
    merged = load_findings(repo_root, folder_name) + new_findings
    save_findings(repo_root, folder_name, merged)
    return merged


def save_static_findings(repo_root: Path, folder_name: str, findings: list[Finding]) -> None:
    """Persist/overwrite the deterministic findings for a session (+ Markdown companion)."""
    _save_findings_file(
        repo_root, folder_name, "static_findings", "Static Findings", "static analysis", findings
    )


def load_static_findings(repo_root: Path, folder_name: str) -> list[Finding]:
    """Load the deterministic findings for a session."""
    return _load_findings_file(repo_root, folder_name, "static_findings")
