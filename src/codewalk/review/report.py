"""Review finding schema and report aggregation.

``Finding`` is a pydantic model because it is the schema that
``codewalk_submit_batch_findings`` validates host-submitted JSON against --
untrusted input needs real validation (closed enums, non-negative line
numbers, non-blank text), not just type hints. ``ArchitectureFlags`` and
``ReviewReport`` are plain dataclasses: they are only ever built internally
from already-validated ``Finding`` objects, so pydantic would add no safety
here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ANCHOR_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)"),
    re.compile(r"^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)"),
    re.compile(
        r"^\s*(?:public|private|protected|static|async)?\s*"
        r"(?:function\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
    ),
    re.compile(
        r"^\s*(?:void|int|String|bool|Future|Widget|[a-zA-Z_][a-zA-Z0-9_<>,\[\]]*)"
        r"\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
    ),
)

_SHORT_WORD_RE = re.compile(r"\b[a-z_][a-z0-9_]{0,2}\b")
_DIGITS_RE = re.compile(r"\d+")


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _extract_function_or_class_anchor(snippet: str | None) -> str | None:
    """Best-effort extraction of an enclosing function/class name from a snippet."""
    if not snippet:
        return None
    for line in snippet.splitlines():
        for pattern in _ANCHOR_PATTERNS:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


class Severity(str, Enum):
    """How serious a review finding is."""

    BLOCKER = "blocker"
    ERROR = "error"
    SUGGESTION = "suggestion"


class Category(str, Enum):
    """What kind of issue was found."""

    BUG = "bug"
    SECURITY = "security"
    STYLE = "style"
    TEST = "test"
    BLAST_RADIUS = "blast_radius"
    DESIGN = "design"
    NAMING = "naming"
    COMPLEXITY = "complexity"
    ERROR_HANDLING = "error_handling"
    TYPE_SAFETY = "type_safety"
    ARCHITECTURE = "architecture"
    LOGGING = "logging"
    PRIVACY = "privacy"
    HYGIENE = "hygiene"


class Confidence(str, Enum):
    """How confident the reviewer is in a finding."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Source(str, Enum):
    """Where a finding originated."""

    DETERMINISTIC = "deterministic"
    LLM = "llm"
    VERIFICATION = "verification"


class Pillar(str, Enum):
    """Review pillar used to bucket findings for summaries."""

    TYPE_SAFETY_ARCHITECTURE = "type_safety_architecture"
    EDGE_CASES_RUNTIME_SAFETY = "edge_cases_runtime_safety"
    IDIOMS_CLEAN_CODE = "idioms_clean_code"
    TESTS_COVERAGE = "tests_coverage"
    SECURITY_BOUNDARIES = "security_boundaries"


def _semantic_anchor(finding: Finding) -> str:
    """Return a stable semantic anchor for a finding.

    Tries the enclosing function/class name first so the same bug survives
    renames and line-number shifts; falls back to a normalized title.
    """
    snippets = [finding.current_code]
    for ev in finding.evidence:
        snippet = ev.get("snippet") if isinstance(ev, dict) else None
        snippets.append(snippet)

    for snippet in snippets:
        anchor = _extract_function_or_class_anchor(snippet)
        if anchor:
            return anchor.lower()

    title = _normalize(finding.title)
    title = _SHORT_WORD_RE.sub("", title)
    title = _DIGITS_RE.sub("", title)
    return "|".join([_normalize(title), finding.file_path])


def _compute_finding_id(finding: Finding) -> str:
    """Compute a stable content-derived ID for a finding."""
    anchor = _semantic_anchor(finding)
    key = "|".join([finding.category.value, finding.file_path, anchor])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class Finding(BaseModel):
    """A single issue produced by the review pipeline.

    Validated with pydantic because this is the schema untrusted
    host-submitted JSON (``codewalk_submit_batch_findings``) is checked
    against before it is ever persisted.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: Category
    file_path: str
    line_number: int | None = Field(default=None, ge=0)
    title: str
    explanation: str
    current_code: str | None = None
    recommended_code: str | None = None
    blocking: bool = False
    confidence: Confidence = Confidence.HIGH
    source: Source = Source.LLM
    pillar: Pillar | None = None
    subcategory: str | None = None
    id: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    verifier_notes: str | None = None
    status: Literal["new", "still_present"] = "new"
    user_verdict: Literal["accepted", "rejected"] | None = None
    verdict_at: str | None = None

    @field_validator("file_path", "title", "explanation")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _ensure_id(self) -> Finding:
        if not self.id:
            self.id = _compute_finding_id(self)
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class ArchitectureFlags:
    """Architecture risk signals attached to a review (bottlenecks, cycles touched)."""

    bottlenecks_touched: list[str] = field(default_factory=list)
    cycles_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bottlenecks_touched": self.bottlenecks_touched,
            "cycles_touched": self.cycles_touched,
        }


@dataclass
class ReviewReport:
    """Aggregated findings + metadata for one review session.

    ``deterministic_findings`` come from static analysis / graph metrics;
    ``findings`` come from the host LLM's submitted findings.
    """

    findings: list[Finding] = field(default_factory=list)
    deterministic_findings: list[Finding] = field(default_factory=list)
    architecture_flags: ArchitectureFlags = field(default_factory=ArchitectureFlags)
    files_reviewed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    session_id: str = ""
    folder_name: str = ""
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issues": [f.to_dict() for f in self.findings],
            "static_issues": [f.to_dict() for f in self.deterministic_findings],
            "architecture_flags": self.architecture_flags.to_dict(),
            "files_reviewed": self.files_reviewed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "session_id": self.session_id,
            "folder_name": self.folder_name,
        }
