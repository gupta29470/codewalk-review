"""Tests for codewalk.graph.call_extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.errors import ParseError
from codewalk.graph.call_extractor import extract_calls_batch, extract_calls_from_file
from tests.conftest import write_repo_files


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestExtractCallsFromFile:
    def test_module_level_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "greet()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert len(calls) == 1
        assert calls[0].caller == "mod.py:<module>"
        assert calls[0].callee_name == "greet"
        assert calls[0].line == 1

    def test_call_inside_function_scope(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "def run():\n    helper()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert calls[0].caller == "mod.py:run"
        assert calls[0].callee_name == "helper"

    def test_method_call_on_object(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "def run():\n    obj.method()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert calls[0].callee_name == "method"

    def test_self_recursive_call_by_short_name_is_excluded(self, tmp_path: Path) -> None:
        """Known limitation (matches upstream): a function calling itself by
        its own short name is filtered out as noise, not recorded as an edge."""
        path = _write(tmp_path, "mod.py", "def run():\n    run()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert calls == []

    def test_duplicate_call_site_deduplicated(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "def run():\n    if True:\n        helper()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert len(calls) == 1

    def test_unsupported_language_returns_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "data.yaml", "key: value\n")
        assert extract_calls_from_file(path, "yaml") == []

    def test_unreadable_file_raises_parse_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.py"
        with pytest.raises(ParseError):
            extract_calls_from_file(missing, "python")

    def test_javascript_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.js", "function run() {\n  helper();\n}\n")
        calls = extract_calls_from_file(path, "javascript", identifier_path="mod.js")
        assert any(c.callee_name == "helper" for c in calls)

    def test_go_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.go", "package mod\n\nfunc Run() {\n\tHelper()\n}\n")
        calls = extract_calls_from_file(path, "go", identifier_path="mod.go")
        assert any(c.callee_name == "Helper" for c in calls)

    def test_class_scope_call_outside_methods(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "class C:\n    x = helper()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert calls[0].caller == "mod.py:C"

    def test_unsupported_call_grammar_language_returns_empty(self, tmp_path: Path) -> None:
        # "go" has NODE_TYPES but let's use a language truly absent from both maps.
        path = _write(tmp_path, "mod.txt", "whatever")
        assert extract_calls_from_file(path, "made_up_language") == []

    def test_ruby_method_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.rb", "def run\n  helper()\nend\n")
        calls = extract_calls_from_file(path, "ruby", identifier_path="mod.rb")
        assert any(c.callee_name == "helper" for c in calls)

    def test_rust_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.rs", "fn run() {\n    helper();\n}\n")
        calls = extract_calls_from_file(path, "rust", identifier_path="mod.rs")
        assert any(c.callee_name == "helper" for c in calls)

    def test_csharp_call(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "Mod.cs",
            "public class Mod {\n    public void Run() {\n        Helper();\n    }\n}\n",
        )
        calls = extract_calls_from_file(path, "csharp", identifier_path="Mod.cs")
        assert any(c.callee_name == "Helper" for c in calls)

    def test_kotlin_member_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.kt", "fun run() {\n    obj.helper()\n}\n")
        calls = extract_calls_from_file(path, "kotlin", identifier_path="mod.kt")
        assert any(c.callee_name == "helper" for c in calls)

    def test_swift_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.swift", "func run() {\n    helper()\n}\n")
        calls = extract_calls_from_file(path, "swift", identifier_path="mod.swift")
        assert any(c.callee_name == "helper" for c in calls)

    def test_c_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.c", "void run() {\n    helper();\n}\n")
        calls = extract_calls_from_file(path, "c", identifier_path="mod.c")
        assert any(c.callee_name == "helper" for c in calls)

    def test_php_call(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.php", "<?php\nfunction run() {\n    helper();\n}\n")
        calls = extract_calls_from_file(path, "php", identifier_path="mod.php")
        assert any(c.callee_name == "helper" for c in calls)

    def test_call_with_no_extractable_callee_is_skipped(self, tmp_path: Path) -> None:
        # A call whose "function" expression is itself a complex sub-expression
        # (an immediately-invoked call result) has no simple identifier name.
        path = _write(tmp_path, "mod.py", "def run():\n    (lambda: None)()\n")
        calls = extract_calls_from_file(path, "python", identifier_path="mod.py")
        assert calls == []


class TestExtractCallsBatch:
    def test_extracts_across_multiple_files(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        calls = extract_calls_batch(files)
        assert any(c.callee_name == "helper" and "main.py" in c.caller for c in calls)

    def test_skips_language_without_grammar(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"data.yaml": "key: value\n"})
        assert extract_calls_batch(files) == []

    def test_unreadable_file_is_skipped_not_raised(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"main.py": "helper()\n"})
        files[0].absolute_path.unlink()
        assert extract_calls_batch(files) == []

    def test_empty_file_list(self) -> None:
        assert extract_calls_batch([]) == []
