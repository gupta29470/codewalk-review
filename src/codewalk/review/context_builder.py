"""Builds the single unified per-batch review context returned to the host LLM.

Pure text assembly: given a batch of diff files plus already-computed risk
annotations, neighborhood snippets, rubrics, and guidelines, produces one
Markdown string. Does no graph or git I/O itself -- callers (batching /
the review engine) are responsible for computing those inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codewalk.review.diff_parser import DiffFile
from codewalk.review.neighborhood import NeighborhoodResult
from codewalk.review.report import Finding
from codewalk.review.rubric_loader import Rubrics
from codewalk.review.static_analysis import StaticAnalysisResult

_DEFAULT_FILE_TOKEN_CAP = 10_000
_DEFAULT_CONTEXT_LINES = 50
_CHARS_PER_TOKEN = 3
_MAX_PREVIOUS_FINDINGS = 50
_MAX_NEIGHBORHOOD_SNIPPETS_SHOWN = 10

REVIEW_INSTRUCTIONS = """# Code Review

Use the repository context, rubrics, and risk annotations below to find
concrete, actionable issues introduced or worsened by this diff.

Do not praise. Do not flag style nits unless they indicate a real bug. Only
flag issues caused or worsened by the current diff. Provide a concrete fix
for every issue you report.

## Severity
- **blocker**: security vulnerability, crash, data loss, race condition, breaking API
  change, PII exposure
- **error**: logic error, missing edge case, unsafe pattern, type issue, untested
  new business logic
- **suggestion**: readability, naming, minor consistency

Call `codewalk_submit_batch_findings` with your findings for this batch."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~3 chars/token for code). No external tokenizer dependency."""
    if not text:
        return 0
    return len(text) // _CHARS_PER_TOKEN


def _extract_import_block(lines: list[str]) -> list[str]:
    """Leading import/using/require statements, kept verbatim in truncated output."""
    imports: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            imports.append(line)
            continue
        if stripped.startswith(("import ", "from ", "using ", "require", "#include", "use ")):
            imports.append(line)
        else:
            break
    return imports


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def smart_truncate_file_content(
    content: str,
    hunks: list[Any],
    max_tokens: int = _DEFAULT_FILE_TOKEN_CAP,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> str:
    """Truncate file content around diff hunks, keeping import blocks and hunk context.

    Collapses large untouched sections with an "N lines omitted" marker
    instead of dropping them silently.
    """
    if not content:
        return ""
    if not hunks:
        return content[: max_tokens * _CHARS_PER_TOKEN]

    lines = content.splitlines()
    total_lines = len(lines)

    ranges: list[tuple[int, int]] = []
    for hunk in hunks:
        start = max(0, hunk.start_line - 1)
        end = min(total_lines - 1, max(start, hunk.end_line - 1))
        ranges.append((max(0, start - context_lines), min(total_lines - 1, end + context_lines)))

    merged = _merge_ranges(ranges)

    import_lines = _extract_import_block(lines)
    if import_lines and (not merged or len(import_lines) - 1 < merged[0][0]):
        merged.insert(0, (0, len(import_lines) - 1))

    parts: list[str] = []
    prev_end = -1
    for start, end in merged:
        if start > prev_end + 1:
            parts.append(f"\n... [{start - prev_end - 1} lines omitted] ...\n")
        parts.extend(lines[start : end + 1])
        prev_end = end
    if prev_end < total_lines - 1:
        parts.append(f"\n... [{total_lines - 1 - prev_end} lines omitted] ...")

    truncated = "\n".join(parts)

    if estimate_tokens(truncated) > max_tokens and context_lines > 5:
        return smart_truncate_file_content(
            content, hunks, max_tokens=max_tokens, context_lines=context_lines // 2
        )
    return truncated


def _read_file_content(repo_root: Path, file_path: str) -> str:
    full_path = repo_root / file_path
    if not full_path.exists():
        return ""
    try:
        return full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _format_hunks(diff_file: DiffFile) -> str:
    lines: list[str] = []
    for hunk in diff_file.hunks:
        header = (
            f"@@ -{hunk.source_start},{hunk.source_length} +{hunk.start_line},{len(hunk.lines)} @@"
        )
        lines.append(header)
        prefix_by_type = {"added": "+", "removed": "-", "context": " "}
        for line in hunk.lines:
            prefix = prefix_by_type.get(line.change_type, " ")
            lines.append(f"{prefix}{line.content}")
    return "\n".join(lines)


def _format_rubrics(rubrics: Rubrics) -> str:
    parts: list[str] = ["## Review Rubric"]
    if rubrics.core:
        parts.append(rubrics.core)
    lang_parts = [text for _, text in sorted(rubrics.language.items())]
    if lang_parts:
        parts.append("\n".join(lang_parts))
    if rubrics.framework:
        parts.append(rubrics.framework)
    if rubrics.fallback:
        parts.append(rubrics.fallback)
    return "\n\n".join(parts)


def _format_previous_findings(
    batch: list[DiffFile],
    previous_findings: list[Finding],
    neighborhood: NeighborhoodResult | None,
) -> str:
    if not previous_findings:
        return ""

    relevant_files = {df.file_path for df in batch}
    if neighborhood:
        relevant_files.update(s.file_path for s in neighborhood.snippets)

    matched = [f for f in previous_findings if f.file_path in relevant_files]
    matched = matched[:_MAX_PREVIOUS_FINDINGS]
    if not matched:
        return ""

    lines = ["## Previous review findings (for context only)", ""]
    for f in matched:
        line = f"- [{f.severity.value}] {f.file_path}"
        if f.line_number:
            line += f":{f.line_number}"
        if f.title:
            line += f" — {f.title}"
        lines.append(line)

    lines.append("")
    lines.append(
        "These issues were flagged in an earlier review of related files. Do not "
        "blindly repeat them. Only report one again if it is still valid and caused "
        'or worsened by the current diff, and set its `status` to "still_present" '
        'instead of "new".'
    )
    return "\n".join(lines)


def build_batch_context(
    repo_root: Path,
    batch: list[DiffFile],
    static_result: StaticAnalysisResult,
    rubrics: Rubrics,
    stack_header: str = "",
    guidelines: str = "",
    user_prompt: str = "",
    previous_findings: list[Finding] | None = None,
    neighborhood: NeighborhoodResult | None = None,
    file_token_cap: int = _DEFAULT_FILE_TOKEN_CAP,
) -> str:
    """Build one review context string for a batch of changed files.

    Contains (in order): review instructions, stack context, guidelines,
    rubrics, team prompt, previous findings, per-file content + diff +
    risk annotation, and neighborhood snippets.
    """
    parts: list[str] = [REVIEW_INSTRUCTIONS]

    if stack_header:
        parts.append(stack_header)

    if guidelines:
        parts.append(
            "These code guidelines define this repository's standards. Enforce them "
            "fully, but use your broader engineering judgment for anything they don't cover."
        )
        parts.append(f"## Code guidelines\n\n{guidelines}")

    parts.append(_format_rubrics(rubrics))

    if user_prompt:
        parts.append(f"## Team-specific instructions\n\n{user_prompt}")

    previous_findings_text = _format_previous_findings(batch, previous_findings or [], neighborhood)
    if previous_findings_text:
        parts.append(previous_findings_text)

    for df in batch:
        risk = static_result.risk_annotations.get(df.file_path)
        parts.append(f"### {df.file_path} (+{df.added_lines}/-{df.removed_lines})")
        if risk and risk.to_prompt_text():
            parts.append(f"> {risk.to_prompt_text()}")
        parts.append("")

        content = _read_file_content(repo_root, df.file_path)
        if content:
            truncated = smart_truncate_file_content(content, df.hunks, max_tokens=file_token_cap)
            parts.append(f"```\n{truncated}\n```")
        else:
            parts.append("*(file deleted or not found)*")

        parts.append("\n**Diff:**")
        parts.append(f"```diff\n{_format_hunks(df)}\n```")
        parts.append("")

    if neighborhood and neighborhood.snippets:
        parts.append("## Neighborhood Context (callers, tests, interfaces)\n")
        for snippet in neighborhood.snippets[:_MAX_NEIGHBORHOOD_SNIPPETS_SHOWN]:
            parts.append(f"**{snippet.source}:** `{snippet.file_path}`")
            parts.append(f"```\n{snippet.content}\n```")
            parts.append("")

    return "\n".join(parts)
