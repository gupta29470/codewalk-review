"""Tests for review.renderers.markdown: findings -> human-readable Markdown."""

from __future__ import annotations

from codewalk.review.renderers.markdown import render_findings_markdown
from codewalk.review.report import Category, Finding, Severity


def _finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": Severity.ERROR,
        "category": Category.BUG,
        "file_path": "src/app.py",
        "line_number": 12,
        "title": "Something is wrong",
        "explanation": "This is broken.",
    }
    defaults.update(overrides)
    return Finding(**defaults)


def test_render_empty_findings_list() -> None:
    md = render_findings_markdown([])
    assert "_No findings._" in md


def test_render_includes_title_and_source_label() -> None:
    md = render_findings_markdown([], title="My Findings", source_label="static analysis")
    assert "# My Findings" in md
    assert "**Source:** static analysis" in md


def test_render_no_source_label_omits_source_line() -> None:
    md = render_findings_markdown([])
    assert "**Source:**" not in md


def test_render_finding_includes_metadata() -> None:
    md = render_findings_markdown([_finding()])
    assert "src/app.py:12" in md
    assert "**Category:** bug" in md
    assert "**Confidence:** high" in md
    assert "**Source:** llm" in md


def test_render_finding_with_subcategory_overrides_category_label() -> None:
    md = render_findings_markdown([_finding(subcategory="sql-injection")])
    assert "**Category:** sql-injection" in md


def test_render_finding_no_line_number() -> None:
    md = render_findings_markdown([_finding(line_number=None)])
    assert "src/app.py`" in md


def test_render_finding_blocking_and_status_and_verdict() -> None:
    f = _finding(blocking=True, status="still_present", user_verdict="accepted")
    md = render_findings_markdown([f])
    assert "**Blocking:** true" in md
    assert "**Status:** still_present" in md
    assert "**Verdict:** accepted" in md


def test_render_finding_default_status_and_no_verdict_omitted() -> None:
    md = render_findings_markdown([_finding()])
    assert "**Status:**" not in md
    assert "**Verdict:**" not in md
    assert "**Blocking:**" not in md


def test_render_finding_multi_paragraph_explanation_wrapped() -> None:
    long_explanation = ("word " * 30).strip() + "\n\nSecond paragraph here."
    md = render_findings_markdown([_finding(explanation=long_explanation)])
    assert "Second paragraph here." in md


def test_render_finding_current_and_recommended_code_blocks() -> None:
    f = _finding(current_code="x = 1\n", recommended_code="x = 2\n")
    md = render_findings_markdown([f])
    assert "### Current code" in md
    assert "### Recommended code" in md
    assert "```python" in md
    assert "x = 1" in md
    assert "x = 2" in md


def test_render_finding_no_code_omits_code_blocks() -> None:
    md = render_findings_markdown([_finding()])
    assert "### Current code" not in md
    assert "### Recommended code" not in md


def test_render_finding_unknown_extension_uses_no_language_tag() -> None:
    f = _finding(file_path="README", current_code="some text")
    md = render_findings_markdown([f])
    assert "```\n" in md


def test_render_multiple_findings_numbered() -> None:
    findings = [_finding(title="First"), _finding(title="Second", file_path="b.py")]
    md = render_findings_markdown(findings)
    assert "## 1. [error] First" in md
    assert "## 2. [error] Second" in md


def test_render_finding_no_evidence_omits_evidence_section() -> None:
    md = render_findings_markdown([_finding()])
    assert "### Evidence" not in md


def test_render_finding_evidence_with_snippet_and_metadata() -> None:
    evidence = [{"file": "src/other.py", "line": 5, "snippet": "def helper():\n    pass\n"}]
    md = render_findings_markdown([_finding(evidence=evidence)])
    assert "### Evidence" in md
    assert "**file:** src/other.py" in md
    assert "**line:** 5" in md
    assert "def helper():" in md


def test_render_finding_evidence_without_snippet_shows_only_metadata() -> None:
    evidence = [{"note": "no code available"}]
    md = render_findings_markdown([_finding(evidence=evidence)])
    assert "### Evidence" in md
    assert "**note:** no code available" in md
    assert "```" not in md


def test_render_finding_evidence_multiple_items() -> None:
    evidence = [
        {"snippet": "first_snippet()"},
        {"snippet": "second_snippet()"},
    ]
    md = render_findings_markdown([_finding(evidence=evidence)])
    assert "first_snippet()" in md
    assert "second_snippet()" in md


def test_render_finding_evidence_non_dict_items_are_skipped() -> None:
    # Finding.evidence is typed list[dict[str, Any]] (pydantic rejects non-dict
    # items at construction time), but _render_evidence defends against it too
    # in case a Finding is ever built by bypassing validation (e.g. model_construct).
    from codewalk.review.renderers.markdown import _render_evidence

    lines = _render_evidence(["not-a-dict", 42], "python")
    assert lines[0] == "### Evidence"
    # No content rendered for the bad items -- just the header + blank line.
    assert not any("not-a-dict" in line or "42" in line for line in lines)


def test_render_finding_no_verifier_notes_omits_section() -> None:
    md = render_findings_markdown([_finding()])
    assert "### Verifier notes" not in md


def test_render_finding_verifier_notes_rendered() -> None:
    md = render_findings_markdown([_finding(verifier_notes="Confirmed against test output.")])
    assert "### Verifier notes" in md
    assert "Confirmed against test output." in md


def test_render_finding_evidence_and_verifier_notes_together() -> None:
    f = _finding(
        evidence=[{"snippet": "x = 1"}],
        verifier_notes="Reproduced locally.",
    )
    md = render_findings_markdown([f])
    assert "### Evidence" in md
    assert "### Verifier notes" in md
    # Evidence must appear before verifier notes, matching source order.
    assert md.index("### Evidence") < md.index("### Verifier notes")
