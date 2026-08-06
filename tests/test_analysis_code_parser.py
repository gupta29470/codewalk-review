"""Tests for codewalk.analysis.code_parser -- multi-language symbol extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.analysis.code_parser import Symbol, get_language, get_parser_for_language, parse_file
from codewalk.errors import ParseError


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestPythonParsing:
    """Python uses the stdlib `ast` module, not tree-sitter."""

    def test_function_and_class_extraction(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.py",
            "def greet(name):\n"
            "    return f'hi {name}'\n"
            "\n"
            "\n"
            "class Greeter:\n"
            "    def say(self):\n"
            "        return greet('world')\n",
        )

        symbols = parse_file(path, "python")

        functions = {s.name: s for s in symbols if s.kind == "function"}
        classes = {s.name: s for s in symbols if s.kind == "class"}
        assert "greet" in functions
        assert functions["greet"].args == ["name"]
        assert "Greeter" in classes
        assert classes["Greeter"].methods == ["say"]
        assert functions["say"].parent_class == "Greeter"
        assert functions["greet"].parent_class is None

    def test_decorators_are_captured(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.py", "@staticmethod\ndef util():\n    pass\n")
        symbols = parse_file(path, "python")
        assert symbols[0].decorators == ["staticmethod"]

    def test_base_classes_are_captured(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "mod.py", "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n"
        )
        symbols = parse_file(path, "python")
        child = next(s for s in symbols if s.name == "Child")
        assert child.bases == ["Base"]

    def test_syntax_error_raises_parse_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "broken.py", "def broken(:\n    pass\n")
        with pytest.raises(ParseError):
            parse_file(path, "python")

    def test_unreadable_file_raises_parse_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.py"
        with pytest.raises(ParseError):
            parse_file(missing, "python")

    def test_nested_function_in_method_is_a_known_limitation(self, tmp_path: Path) -> None:
        """Documented limitation: line-range attribution, not real scope analysis --
        a function nested inside a method still gets attributed as a class method."""
        path = _write(
            tmp_path,
            "mod.py",
            "class C:\n"
            "    def outer(self):\n"
            "        def inner():\n"
            "            pass\n"
            "        return inner\n",
        )
        symbols = parse_file(path, "python")
        inner = next(s for s in symbols if s.name == "inner")
        assert inner.parent_class == "C"  # known limitation, not "None"


class TestTreeSitterLanguages:
    """One representative function+class fixture per tree-sitter-backed language."""

    def test_javascript(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.js",
            "function greet(name) {\n  return name;\n}\n\nclass Greeter {\n  say() {}\n}\n",
        )
        symbols = parse_file(path, "javascript")
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["name"]
        assert any(s.kind == "class" and s.name == "Greeter" for s in symbols)

    def test_typescript(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "mod.ts", "function greet(name: string): string {\n  return name;\n}\n"
        )
        symbols = parse_file(path, "typescript")
        assert symbols[0].name == "greet"
        assert symbols[0].args == ["name"]

    def test_java(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "Mod.java",
            "public class Mod {\n"
            "    public int greet(String name) {\n"
            "        return 1;\n"
            "    }\n}\n",
        )
        symbols = parse_file(path, "java")
        assert any(s.kind == "class" and s.name == "Mod" for s in symbols)
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["name"]

    def test_go(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.go",
            "package mod\n\nfunc Greet(name string) string {\n\treturn name\n}\n",
        )
        symbols = parse_file(path, "go")
        assert symbols[0].name == "Greet"
        assert symbols[0].args == ["name"]

    def test_rust(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "mod.rs", "fn greet(name: &str) -> String {\n    name.to_string()\n}\n"
        )
        symbols = parse_file(path, "rust")
        assert symbols[0].name == "greet"
        assert symbols[0].args == ["name"]

    def test_ruby(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "mod.rb", "class Greeter\n  def greet(name)\n    name\n  end\nend\n"
        )
        symbols = parse_file(path, "ruby")
        assert any(s.kind == "class" and s.name == "Greeter" for s in symbols)
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["name"]

    def test_c(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.c", "int greet(int x) {\n    return x;\n}\n")
        symbols = parse_file(path, "c")
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["x"]

    def test_cpp(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.cpp",
            "class Greeter {\npublic:\n    int greet(int x) { return x; }\n};\n",
        )
        symbols = parse_file(path, "cpp")
        assert any(s.kind == "class" and s.name == "Greeter" for s in symbols)
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["x"]

    def test_csharp(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "Mod.cs",
            "public class Greeter {\n    public int Greet(int x) {\n        return x;\n    }\n}\n",
        )
        symbols = parse_file(path, "csharp")
        assert any(s.kind == "class" and s.name == "Greeter" for s in symbols)
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "Greet"
        assert function.args == ["x"]

    def test_php(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.php",
            "<?php\nclass Greeter {\n"
            "    function greet($name) {\n"
            "        return $name;\n"
            "    }\n}\n",
        )
        symbols = parse_file(path, "php")
        assert any(s.kind == "class" and s.name == "Greeter" for s in symbols)
        function = next(s for s in symbols if s.kind == "function")
        assert function.name == "greet"
        assert function.args == ["$name"]  # PHP variable names include the sigil

    def test_kotlin(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.kt", "fun greet(name: String): String {\n    return name\n}\n")
        symbols = parse_file(path, "kotlin")
        assert symbols[0].name == "greet"
        assert symbols[0].args == ["name"]

    def test_swift(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, "mod.swift", "func greet(name: String) -> String {\n    return name\n}\n"
        )
        symbols = parse_file(path, "swift")
        assert symbols[0].name == "greet"
        assert symbols[0].args == ["name"]


class TestGracefulDegradation:
    def test_unsupported_language_returns_empty_list(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "data.yaml", "key: value\n")
        assert parse_file(path, "yaml") == []

    def test_unknown_language_name_returns_empty_list(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "mod.xyz", "whatever")
        assert parse_file(path, "some_made_up_language") == []

    def test_malformed_tree_sitter_source_does_not_crash(self, tmp_path: Path) -> None:
        """tree-sitter is fault-tolerant: invalid syntax yields a partial/empty
        result, not an exception."""
        path = _write(tmp_path, "broken.go", "func {{{ this is not valid go\n")
        symbols = parse_file(path, "go")  # must not raise
        assert isinstance(symbols, list)

    def test_unreadable_tree_sitter_file_raises_parse_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.go"
        with pytest.raises(ParseError):
            parse_file(missing, "go")

    def test_empty_file_returns_no_symbols(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "empty.py", "")
        assert parse_file(path, "python") == []


class TestLanguageRegistry:
    def test_get_language_returns_none_for_unmapped_language(self) -> None:
        assert get_language("cobol") is None

    def test_get_language_is_cached(self) -> None:
        first = get_language("python")
        second = get_language("python")
        assert first is second  # not just equal -- same cached object

    def test_get_language_returns_none_when_grammar_import_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codewalk.analysis.code_parser as code_parser_module

        get_language.cache_clear()
        monkeypatch.setitem(code_parser_module.GRAMMAR_MAP, "made_up", "no_such_module_xyz")
        assert get_language("made_up") is None
        get_language.cache_clear()

    def test_get_parser_for_language_returns_none_when_unsupported(self) -> None:
        assert get_parser_for_language("cobol") is None

    def test_get_parser_for_language_returns_parser_for_supported_language(self) -> None:
        assert get_parser_for_language("go") is not None


class TestDecoratorsAndClassBases:
    def test_typescript_decorator_is_captured(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "mod.ts",
            "@Component\nclass Widget {\n  render() {}\n}\n",
        )
        symbols = parse_file(path, "typescript")
        widget = next(s for s in symbols if s.name == "Widget")
        assert widget.decorators == ["Component"]

    def test_java_class_bases_are_captured(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "Mod.java",
            "public class Dog extends Animal {\n    void bark() {}\n}\n",
        )
        symbols = parse_file(path, "java")
        dog = next(s for s in symbols if s.name == "Dog")
        assert "Animal" in dog.bases


class TestWalkTree:
    def test_skip_children_types_prevents_double_yield(self, tmp_path: Path) -> None:
        """A method wrapper whose type is in skip_children_types must not also
        yield its nested inner node as a separate match."""
        path = _write(tmp_path, "mod.js", "class C {\n  method() {\n    return 1;\n  }\n}\n")
        symbols = parse_file(path, "javascript")
        methods = [s for s in symbols if s.kind == "function" and s.name == "method"]
        assert len(methods) == 1


def test_symbol_is_a_plain_dataclass_with_expected_defaults() -> None:
    symbol = Symbol(kind="function", name="f", start_line=1, end_line=2, code="def f(): pass")
    assert symbol.decorators == []
    assert symbol.args == []
    assert symbol.bases == []
    assert symbol.methods == []
    assert symbol.parent_class is None
