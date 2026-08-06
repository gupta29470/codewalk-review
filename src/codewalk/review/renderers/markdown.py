"""Render findings as human-readable Markdown.

This is a read-only companion to the machine-readable ``llm_findings.json`` /
``static_findings.json`` files written by ``session_store.py`` -- JSON stays
the source of truth.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from codewalk.review.report import Finding

_WRAP_WIDTH = 88

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sql": "sql",
}


def _wrap_text(text: str | None, width: int = _WRAP_WIDTH) -> str:
    """Hard-wrap text for readable raw Markdown, preserving paragraph breaks."""
    if not text:
        return ""
    wrapped = []
    for para in text.split("\n"):
        wrapped.append(textwrap.fill(para, width=width) if para.strip() else "")
    return "\n".join(wrapped)


def _language_for_file(file_path: str) -> str:
    return _LANGUAGE_BY_SUFFIX.get(Path(file_path).suffix.lower(), "")


def _finding_meta_line(f: Finding) -> str:
    loc = f"`{f.file_path}"
    if f.line_number:
        loc += f":{f.line_number}"
    loc += "`"

    meta_parts = [
        f"**ID:** `{f.id}`",
        f"**File:** {loc}",
        f"**Category:** {f.subcategory or f.category.value}",
        f"**Confidence:** {f.confidence.value}",
        f"**Source:** {f.source.value}",
    ]
    if f.status != "new":
        meta_parts.append(f"**Status:** {f.status}")
    if f.blocking:
        meta_parts.append("**Blocking:** true")
    if f.user_verdict:
        meta_parts.append(f"**Verdict:** {f.user_verdict}")
    return " · ".join(meta_parts)


def _code_block_lines(heading: str, code: str | None, language: str) -> list[str]:
    if not code:
        return []
    return [f"### {heading}", f"```{language}", code.rstrip("\n"), "```", ""]


def _render_evidence(evidence: list[dict[str, object]], language: str) -> list[str]:
    if not evidence:
        return []
    lines = ["### Evidence", ""]
    for item in evidence:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet")
        meta = {k: v for k, v in item.items() if k != "snippet"}
        if meta:
            lines.append(" · ".join(f"**{k}:** {v}" for k, v in meta.items()))
        if snippet:
            lines.extend([f"```{language}", str(snippet).rstrip("\n"), "```"])
        lines.append("")
    return lines


def _render_finding(idx: int, f: Finding) -> list[str]:
    lines = [f"## {idx}. [{f.severity.value}] {f.title}", "", _finding_meta_line(f), ""]

    explanation = _wrap_text(f.explanation)
    if explanation:
        lines.extend([explanation, ""])

    lang = _language_for_file(f.file_path)
    lines.extend(_code_block_lines("Current code", f.current_code, lang))
    lines.extend(_code_block_lines("Recommended code", f.recommended_code, lang))
    lines.extend(_render_evidence(f.evidence, lang))

    verifier_notes = _wrap_text(f.verifier_notes)
    if verifier_notes:
        lines.extend(["### Verifier notes", "", verifier_notes, ""])

    return lines


def render_findings_markdown(
    findings: list[Finding],
    title: str = "Review Findings",
    source_label: str = "",
) -> str:
    """Render findings as a hard-wrapped Markdown document."""
    lines: list[str] = [f"# {title}", ""]
    if source_label:
        lines.extend([f"**Source:** {source_label}", ""])

    if not findings:
        lines.extend(["_No findings._", ""])
        return "\n".join(lines)

    for idx, f in enumerate(findings, start=1):
        lines.extend(_render_finding(idx, f))

    return "\n".join(lines)
