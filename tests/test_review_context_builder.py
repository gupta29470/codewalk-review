"""Tests for review.context_builder: per-batch review context assembly."""

from __future__ import annotations

from pathlib import Path

from codewalk.review.context_builder import (
    build_batch_context,
    estimate_tokens,
    smart_truncate_file_content,
)
from codewalk.review.neighborhood import NeighborhoodResult, NeighborhoodSnippet
from codewalk.review.report import Category, Confidence, Finding, Severity, Source
from codewalk.review.rubric_loader import Rubrics
from codewalk.review.static_analysis import run_static_analysis
from tests.conftest import make_diff_file


def test_estimate_tokens_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_roughly_chars_over_three() -> None:
    assert estimate_tokens("abcdefghi") == 3


def test_smart_truncate_no_hunks_falls_back_to_head_truncation() -> None:
    content = "x" * 1000
    truncated = smart_truncate_file_content(content, [], max_tokens=10)
    assert len(truncated) == 30


def test_smart_truncate_empty_content_returns_empty() -> None:
    assert smart_truncate_file_content("", [], max_tokens=100) == ""


def test_smart_truncate_keeps_hunk_context_and_omits_rest() -> None:
    lines = [f"line{i}" for i in range(200)]
    content = "\n".join(lines)
    diff_file = make_diff_file("big.py", added=["line100"], start_line=101)
    truncated = smart_truncate_file_content(
        content, diff_file.hunks, max_tokens=10_000, context_lines=5
    )
    assert "line100" in truncated
    assert "lines omitted" in truncated


def test_smart_truncate_keeps_import_block() -> None:
    lines = ["import os", "import sys", ""] + [f"x{i} = {i}" for i in range(200)]
    content = "\n".join(lines)
    diff_file = make_diff_file("big.py", added=["x150 = 999"], start_line=151)
    truncated = smart_truncate_file_content(
        content, diff_file.hunks, max_tokens=10_000, context_lines=5
    )
    assert "import os" in truncated
    assert "import sys" in truncated


def _finding(file_path: str = "a.py", title: str = "Old bug") -> Finding:
    return Finding(
        severity=Severity.ERROR,
        category=Category.BUG,
        file_path=file_path,
        line_number=3,
        title=title,
        explanation="explanation",
        confidence=Confidence.HIGH,
        source=Source.LLM,
    )


def test_build_batch_context_includes_diff_and_rubrics(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")

    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)
    rubrics = Rubrics(core="Core rules.", fallback="Fallback rules.")

    context = build_batch_context(root, diff_files, static_result, rubrics)
    assert "a.py" in context
    assert "Core rules." in context
    assert "```diff" in context
    assert "codewalk_submit_batch_findings" in context


def test_build_batch_context_missing_file_shows_placeholder(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    diff_files = [make_diff_file("missing.py", added=["x = 1"])]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(root, diff_files, static_result, Rubrics())
    assert "*(file deleted or not found)*" in context


def test_build_batch_context_includes_stack_header_and_guidelines(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(
        root,
        diff_files,
        static_result,
        Rubrics(),
        stack_header="## Repository Architecture Context\n- **Languages:** python",
        guidelines="Always use type hints.",
        user_prompt="Focus on security.",
    )
    assert "Repository Architecture Context" in context
    assert "Always use type hints." in context
    assert "Focus on security." in context


def test_build_batch_context_includes_risk_annotation_text(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"] * 60)]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(root, diff_files, static_result, Rubrics())
    # A 60-line-added lone file has a fan-in capped at 50, likely triggering high-fan-in.
    annotation = static_result.risk_annotations["a.py"]
    if annotation.to_prompt_text():
        assert annotation.to_prompt_text() in context


def test_build_batch_context_previous_findings_filtered_to_relevant_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)

    previous = [
        _finding(file_path="a.py", title="Relevant"),
        _finding(file_path="z.py", title="Irrelevant"),
    ]
    context = build_batch_context(
        root, diff_files, static_result, Rubrics(), previous_findings=previous
    )
    assert "Relevant" in context
    assert "Irrelevant" not in context


def test_build_batch_context_no_previous_findings_omits_section(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(root, diff_files, static_result, Rubrics())
    assert "Previous review findings" not in context


def test_build_batch_context_includes_neighborhood_snippets(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)

    neighborhood = NeighborhoodResult(
        snippets=[NeighborhoodSnippet(file_path="caller.py", content="call_a()", source="caller")]
    )
    context = build_batch_context(
        root, diff_files, static_result, Rubrics(), neighborhood=neighborhood
    )
    assert "Neighborhood Context" in context
    assert "caller.py" in context


def test_build_batch_context_empty_neighborhood_omits_section(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"])]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(
        root, diff_files, static_result, Rubrics(), neighborhood=NeighborhoodResult()
    )
    assert "Neighborhood Context" not in context


def test_build_batch_context_respects_file_token_cap(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n" * 5000, encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 2"], start_line=2500)]
    static_result = run_static_analysis(diff_files)

    context = build_batch_context(root, diff_files, static_result, Rubrics(), file_token_cap=50)
    assert "lines omitted" in context
