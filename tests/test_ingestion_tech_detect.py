"""Tests for codewalk.ingestion.tech_detect."""

from __future__ import annotations

from pathlib import Path

from codewalk.ingestion.tech_detect import detect_tech_stack


def test_empty_repo_detects_nothing(tmp_path: Path) -> None:
    assert detect_tech_stack(tmp_path) == []


def test_nonexistent_repo_returns_empty_list(tmp_path: Path) -> None:
    assert detect_tech_stack(tmp_path / "does_not_exist") == []


def test_detects_single_python_manifest(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    assert detect_tech_stack(tmp_path) == ["python"]


def test_dedupes_multiple_manifests_for_same_tech(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_tech_stack(tmp_path) == ["python"]


def test_detects_multiple_distinct_technologies(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")

    result = detect_tech_stack(tmp_path)

    assert result == sorted({"python", "javascript/node", "go"})


def test_result_is_sorted(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")
    result = detect_tech_stack(tmp_path)
    assert result == sorted(result)
