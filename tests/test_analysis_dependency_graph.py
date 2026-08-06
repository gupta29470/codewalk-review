"""Tests for codewalk.analysis.dependency_graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.analysis.dependency_graph import (
    build_dependency_graph,
    extract_imports,
    resolve_import_to_file,
)
from codewalk.errors import ParseError
from tests.conftest import write_repo_files


class TestExtractImports:
    def test_python_import_and_from_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.py"
        path.write_text("import os\nfrom pathlib import Path\n", encoding="utf-8")
        imports = extract_imports(path, "python")
        assert "os" in imports
        assert "pathlib" in imports

    def test_python_multi_name_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.py"
        path.write_text("import os, sys, json\n", encoding="utf-8")
        assert set(extract_imports(path, "python")) == {"os", "sys", "json"}

    def test_python_relative_import_from_dot(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.py"
        path.write_text("from . import utils, helpers\n", encoding="utf-8")
        assert set(extract_imports(path, "python")) == {".utils", ".helpers"}

    def test_javascript_es_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.js"
        path.write_text("import { helper } from './helper';\n", encoding="utf-8")
        assert extract_imports(path, "javascript") == ["./helper"]

    def test_javascript_commonjs_require(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.js"
        path.write_text("const helper = require('./helper');\n", encoding="utf-8")
        assert extract_imports(path, "javascript") == ["./helper"]

    def test_unsupported_language_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.yaml"
        path.write_text("key: value\n", encoding="utf-8")
        assert extract_imports(path, "yaml") == []

    def test_unreadable_file_raises_parse_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.py"
        with pytest.raises(ParseError):
            extract_imports(missing, "python")

    def test_java_import(self, tmp_path: Path) -> None:
        path = tmp_path / "Mod.java"
        path.write_text("import com.example.Helper;\n\nclass Mod {}\n", encoding="utf-8")
        assert extract_imports(path, "java") == ["com.example.Helper"]

    def test_go_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.go"
        path.write_text('package mod\n\nimport "fmt"\n', encoding="utf-8")
        assert extract_imports(path, "go") == ["fmt"]

    def test_rust_use_declaration(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.rs"
        path.write_text("use std::collections::HashMap;\n", encoding="utf-8")
        assert extract_imports(path, "rust") == ["std::collections::HashMap"]

    def test_c_include(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.c"
        path.write_text('#include "foo.h"\n', encoding="utf-8")
        assert extract_imports(path, "c") == ["foo.h"]

    def test_cpp_include(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.cpp"
        path.write_text("#include <vector>\n", encoding="utf-8")
        assert extract_imports(path, "cpp") == ["vector"]

    def test_csharp_using_directive(self, tmp_path: Path) -> None:
        path = tmp_path / "Mod.cs"
        path.write_text("using System.Collections.Generic;\n", encoding="utf-8")
        assert extract_imports(path, "csharp") == ["System.Collections.Generic"]

    def test_php_namespace_use(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.php"
        path.write_text("<?php\nuse Example\\Helper;\n", encoding="utf-8")
        assert extract_imports(path, "php") == ["Example\\Helper"]

    def test_kotlin_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.kt"
        path.write_text("import okio.internal.Buffer\n", encoding="utf-8")
        assert extract_imports(path, "kotlin") == ["okio.internal.Buffer"]

    def test_swift_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.swift"
        path.write_text("import Foundation\n", encoding="utf-8")
        assert extract_imports(path, "swift") == ["Foundation"]

    def test_ruby_require(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.rb"
        path.write_text("require 'json'\n", encoding="utf-8")
        assert extract_imports(path, "ruby") == ["json"]

    def test_ruby_non_require_call_yields_no_imports(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.rb"
        path.write_text("puts 'hello'\n", encoding="utf-8")
        assert extract_imports(path, "ruby") == []

    def test_typescript_import(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.ts"
        path.write_text("import { helper } from './helper';\n", encoding="utf-8")
        assert extract_imports(path, "typescript") == ["./helper"]

    def test_javascript_non_require_call_yields_no_imports(self, tmp_path: Path) -> None:
        path = tmp_path / "mod.js"
        path.write_text("console.log('hi');\n", encoding="utf-8")
        assert extract_imports(path, "javascript") == []


class TestResolveImportToFile:
    def test_python_absolute_import(self) -> None:
        all_files = frozenset({"pkg/utils.py", "pkg/__init__.py"})
        assert resolve_import_to_file("pkg.utils", "python", all_files) == "pkg/utils.py"

    def test_python_relative_single_dot(self) -> None:
        all_files = frozenset({"pkg/utils.py", "pkg/main.py"})
        resolved = resolve_import_to_file(".utils", "python", all_files, source_file="pkg/main.py")
        assert resolved == "pkg/utils.py"

    def test_python_relative_double_dot(self) -> None:
        all_files = frozenset({"pkg/utils.py", "pkg/sub/main.py"})
        resolved = resolve_import_to_file(
            "..utils", "python", all_files, source_file="pkg/sub/main.py"
        )
        assert resolved == "pkg/utils.py"

    def test_python_unresolved_import_returns_raw(self) -> None:
        all_files = frozenset({"pkg/utils.py"})
        assert resolve_import_to_file("os", "python", all_files) == "os"

    def test_python_package_init_resolution(self) -> None:
        all_files = frozenset({"pkg/sub/__init__.py"})
        assert resolve_import_to_file("pkg.sub", "python", all_files) == "pkg/sub/__init__.py"

    def test_javascript_relative_with_extension_swap(self) -> None:
        """Source imports './foo.js' but only foo.ts exists on disk (TS convention)."""
        all_files = frozenset({"src/foo.ts", "src/main.js"})
        resolved = resolve_import_to_file(
            "./foo.js", "javascript", all_files, source_file="src/main.js"
        )
        assert resolved == "src/foo.ts"

    def test_javascript_relative_without_extension_tries_index(self) -> None:
        all_files = frozenset({"src/utils/index.ts", "src/main.ts"})
        resolved = resolve_import_to_file(
            "./utils", "typescript", all_files, source_file="src/main.ts"
        )
        assert resolved == "src/utils/index.ts"

    def test_javascript_bare_import_is_external_unresolved(self) -> None:
        all_files = frozenset({"src/main.js"})
        assert resolve_import_to_file("express", "javascript", all_files) == "express"

    def test_java_dotted_package_resolution(self) -> None:
        all_files = frozenset({"com/example/Helper.java"})
        resolved = resolve_import_to_file("com.example.Helper", "java", all_files)
        assert resolved == "com/example/Helper.java"

    def test_java_suffix_match_when_repo_scanned_from_subdir(self) -> None:
        """Repo scanned starting inside src/, so all_files lacks the full package prefix."""
        all_files = frozenset({"example/Helper.java"})
        resolved = resolve_import_to_file("com.example.Helper", "java", all_files)
        assert resolved == "example/Helper.java"

    def test_kotlin_dotted_package_resolution(self) -> None:
        all_files = frozenset({"okio/internal/Buffer.kt"})
        resolved = resolve_import_to_file("okio.internal.Buffer", "kotlin", all_files)
        assert resolved == "okio/internal/Buffer.kt"

    def test_go_suffix_directory_match(self) -> None:
        all_files = frozenset({"pkg/util/helper.go"})
        resolved = resolve_import_to_file("github.com/example/repo/pkg/util", "go", all_files)
        assert resolved == "pkg/util/helper.go"

    def test_go_prefers_deepest_directory_suffix_match(self) -> None:
        """When multiple files share the last path segment, the one whose
        parent directories match the LONGEST suffix of the import path wins."""
        all_files = frozenset({"other/util/helper.go", "pkg/util/helper.go"})
        resolved = resolve_import_to_file("github.com/example/repo/pkg/util", "go", all_files)
        assert resolved == "pkg/util/helper.go"

    def test_go_falls_back_to_single_segment_match_when_no_deeper_match_exists(self) -> None:
        """Known limitation: with only one candidate, a single shared directory
        segment name is accepted even without matching the full import path."""
        all_files = frozenset({"other/util/helper.go"})
        resolved = resolve_import_to_file("github.com/example/repo/pkg/util", "go", all_files)
        assert resolved == "other/util/helper.go"

    def test_rust_crate_relative_resolution(self) -> None:
        all_files = frozenset({"Cargo.toml", "src/utils.rs", "src/main.rs"})
        resolved = resolve_import_to_file(
            "crate::utils", "rust", all_files, source_file="src/main.rs"
        )
        assert resolved == "src/utils.rs"

    def test_ruby_relative_require(self) -> None:
        all_files = frozenset({"lib/helper.rb"})
        assert resolve_import_to_file("./lib/helper", "ruby", all_files) == "lib/helper.rb"

    def test_c_include_with_prefix(self) -> None:
        all_files = frozenset({"include/foo.h"})
        assert resolve_import_to_file("foo.h", "c", all_files) == "include/foo.h"

    def test_csharp_namespace_resolution(self) -> None:
        all_files = frozenset({"Example/Helper.cs"})
        resolved = resolve_import_to_file("Example.Helper", "csharp", all_files)
        assert resolved == "Example/Helper.cs"

    def test_php_namespace_resolution(self) -> None:
        all_files = frozenset({"src/Example/Helper.php"})
        resolved = resolve_import_to_file("Example\\Helper", "php", all_files)
        assert resolved == "src/Example/Helper.php"

    def test_swift_has_no_file_level_resolution(self) -> None:
        all_files = frozenset({"Sources/Helper.swift"})
        assert resolve_import_to_file("Foundation", "swift", all_files) == "Foundation"

    def test_unknown_language_returns_raw(self) -> None:
        all_files = frozenset({"a.txt"})
        assert resolve_import_to_file("whatever", "made_up_language", all_files) == "whatever"

    def test_python_suffix_match_when_repo_scanned_from_subdir(self) -> None:
        all_files = frozenset({"config.py"})
        resolved = resolve_import_to_file("codewalk.config", "python", all_files)
        assert resolved == "config.py"

    def test_python_relative_import_too_many_dots_returns_raw(self) -> None:
        all_files = frozenset({"main.py"})
        resolved = resolve_import_to_file("....utils", "python", all_files, source_file="main.py")
        assert resolved == "....utils"

    def test_rust_non_crate_import_returns_raw(self) -> None:
        all_files = frozenset({"src/main.rs"})
        assert resolve_import_to_file("std::io", "rust", all_files) == "std::io"

    def test_ruby_absolute_require_returns_raw(self) -> None:
        all_files = frozenset({"lib/helper.rb"})
        assert resolve_import_to_file("json", "ruby", all_files) == "json"

    def test_csharp_suffix_match_when_repo_scanned_from_subdir(self) -> None:
        all_files = frozenset({"Helper.cs"})
        resolved = resolve_import_to_file("Example.Namespace.Helper", "csharp", all_files)
        assert resolved == "Helper.cs"

    def test_php_suffix_match_when_repo_scanned_from_subdir(self) -> None:
        all_files = frozenset({"Helper.php"})
        resolved = resolve_import_to_file("Example\\Namespace\\Helper", "php", all_files)
        assert resolved == "Helper.php"

    def test_c_import_exact_path_match(self) -> None:
        all_files = frozenset({"foo/bar.h"})
        assert resolve_import_to_file("foo/bar.h", "c", all_files) == "foo/bar.h"

    def test_javascript_extension_present_but_file_missing_returns_raw(self) -> None:
        all_files = frozenset({"src/main.js"})
        resolved = resolve_import_to_file(
            "./missing.js", "javascript", all_files, source_file="src/main.js"
        )
        assert resolved == "./missing.js"


class TestBuildDependencyGraph:
    def test_simple_two_file_graph(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from .utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        result = build_dependency_graph(files)
        assert result.graph["main.py"] == ["utils.py"]
        assert result.stats.total_files == 2
        assert result.warnings == []

    def test_circular_imports_do_not_crash(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "a.py": "from . import b\n",
                "b.py": "from . import a\n",
            },
        )
        result = build_dependency_graph(files)
        assert result.graph["a.py"] == ["b.py"]
        assert result.graph["b.py"] == ["a.py"]

    def test_unresolved_import_counted(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"main.py": "import os\n"})
        result = build_dependency_graph(files)
        assert result.graph["main.py"] == ["os"]
        assert result.stats.unresolved == 1

    def test_single_file_repo(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"solo.py": "x = 1\n"})
        result = build_dependency_graph(files)
        assert result.graph == {"solo.py": []}
        assert result.stats.total_edges == 0

    def test_repo_with_zero_recognized_languages(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"data.yaml": "key: value\n", "notes.md": "# hi\n"})
        result = build_dependency_graph(files)
        assert result.graph == {"data.yaml": [], "notes.md": []}
        assert result.stats.total_edges == 0
        assert result.warnings == []

    def test_unreadable_file_produces_warning_not_crash(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"main.py": "import os\n"})
        # Simulate a file that disappeared between scan and graph-build.
        files[0].absolute_path.unlink()

        result = build_dependency_graph(files)

        assert result.graph["main.py"] == []
        assert len(result.warnings) == 1
        assert "main.py" in result.warnings[0]

    def test_empty_file_list(self) -> None:
        result = build_dependency_graph([])
        assert result.graph == {}
        assert result.stats.total_files == 0
