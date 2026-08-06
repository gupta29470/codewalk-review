"""Tests for review.report: Finding validation and ID computation, ReviewReport."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codewalk.review.report import (
    ArchitectureFlags,
    Category,
    Confidence,
    Finding,
    ReviewReport,
    Severity,
    Source,
)


def _make_finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": Severity.ERROR,
        "category": Category.BUG,
        "file_path": "src/app.py",
        "line_number": 10,
        "title": "Off-by-one error",
        "explanation": "The loop bound is wrong.",
    }
    defaults.update(overrides)
    return Finding(**defaults)  # type: ignore[arg-type]


def test_finding_id_is_auto_computed_and_stable() -> None:
    f1 = _make_finding()
    f2 = _make_finding()
    assert f1.id
    assert f1.id == f2.id


def test_finding_id_differs_for_different_file_path() -> None:
    f1 = _make_finding(file_path="src/app.py")
    f2 = _make_finding(file_path="src/other.py")
    assert f1.id != f2.id


def test_finding_id_differs_for_different_category() -> None:
    f1 = _make_finding(category=Category.BUG)
    f2 = _make_finding(category=Category.SECURITY)
    assert f1.id != f2.id


def test_finding_id_survives_line_number_shift_via_anchor() -> None:
    """Same enclosing function anchor -> same id even if line_number changes."""
    f1 = _make_finding(
        line_number=10,
        current_code="def compute_total(items):\n    return sum(items)",
    )
    f2 = _make_finding(
        line_number=42,
        current_code="def compute_total(items):\n    return sum(items) + 1",
    )
    assert f1.id == f2.id


def test_finding_explicit_id_is_preserved() -> None:
    f = _make_finding(id="custom-id-123")
    assert f.id == "custom-id-123"


def test_finding_rejects_invalid_severity() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity="catastrophic",  # type: ignore[arg-type]
            category=Category.BUG,
            file_path="a.py",
            line_number=1,
            title="t",
            explanation="e",
        )


def test_finding_rejects_invalid_category() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity=Severity.ERROR,
            category="nonsense",  # type: ignore[arg-type]
            file_path="a.py",
            line_number=1,
            title="t",
            explanation="e",
        )


def test_finding_rejects_negative_line_number() -> None:
    with pytest.raises(ValidationError):
        _make_finding(line_number=-1)


def test_finding_allows_none_line_number() -> None:
    f = _make_finding(line_number=None)
    assert f.line_number is None


def test_finding_allows_zero_line_number() -> None:
    f = _make_finding(line_number=0)
    assert f.line_number == 0


def test_finding_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        _make_finding(title="   ")


def test_finding_rejects_blank_explanation() -> None:
    with pytest.raises(ValidationError):
        _make_finding(explanation="")


def test_finding_rejects_blank_file_path() -> None:
    with pytest.raises(ValidationError):
        _make_finding(file_path="")


def test_finding_rejects_unknown_extra_field() -> None:
    with pytest.raises(ValidationError):
        Finding(
            severity=Severity.ERROR,
            category=Category.BUG,
            file_path="a.py",
            line_number=1,
            title="t",
            explanation="e",
            made_up_field="nope",  # type: ignore[call-arg]
        )


def test_finding_defaults() -> None:
    f = _make_finding()
    assert f.confidence == Confidence.HIGH
    assert f.source == Source.LLM
    assert f.blocking is False
    assert f.status == "new"
    assert f.user_verdict is None


def test_finding_to_dict_shape() -> None:
    f = _make_finding()
    d = f.to_dict()
    assert d["severity"] == "error"
    assert d["category"] == "bug"
    assert d["id"] == f.id
    assert "line_number" in d


def test_finding_model_validate_round_trip() -> None:
    f = _make_finding()
    d = f.to_dict()
    restored = Finding.model_validate(d)
    assert restored.id == f.id
    assert restored.severity == f.severity


def test_architecture_flags_to_dict() -> None:
    flags = ArchitectureFlags(bottlenecks_touched=["a.py"], cycles_touched=["b.py", "c.py"])
    d = flags.to_dict()
    assert d == {"bottlenecks_touched": ["a.py"], "cycles_touched": ["b.py", "c.py"]}


def test_review_report_to_dict_shape() -> None:
    finding = _make_finding()
    report = ReviewReport(
        findings=[finding],
        deterministic_findings=[],
        architecture_flags=ArchitectureFlags(),
        files_reviewed=1,
        lines_added=5,
        lines_removed=2,
        session_id="abc",
        folder_name="1-Jan-2026-main",
    )
    d = report.to_dict()
    assert d["issues"] == [finding.to_dict()]
    assert d["static_issues"] == []
    assert d["files_reviewed"] == 1
    assert d["session_id"] == "abc"


def test_review_report_defaults_are_empty() -> None:
    report = ReviewReport()
    assert report.findings == []
    assert report.deterministic_findings == []
    assert report.files_reviewed == 0
