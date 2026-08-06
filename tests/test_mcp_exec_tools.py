"""Tests for mcp.exec_tools: subprocess-based static analysis and test running."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codewalk.codewalk_config import CodewalkConfig
from codewalk.mcp import exec_tools
from codewalk.mcp.exec_tools import (
    language_for_path,
    languages_in,
    run_static_analysis,
    run_tests,
)


def test_language_for_path_known_and_unknown() -> None:
    assert language_for_path("a.py") == "python"
    assert language_for_path("a.rb") == "ruby"
    assert language_for_path("a.unknownext") is None


def test_languages_in_dedupes_preserving_order() -> None:
    assert languages_in(["a.py", "b.py", "c.go", "d.py"]) == ["python", "go"]


def test_languages_in_empty_list() -> None:
    assert languages_in([]) == []


def test_run_static_analysis_command_not_installed_is_not_an_error(tmp_path: Path) -> None:
    config = CodewalkConfig(tools={"static_analysis": {"python": ["this-tool-does-not-exist"]}})
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    results = run_static_analysis(tmp_path, ["a.py"], config)
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].skipped_reason is not None
    assert "not installed" in results[0].skipped_reason


def test_run_static_analysis_default_python_command(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    results = run_static_analysis(tmp_path, ["a.py"])
    assert len(results) == 1
    assert "ruff" in results[0].command


def test_run_static_analysis_no_files_falls_back_to_python(tmp_path: Path) -> None:
    results = run_static_analysis(tmp_path, [])
    assert len(results) == 1
    assert "ruff" in results[0].command


def test_run_static_analysis_unconfigured_language_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.rb").write_text("puts 1\n", encoding="utf-8")
    results = run_static_analysis(tmp_path, ["a.rb"])
    assert results == []


def test_run_static_analysis_uses_configured_command_override(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    config = CodewalkConfig(tools={"static_analysis": {"python": ["echo", "custom-lint"]}})
    results = run_static_analysis(tmp_path, ["a.py"], config)
    assert len(results) == 1
    assert "custom-lint" in results[0].command
    assert results[0].ok is True


def test_run_tests_no_command_for_unconfigured_language_returns_none(tmp_path: Path) -> None:
    assert run_tests(tmp_path, ["a.rb"]) is None


def test_run_tests_default_python_command(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_tests(tmp_path, ["test_a.py"])
    assert result is not None
    assert "pytest" in result.command


def test_run_tests_uses_configured_command_override(tmp_path: Path) -> None:
    config = CodewalkConfig(tools={"test_command": {"python": ["echo", "custom-test"]}})
    result = run_tests(tmp_path, ["a.py"], config)
    assert result is not None
    assert "custom-test" in result.command
    assert result.ok is True


def test_run_tests_no_files_defaults_to_python(tmp_path: Path) -> None:
    result = run_tests(tmp_path, [])
    assert result is not None
    assert "pytest" in result.command


def test_run_command_timeout_reported_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

    monkeypatch.setattr(exec_tools.subprocess, "run", _raise_timeout)
    result = exec_tools._run_command(tmp_path, ["sleep", "999"], [])
    assert result.ok is False
    assert result.skipped_reason is not None
    assert "timed out" in result.skipped_reason
