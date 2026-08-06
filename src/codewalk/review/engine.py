"""Review orchestration engine: session lifecycle, batching, and findings.

The only orchestration layer for review -- no internal LLM calls, no
`reviewers/` package, no file-apply step. Given a repo and diff options, this
produces one ready-to-return Markdown context per batch for the host LLM to
review, and persists whatever findings the host submits via MCP. Everything
here is testable without the MCP layer at all.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from codewalk.atomic_io import write_json_atomic
from codewalk.errors import InvalidFindingError, SessionNotFoundError
from codewalk.paths import review_session_dir
from codewalk.review import session_store
from codewalk.review.batching import estimate_shared_context_tokens, make_batches
from codewalk.review.context_builder import build_batch_context
from codewalk.review.diff_parser import DiffFile, get_diff, get_parsed_diff
from codewalk.review.neighborhood import expand_neighborhood
from codewalk.review.report import Category, Confidence, Finding, Severity, Source
from codewalk.review.rubric_loader import Rubrics, build_rubrics
from codewalk.review.session import ReviewSession, SessionStatus
from codewalk.review.stack_detect import (
    fallback_detect_stack,
    format_stack_context_header,
    get_rubric_names_from_stack,
    load_cached_stack_context,
)
from codewalk.review.static_analysis import StaticAnalysisResult, run_static_analysis
from codewalk.workspace import Workspace

_BATCH_STATE_FILENAME = "batch_state.json"
_DEFAULT_MAX_TOKENS_PER_BATCH = 50_000
# Generous slack for line-number sanity checks: newly added lines can push a
# finding's line number past the on-disk file's current length in edge cases
# (e.g. the file was edited again after the diff was captured).
_LINE_NUMBER_SLACK = 200


# ─── Session naming helpers ──────────────────────────────────────────


def _sanitize_branch_name(branch: str | None) -> str:
    """Make a git branch name safe for use in a directory name."""
    if not branch:
        return "none"
    sanitized = re.sub(r'[\\/:*?"<>|]+', "-", branch)
    sanitized = re.sub(r"-+", "-", sanitized)
    return sanitized.strip("-").strip("_") or "none"


def _build_session_folder_name(
    created_at: datetime, current_branch: str | None, target_branch: str | None
) -> str:
    """Build a descriptive, filesystem-safe session folder name.

    Format: DD-Month-YYYY-HHMMSS-<current_branch>[-to-<target_branch>].
    """
    date_part = created_at.strftime("%d-%B-%Y-%H%M%S")
    parts = [date_part, _sanitize_branch_name(current_branch)]
    if target_branch:
        parts.extend(["to", _sanitize_branch_name(target_branch)])
    return "-".join(parts)


def _current_branch(repo_root: Path) -> str | None:
    """Best-effort current git branch name; None if it can't be determined."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ─── Batch state persistence (engine-internal, not part of report.py's schema) ──


@dataclass
class BatchState:
    """Engine-internal bookkeeping needed to resume `next_batch()` across calls."""

    session_id: str
    target_branch: str | None
    commit: str | None
    staged: bool
    total_files: int
    batch_queue: list[list[str]]
    stack: dict[str, Any]
    rubric_core: str = ""
    rubric_language: dict[str, str] = field(default_factory=dict)
    rubric_framework: str = ""
    rubric_fallback: str = ""
    current_batch_index: int = 0
    batch_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    previous_session_id: str | None = None
    rejected_ids: list[str] = field(default_factory=list)

    @property
    def total_batches(self) -> int:
        return len(self.batch_queue)

    def rubrics(self) -> Rubrics:
        return Rubrics(
            core=self.rubric_core,
            language=self.rubric_language,
            framework=self.rubric_framework,
            fallback=self.rubric_fallback,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_branch": self.target_branch,
            "commit": self.commit,
            "staged": self.staged,
            "total_files": self.total_files,
            "batch_queue": self.batch_queue,
            "stack": self.stack,
            "rubric_core": self.rubric_core,
            "rubric_language": self.rubric_language,
            "rubric_framework": self.rubric_framework,
            "rubric_fallback": self.rubric_fallback,
            "current_batch_index": self.current_batch_index,
            "batch_outcomes": self.batch_outcomes,
            "previous_session_id": self.previous_session_id,
            "rejected_ids": self.rejected_ids,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BatchState:
        return BatchState(
            session_id=data["session_id"],
            target_branch=data.get("target_branch"),
            commit=data.get("commit"),
            staged=data.get("staged", False),
            total_files=data.get("total_files", 0),
            batch_queue=data.get("batch_queue", []),
            stack=data.get("stack", {}),
            rubric_core=data.get("rubric_core", ""),
            rubric_language=data.get("rubric_language", {}),
            rubric_framework=data.get("rubric_framework", ""),
            rubric_fallback=data.get("rubric_fallback", ""),
            current_batch_index=data.get("current_batch_index", 0),
            batch_outcomes=data.get("batch_outcomes", {}),
            previous_session_id=data.get("previous_session_id"),
            rejected_ids=data.get("rejected_ids", []),
        )


def _batch_state_path(repo_root: Path, folder_name: str) -> Path:
    return review_session_dir(repo_root, folder_name) / _BATCH_STATE_FILENAME


def _save_batch_state(repo_root: Path, folder_name: str, state: BatchState) -> None:
    write_json_atomic(_batch_state_path(repo_root, folder_name), state.to_dict())


def _load_batch_state(repo_root: Path, folder_name: str) -> BatchState | None:
    path = _batch_state_path(repo_root, folder_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return BatchState.from_dict(data)


def _require_session(repo_root: Path, session_id: str) -> tuple[ReviewSession, BatchState]:
    session = session_store.load_session(repo_root, session_id)
    if session is None:
        raise SessionNotFoundError(f"no review session found for id '{session_id}'")
    folder = session.folder_name or session.session_id
    batch_state = _load_batch_state(repo_root, folder)
    if batch_state is None:
        raise SessionNotFoundError(f"review session '{session_id}' has no batch state")
    return session, batch_state


# ─── Deterministic (static) findings ─────────────────────────────────


def _build_static_findings(static_result: StaticAnalysisResult) -> list[Finding]:
    """Convert high-impact risk annotations into deterministic findings."""
    findings: list[Finding] = []
    for file_path, ann in static_result.risk_annotations.items():
        is_notable = (
            ann.is_high_fan_in
            or ann.is_high_pagerank
            or ann.is_bottleneck
            or ann.cycle_participation
        )
        if not is_notable:
            continue

        signals: list[str] = []
        if ann.is_high_fan_in:
            signals.append(f"{ann.fan_in} direct callers/affected files")
        if ann.is_high_pagerank:
            signals.append(f"PageRank {ann.pagerank:.4f}")
        if ann.is_bottleneck:
            signals.append("architectural bottleneck")
        if ann.cycle_participation:
            signals.append("circular dependency participant")

        explanation = (
            f"`{file_path}` scores highly on dependency-graph risk: {', '.join(signals)}. "
            "Changes here can have broad downstream effects -- ensure tests "
            "and callers are covered."
        )
        findings.append(
            Finding(
                severity=Severity.ERROR
                if (ann.is_bottleneck or ann.cycle_participation)
                else Severity.SUGGESTION,
                category=Category.ARCHITECTURE,
                file_path=file_path,
                line_number=None,
                title=f"High-impact file changed: {file_path}",
                explanation=explanation,
                confidence=Confidence.HIGH,
                source=Source.DETERMINISTIC,
            )
        )
    return findings


# ─── Result types returned to callers (MCP layer formats these) ─────


@dataclass
class BatchResult:
    """One batch's ready-to-return review context."""

    batch_index: int  # 0-based
    total_batches: int
    file_paths: list[str]
    context: str


@dataclass
class ReviewStartResult:
    """Result of `start_review()` / `re_review()`."""

    has_changes: bool
    session: ReviewSession | None = None
    total_files: int = 0
    total_batches: int = 0
    stack: dict[str, Any] = field(default_factory=dict)
    first_batch: BatchResult | None = None
    rejected_count: int = 0


@dataclass
class SubmitResult:
    """Result of `submit_findings()`."""

    batch_number: int
    saved_count: int
    running_total: int


@dataclass
class ReviewSummary:
    """Result of `get_summary()`."""

    session_id: str
    total_files: int
    total_batches: int
    static_findings: list[Finding]
    llm_findings: list[Finding]
    rejected_filtered_count: int
    batch_outcomes: dict[str, dict[str, Any]]


@dataclass
class ReviewDetails:
    """Result of `get_review_details()`."""

    session: ReviewSession
    batch_state: BatchState | None
    static_findings_count: int
    llm_findings_count: int


# ─── Internal helpers shared by start_review/next_batch ─────────────


def _resolve_workspace(repo_root: Path, workspace: Workspace | None) -> Workspace:
    return workspace if workspace is not None else Workspace.open_or_build(repo_root)


def _resolve_stack(repo_root: Path, changed_files: list[str]) -> dict[str, Any]:
    """Cached stack context wins; otherwise an in-memory (not persisted) fallback."""
    cached = load_cached_stack_context(repo_root)
    if cached is not None:
        return cached
    return fallback_detect_stack(repo_root, changed_files)


def _build_batch_result(
    repo_root: Path,
    workspace: Workspace,
    static_result: StaticAnalysisResult,
    rubrics: Rubrics,
    stack_header: str,
    batch_index: int,
    total_batches: int,
    batch_diff_files: list[DiffFile],
) -> BatchResult:
    neighborhood = expand_neighborhood(
        repo_root,
        batch_diff_files,
        graph_store=workspace.graph_store,
        deep=len(batch_diff_files) == 1,
    )
    context = build_batch_context(
        repo_root,
        batch_diff_files,
        static_result,
        rubrics,
        stack_header=stack_header,
        neighborhood=neighborhood,
    )
    return BatchResult(
        batch_index=batch_index,
        total_batches=total_batches,
        file_paths=[df.file_path for df in batch_diff_files],
        context=context,
    )


def _fetch_diff_files(
    repo_root: Path, target_branch: str | None, commit: str | None, staged: bool
) -> list[DiffFile]:
    diff_text = get_diff(
        repo_path=str(repo_root), target_branch=target_branch, commit=commit, staged=staged
    )
    diff_files = get_parsed_diff(diff_text)
    # `.codewalk/` holds our own graph DB and session state -- never treat it
    # as part of the user's changes, even when it's untracked in their repo.
    return [df for df in diff_files if not df.file_path.startswith(".codewalk/")]


# ─── Public orchestration API ─────────────────────────────────────────


def start_review(
    repo_root: Path,
    target_branch: str | None = None,
    staged: bool = False,
    commit: str | None = None,
    workspace: Workspace | None = None,
    max_tokens_per_batch: int = _DEFAULT_MAX_TOKENS_PER_BATCH,
) -> ReviewStartResult:
    """Start a new review session: diff, batch, persist, and build batch 1's context.

    Returns `has_changes=False` (not an error) when there is nothing to review.
    """
    diff_files = _fetch_diff_files(repo_root, target_branch, commit, staged)
    if not diff_files:
        return ReviewStartResult(has_changes=False)

    ws = _resolve_workspace(repo_root, workspace)
    changed_paths = [df.file_path for df in diff_files]

    stack = _resolve_stack(repo_root, changed_paths)
    stack_header = format_stack_context_header(stack)
    rubric_names = get_rubric_names_from_stack(stack)
    rubrics = build_rubrics(repo_root, changed_paths, detected_rubric_names=rubric_names)

    static_result = run_static_analysis(
        diff_files, graph_runtime=ws.graph_runtime, graph_store=ws.graph_store
    )
    static_findings = _build_static_findings(static_result)

    # Every batch repeats the stack header and rubric text, so it counts
    # against the per-batch token budget just like file content does.
    base_tokens = estimate_shared_context_tokens(
        stack_header, rubrics.core, rubrics.framework, rubrics.fallback, *rubrics.language.values()
    )
    batches = make_batches(
        diff_files, max_tokens_per_batch=max_tokens_per_batch, base_tokens=base_tokens
    )

    current_branch = _current_branch(repo_root)
    created_at = datetime.now(timezone.utc)
    folder_name = _build_session_folder_name(created_at, current_branch, target_branch)
    session_id = ReviewSession.generate_id()

    session = ReviewSession(
        session_id=session_id,
        repo_path=str(repo_root),
        target_branch=target_branch,
        commit=commit,
        staged=staged,
        status=SessionStatus.ACTIVE,
        folder_name=folder_name,
        current_branch=current_branch,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
    )
    session_store.save_session(session)
    session_store.save_static_findings(repo_root, folder_name, static_findings)
    session_store.save_findings(repo_root, folder_name, [])

    batch_state = BatchState(
        session_id=session_id,
        target_branch=target_branch,
        commit=commit,
        staged=staged,
        total_files=len(diff_files),
        batch_queue=[[df.file_path for df in batch] for batch in batches],
        stack=stack,
        rubric_core=rubrics.core,
        rubric_language=rubrics.language,
        rubric_framework=rubrics.framework,
        rubric_fallback=rubrics.fallback,
        current_batch_index=0,
    )
    _save_batch_state(repo_root, folder_name, batch_state)

    first_batch = _build_batch_result(
        repo_root, ws, static_result, rubrics, stack_header, 0, len(batches), batches[0]
    )

    return ReviewStartResult(
        has_changes=True,
        session=session,
        total_files=len(diff_files),
        total_batches=len(batches),
        stack=stack,
        first_batch=first_batch,
    )


def next_batch(
    repo_root: Path, session_id: str, workspace: Workspace | None = None
) -> BatchResult | None:
    """Return the next batch's context, or None if every batch has already been returned.

    Raises:
        SessionNotFoundError: `session_id` doesn't correspond to a persisted session.
    """
    session, batch_state = _require_session(repo_root, session_id)
    next_index = batch_state.current_batch_index + 1
    if next_index >= batch_state.total_batches:
        return None

    ws = _resolve_workspace(repo_root, workspace)
    diff_files = _fetch_diff_files(
        repo_root, batch_state.target_branch, batch_state.commit, batch_state.staged
    )
    wanted_paths = set(batch_state.batch_queue[next_index])
    batch_diff_files = [df for df in diff_files if df.file_path in wanted_paths]

    static_result = run_static_analysis(
        diff_files, graph_runtime=ws.graph_runtime, graph_store=ws.graph_store
    )
    rubrics = batch_state.rubrics()
    stack_header = format_stack_context_header(batch_state.stack)

    result = _build_batch_result(
        repo_root,
        ws,
        static_result,
        rubrics,
        stack_header,
        next_index,
        batch_state.total_batches,
        batch_diff_files,
    )

    batch_state.current_batch_index = next_index
    folder = session.folder_name or session.session_id
    _save_batch_state(repo_root, folder, batch_state)
    return result


def _validate_submitted_findings(
    findings: list[dict[str, Any]], allowed_file_paths: set[str]
) -> list[Finding]:
    validated: list[Finding] = []
    for raw in findings:
        try:
            finding = Finding.model_validate(raw)
        except ValidationError as exc:
            raise InvalidFindingError(f"invalid finding: {exc}") from exc

        if allowed_file_paths and finding.file_path not in allowed_file_paths:
            raise InvalidFindingError(
                f"finding references '{finding.file_path}', which is not part of "
                "the batch that was just reviewed"
            )
        validated.append(finding)
    return validated


def _check_line_number_bounds(repo_root: Path, finding: Finding) -> None:
    """Best-effort sanity check: reject line numbers far beyond the file's length."""
    if finding.line_number is None:
        return
    full_path = repo_root / finding.file_path
    if not full_path.exists():
        return
    try:
        line_count = full_path.read_text(encoding="utf-8").count("\n") + 1
    except (OSError, UnicodeDecodeError):
        return
    if finding.line_number > line_count + _LINE_NUMBER_SLACK:
        raise InvalidFindingError(
            f"finding for '{finding.file_path}' references line {finding.line_number}, "
            f"but the file only has {line_count} lines"
        )


def _dedupe_by_id(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    deduped: list[Finding] = []
    for finding in findings:
        if finding.id in seen:
            continue
        seen.add(finding.id)
        deduped.append(finding)
    return deduped


def submit_findings(
    repo_root: Path,
    session_id: str,
    findings: list[dict[str, Any]],
    notes: str = "",
) -> SubmitResult:
    """Validate and persist findings for the batch most recently returned.

    An empty `findings` list is valid ("no issues found") and does not
    require `notes`. Raises `InvalidFindingError` for schema violations, an
    out-of-batch `file_path` (possible host hallucination), or a wildly
    out-of-range `line_number`.

    Raises:
        SessionNotFoundError: `session_id` doesn't correspond to a persisted session.
        InvalidFindingError: a submitted finding fails validation.
    """
    session, batch_state = _require_session(repo_root, session_id)
    folder = session.folder_name or session.session_id

    current_index = batch_state.current_batch_index
    allowed_paths: set[str] = set()
    if batch_state.batch_queue:
        allowed_paths = set(batch_state.batch_queue[current_index])

    validated = _validate_submitted_findings(findings, allowed_paths)
    for finding in validated:
        _check_line_number_bounds(repo_root, finding)
    validated = _dedupe_by_id(validated)

    merged = session_store.append_findings(repo_root, folder, validated)

    batch_number = current_index + 1
    batch_state.batch_outcomes[str(batch_number)] = {
        "outcome": "findings" if validated else "clean",
        "count": len(validated),
        "notes": notes.strip(),
    }
    _save_batch_state(repo_root, folder, batch_state)

    return SubmitResult(
        batch_number=batch_number, saved_count=len(validated), running_total=len(merged)
    )


def get_summary(repo_root: Path, session_id: str) -> ReviewSummary:
    """Combine static + LLM findings for a session, filtering out rejected re-review carry-overs.

    Raises:
        SessionNotFoundError: `session_id` doesn't correspond to a persisted session.
    """
    session, batch_state = _require_session(repo_root, session_id)
    folder = session.folder_name or session.session_id

    static_findings = session_store.load_static_findings(repo_root, folder)
    llm_findings = session_store.load_findings(repo_root, folder)

    rejected_ids = set(batch_state.rejected_ids)
    filtered_count = 0
    if rejected_ids:
        before = len(llm_findings)
        llm_findings = [f for f in llm_findings if f.id not in rejected_ids]
        filtered_count = before - len(llm_findings)

    return ReviewSummary(
        session_id=session_id,
        total_files=batch_state.total_files,
        total_batches=batch_state.total_batches,
        static_findings=static_findings,
        llm_findings=llm_findings,
        rejected_filtered_count=filtered_count,
        batch_outcomes=batch_state.batch_outcomes,
    )


def re_review(
    repo_root: Path,
    target_branch: str | None = None,
    staged: bool = False,
    commit: str | None = None,
    workspace: Workspace | None = None,
    max_tokens_per_batch: int = _DEFAULT_MAX_TOKENS_PER_BATCH,
) -> ReviewStartResult:
    """Start a fresh review session, hiding findings rejected in the last one.

    Raises:
        SessionNotFoundError: no previous session exists for `target_branch`.
    """
    previous = session_store.find_last_session(repo_root, target_branch)
    if previous is None:
        raise SessionNotFoundError(
            f"no previous review session found for branch '{target_branch or 'current'}'"
        )

    previous_folder = previous.folder_name or previous.session_id
    previous_findings = session_store.load_findings(repo_root, previous_folder)
    rejected_ids = sorted({f.id for f in previous_findings if f.user_verdict == "rejected"})

    result = start_review(
        repo_root,
        target_branch=target_branch,
        staged=staged,
        commit=commit,
        workspace=workspace,
        max_tokens_per_batch=max_tokens_per_batch,
    )
    if not result.has_changes or result.session is None:
        return result

    folder = result.session.folder_name or result.session.session_id
    batch_state = _load_batch_state(repo_root, folder)
    if batch_state is not None:
        batch_state.previous_session_id = previous.session_id
        batch_state.rejected_ids = rejected_ids
        _save_batch_state(repo_root, folder, batch_state)

    result.rejected_count = len(rejected_ids)
    return result


def get_review_details(repo_root: Path, session_id: str) -> ReviewDetails:
    """Return a session's persisted metadata + batch/finding counts for introspection.

    Raises:
        SessionNotFoundError: `session_id` doesn't correspond to a persisted session.
    """
    session, batch_state = _require_session(repo_root, session_id)
    folder = session.folder_name or session.session_id
    static_count = len(session_store.load_static_findings(repo_root, folder))
    llm_count = len(session_store.load_findings(repo_root, folder))
    return ReviewDetails(
        session=session,
        batch_state=batch_state,
        static_findings_count=static_count,
        llm_findings_count=llm_count,
    )
