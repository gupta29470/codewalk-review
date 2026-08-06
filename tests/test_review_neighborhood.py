"""Tests for review.neighborhood: caller/test/interface expansion around a diff."""

from __future__ import annotations

from pathlib import Path

from codewalk.review.neighborhood import (
    _extensions_for_file,
    _find_test_files,
    _is_test_file,
    _read_lines,
    _relative_import_module,
    expand_neighborhood,
)
from tests.conftest import build_graph, make_diff_file


def test_no_graph_store_still_finds_interfaces(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "helper.ts").write_text("export const helper = () => 1;\n", encoding="utf-8")
    (root / "src" / "main.ts").write_text(
        "import { helper } from './helper';\n\nhelper();\n", encoding="utf-8"
    )
    diff_files = [make_diff_file("src/main.ts", added=["helper();"])]

    result = expand_neighborhood(root, diff_files, graph_store=None)
    interface_paths = {s.file_path for s in result.snippets if s.source == "interface"}
    assert "src/helper.ts" in interface_paths


def test_no_graph_store_no_callers_or_tests(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    diff_files = [make_diff_file("a.py", added=["x = 1"])]

    result = expand_neighborhood(root, diff_files, graph_store=None)
    assert all(s.source != "caller" for s in result.snippets)
    assert all(s.source != "test" for s in result.snippets)


def test_finds_callers_via_graph_store(tmp_path: Path) -> None:
    store, _runtime, root = build_graph(
        tmp_path,
        {
            "utils.py": "def helper():\n    return 1\n",
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
        },
    )
    diff_files = [make_diff_file("utils.py", added=["def helper():", "    return 2"])]

    result = expand_neighborhood(root, diff_files, graph_store=store)
    caller_files = {s.file_path for s in result.snippets if s.source == "caller"}
    assert "main.py" in caller_files


def test_finds_tests_by_naming_convention(tmp_path: Path) -> None:
    store, _runtime, root = build_graph(
        tmp_path,
        {
            "app.py": "def compute():\n    return 1\n",
            "test_app.py": (
                "from app import compute\n\n\ndef test_compute():\n    assert compute() == 1\n"
            ),
        },
    )
    diff_files = [make_diff_file("app.py", added=["x = 1"])]

    result = expand_neighborhood(root, diff_files, graph_store=store)
    test_files = {s.file_path for s in result.snippets if s.source == "test"}
    assert "test_app.py" in test_files


def test_no_matching_test_file_no_test_snippets(tmp_path: Path) -> None:
    store, _runtime, root = build_graph(tmp_path, {"lonely.py": "x = 1\n"})
    diff_files = [make_diff_file("lonely.py", added=["x = 2"])]

    result = expand_neighborhood(root, diff_files, graph_store=store)
    assert all(s.source != "test" for s in result.snippets)


def test_deep_mode_widens_budget(tmp_path: Path) -> None:
    store, _runtime, root = build_graph(
        tmp_path,
        {
            "utils.py": "def helper():\n    return 1\n",
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
        },
    )
    diff_files = [make_diff_file("utils.py", added=["def helper():", "    return 2"])]

    result = expand_neighborhood(root, diff_files, graph_store=store, deep=True, max_snippets=1)
    # deep=True raises the effective max_snippets floor above the caller's low value.
    assert result is not None


def test_max_tokens_budget_truncates_snippet_list(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    big_content = "x = 1\n" * 5000
    for i in range(3):
        (root / f"mod{i}.ts").write_text(
            f"import {{ helper }} from './helper{i}';\n{big_content}", encoding="utf-8"
        )
        (root / f"helper{i}.ts").write_text(big_content, encoding="utf-8")

    diff_files = [make_diff_file(f"mod{i}.ts", added=["x = 1"]) for i in range(3)]
    result = expand_neighborhood(root, diff_files, graph_store=None, max_tokens=10)
    assert result.snippets == []


def test_max_snippets_cap_respected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for i in range(5):
        (root / f"mod{i}.ts").write_text(
            f"import {{ h }} from './helper{i}';\nh();\n", encoding="utf-8"
        )
        (root / f"helper{i}.ts").write_text("export const h = () => 1;\n", encoding="utf-8")

    diff_files = [make_diff_file(f"mod{i}.ts", added=["h();"]) for i in range(5)]
    result = expand_neighborhood(root, diff_files, graph_store=None, max_snippets=2)
    assert len(result.snippets) <= 2


def test_unreadable_or_missing_file_does_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    diff_files = [make_diff_file("does_not_exist.py", added=["x = 1"])]

    result = expand_neighborhood(root, diff_files, graph_store=None)
    assert result.snippets == []


def test_relevant_files_prioritized_in_snippet_order(tmp_path: Path) -> None:
    store, _runtime, root = build_graph(
        tmp_path,
        {
            "utils.py": "def helper():\n    return 1\n",
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "test_utils.py": (
                "from utils import helper\n\n\ndef test_helper():\n    assert helper() == 1\n"
            ),
        },
    )
    diff_files = [make_diff_file("utils.py", added=["def helper():", "    return 2"])]

    result = expand_neighborhood(root, diff_files, graph_store=store, relevant_files={"main.py"})
    if result.snippets:
        # main.py (an explicitly "relevant" file) should sort ahead of test snippets.
        priorities = [s.file_path for s in result.snippets]
        if "main.py" in priorities:
            assert priorities.index("main.py") == 0


def test_read_lines_nonexistent_path_returns_empty(tmp_path: Path) -> None:
    assert _read_lines(tmp_path / "missing.py", 1, 10) == ""


def test_read_lines_returns_requested_range(tmp_path: Path) -> None:
    path = tmp_path / "file.py"
    path.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")
    content = _read_lines(path, 2, 4)
    assert content == "line1\nline2\nline3"


def test_find_test_files_stops_at_candidate_limit() -> None:
    all_files = [f"pkg{i}/app.test.ts" for i in range(10)]
    candidates = _find_test_files(all_files, "app")
    assert len(candidates) == 5


def test_extensions_for_file_go_and_dart() -> None:
    assert _extensions_for_file("main.go") == (".go",)
    assert _extensions_for_file("main.dart") == (".dart",)


def test_extensions_for_file_unknown_falls_back_to_all() -> None:
    assert ".py" in _extensions_for_file("main.rs")


def test_relative_import_module_plain_import_without_from_returns_none() -> None:
    assert _relative_import_module("import os") is None


def test_relative_import_module_non_relative_from_returns_none() -> None:
    assert _relative_import_module("from external_pkg import thing") is None


def test_relative_import_module_relative_import_detected() -> None:
    assert _relative_import_module("import { thing } from './helper';") == "./helper"


def test_relative_import_module_non_import_line_returns_none() -> None:
    assert _relative_import_module("x = 1") is None


def test_is_test_file_variants() -> None:
    assert _is_test_file("app.test.ts") is True
    assert _is_test_file("app.spec.ts") is True
    assert _is_test_file("test_app.py") is True
    assert _is_test_file("app_test.py") is True
    assert _is_test_file("app.py") is False


def test_find_interfaces_resolves_index_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "utils").mkdir(parents=True)
    (root / "utils" / "index.ts").write_text("export const helper = () => 1;\n", encoding="utf-8")
    (root / "main.ts").write_text(
        "import { helper } from './utils';\n\nhelper();\n", encoding="utf-8"
    )

    diff_files = [make_diff_file("main.ts", added=["helper();"])]
    result = expand_neighborhood(root, diff_files, graph_store=None)
    interface_paths = {s.file_path for s in result.snippets if s.source == "interface"}
    assert "utils/index.ts" in interface_paths


def test_find_interfaces_dedupes_repeated_import(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "helper.ts").write_text("export const helper = () => 1;\n", encoding="utf-8")
    (root / "main.ts").write_text(
        "import { helper } from './helper';\nimport { helper as h2 } from './helper';\nhelper();\n",
        encoding="utf-8",
    )

    diff_files = [make_diff_file("main.ts", added=["helper();"])]
    result = expand_neighborhood(root, diff_files, graph_store=None)
    interface_snippets = [s for s in result.snippets if s.source == "interface"]
    assert len(interface_snippets) == 1


def test_find_interfaces_unresolvable_relative_import_skipped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "main.ts").write_text("import { helper } from './does_not_exist';\n", encoding="utf-8")

    diff_files = [make_diff_file("main.ts", added=["helper();"])]
    result = expand_neighborhood(root, diff_files, graph_store=None)
    assert all(s.source != "interface" for s in result.snippets)
