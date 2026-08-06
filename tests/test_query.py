"""Tests for codewalk.query.query -- deterministic query/explain layer.

No ChromaDB: symbol lookup reads the DuckDB `symbols` table; code snippets
are read live from disk using the graph snapshot's line ranges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk.analysis.dependency_graph import build_dependency_graph
from codewalk.analysis.module_detector import detect_modules
from codewalk.errors import GraphNotBuiltError
from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore
from codewalk.query import query as q
from tests.conftest import write_repo_files


def _build(tmp_path: Path, files: dict[str, str]) -> tuple[GraphStore, GraphRuntime, Path]:
    root = tmp_path / "repo"
    scanned = write_repo_files(root, files)
    dep_result = build_dependency_graph(scanned)
    module_result = detect_modules(scanned, dep_graph=dep_result.graph)
    store = GraphStore(root / ".codewalk" / "graph.duckdb")
    store.populate_from_analysis(scanned, dep_result.graph, module_result)
    runtime = GraphRuntime(store)
    return store, runtime, root


class TestExplainFunctionText:
    def test_happy_path_includes_code_and_blast_radius(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        try:
            text = q.explain_function_text(store, root, "helper")
            assert "def helper():" in text
            assert "Blast Radius" in text
            assert "Called by" in text
            assert "run" in text
        finally:
            store.close()

    def test_unknown_function_raises_value_error_with_suggestion(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def helper_one():\n    pass\n"})
        try:
            with pytest.raises(ValueError, match="helper_one"):
                q.explain_function_text(store, root, "help")
        finally:
            store.close()

    def test_unknown_function_with_no_suggestions(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def alpha():\n    pass\n"})
        try:
            with pytest.raises(ValueError, match="No function named"):
                q.explain_function_text(store, root, "zzz_completely_unrelated")
        finally:
            store.close()

    def test_empty_graph_raises_graph_not_built_error(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.explain_function_text(store, tmp_path, "anything")
        finally:
            store.close()

    def test_function_in_two_files_lists_both(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(
            tmp_path,
            {"a.py": "def helper():\n    pass\n", "b.py": "def helper():\n    pass\n"},
        )
        try:
            text = q.explain_function_text(store, root, "helper")
            assert "Also found at" in text
            assert "a.py" in text
            assert "b.py" in text
        finally:
            store.close()

    def test_stale_line_range_does_not_crash(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def helper():\n    pass\n"})
        try:
            # Simulate the file shrinking since the graph was built.
            (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
            text = q.explain_function_text(store, root, "helper")
            assert "stale" in text.lower() or "helper" in text
        finally:
            store.close()


class TestExplainClassText:
    def test_happy_path(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "class Greeter:\n    pass\n"})
        try:
            text = q.explain_class_text(store, root, "Greeter")
            assert "class Greeter" in text
        finally:
            store.close()

    def test_unknown_class_raises_value_error(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "class Greeter:\n    pass\n"})
        try:
            with pytest.raises(ValueError, match="No class named"):
                q.explain_class_text(store, root, "Missing")
        finally:
            store.close()


class TestLookupSymbolText:
    def test_exact_match(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def helper():\n    pass\n"})
        try:
            text = q.lookup_symbol_text(store, root, "helper")
            assert "helper" in text
            assert "def helper" in text
        finally:
            store.close()

    def test_substring_fallback(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def helper_function():\n    pass\n"})
        try:
            text = q.lookup_symbol_text(store, root, "helper")
            assert "helper_function" in text
        finally:
            store.close()

    def test_no_match_raises_value_error(self, tmp_path: Path) -> None:
        store, _runtime, root = _build(tmp_path, {"mod.py": "def helper():\n    pass\n"})
        try:
            with pytest.raises(ValueError, match="No symbols matched"):
                q.lookup_symbol_text(store, root, "zzz_nothing")
        finally:
            store.close()


class TestOverviewText:
    def test_happy_path(self, tmp_path: Path) -> None:
        store, runtime, root = _build(
            tmp_path,
            {
                "requirements.txt": "pytest\n",
                "src/auth/login.py": "x = 1\n",
                "src/billing/pay.py": "from auth.login import x\n",
            },
        )
        try:
            text = q.overview_text(store, runtime, root)
            assert "Project Overview" in text
            assert "auth" in text
            assert "billing" in text
        finally:
            store.close()

    def test_empty_repo_raises_graph_not_built_error(self, tmp_path: Path) -> None:
        store, runtime, root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.overview_text(store, runtime, root)
        finally:
            store.close()

    def test_cycle_is_flagged(self, tmp_path: Path) -> None:
        store, runtime, root = _build(
            tmp_path, {"a.py": "from b import x\n", "b.py": "from a import y\n"}
        )
        try:
            text = q.overview_text(store, runtime, root)
            assert "Circular Dependencies" in text
        finally:
            store.close()


class TestBlastRadiusMapText:
    def test_top_n_default(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path, {"core.py": "x = 1\n", "user.py": "from core import x\n"}
        )
        try:
            text = q.blast_radius_map_text(store)
            assert "top 30 riskiest" in text
            assert "core.py" in text
        finally:
            store.close()

    def test_module_target(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/billing/pay.py": "from auth.login import x\n",
            },
        )
        try:
            text = q.blast_radius_map_text(store, target="auth")
            assert "module 'auth'" in text
        finally:
            store.close()

    def test_file_target(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path, {"core.py": "x = 1\n", "user.py": "from core import x\n"}
        )
        try:
            text = q.blast_radius_map_text(store, target="core.py")
            assert "file 'core.py'" in text
        finally:
            store.close()

    def test_unknown_target_raises_value_error(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="not found as a module or file"):
                q.blast_radius_map_text(store, target="totally_missing")
        finally:
            store.close()

    def test_empty_graph_raises(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.blast_radius_map_text(store)
        finally:
            store.close()


class TestFindCircularDependenciesText:
    def test_no_cycles(self, tmp_path: Path) -> None:
        _store, runtime, _root = _build(tmp_path, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
        assert q.find_circular_dependencies_text(runtime) == "No circular dependencies detected."

    def test_cycle_detected(self, tmp_path: Path) -> None:
        _store, runtime, _root = _build(
            tmp_path, {"a.py": "from b import x\n", "b.py": "from a import y\n"}
        )
        text = q.find_circular_dependencies_text(runtime)
        assert "Circular Dependencies" in text
        assert "a.py" in text and "b.py" in text


class TestReadingOrderText:
    def test_happy_path_order(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
        try:
            text = q.reading_order_text(store)
            assert text.index("b.py") < text.index("a.py")
        finally:
            store.close()

    def test_module_scope(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/billing/pay.py": "y = 1\n",
            },
        )
        try:
            text = q.reading_order_text(store, module_name="auth")
            assert "login.py" in text
            assert "pay.py" not in text
        finally:
            store.close()

    def test_unknown_module_raises(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="not found"):
                q.reading_order_text(store, module_name="missing")
        finally:
            store.close()


class TestExecutionFlowText:
    def test_module_level(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/billing/pay.py": "from auth.login import x\n",
            },
        )
        try:
            text = q.execution_flow_text(store)
            assert "Module Level" in text
            assert "billing" in text
        finally:
            store.close()

    def test_file_level_for_module(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path,
            {
                "auth/login.py": "from auth.session import y\n",
                "auth/session.py": "y = 1\n",
                "billing/pay.py": "z = 1\n",  # sibling prevents single-child module collapse
            },
        )
        try:
            text = q.execution_flow_text(store, module_name="auth")
            assert "file level" in text
            assert "login.py" in text
        finally:
            store.close()

    def test_unknown_module_raises(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="not found"):
                q.execution_flow_text(store, module_name="missing")
        finally:
            store.close()


class TestArchitectureHealthText:
    def test_happy_path(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(
            tmp_path, {"core.py": "x = 1\n", "user.py": "from core import x\n"}
        )
        try:
            text = q.architecture_health_text(store, runtime)
            assert "Architecture Health" in text
            assert "No circular dependencies" in text
        finally:
            store.close()

    def test_cycles_flagged(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(
            tmp_path, {"a.py": "from b import x\n", "b.py": "from a import y\n"}
        )
        try:
            text = q.architecture_health_text(store, runtime)
            assert "circular dependency group" in text
        finally:
            store.close()

    def test_empty_graph_raises(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.architecture_health_text(store, runtime)
        finally:
            store.close()


class TestCallChainText:
    def test_connected_files(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(
            tmp_path,
            {"a.py": "from b import x\n", "b.py": "from c import y\n", "c.py": "y = 1\n"},
        )
        try:
            text = q.call_chain_text(store, runtime, "a.py", "c.py")
            assert "a.py -> b.py -> c.py" in text
        finally:
            store.close()

    def test_no_path_returns_message_not_raise(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})
        try:
            text = q.call_chain_text(store, runtime, "a.py", "b.py")
            assert "No import chain found" in text
        finally:
            store.close()

    def test_self_referential_chain(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            text = q.call_chain_text(store, runtime, "a.py", "a.py")
            assert "trivial chain" in text
        finally:
            store.close()

    def test_unknown_source_raises_value_error(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="Source file"):
                q.call_chain_text(store, runtime, "missing.py", "a.py")
        finally:
            store.close()

    def test_unknown_target_raises_value_error(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="Target file"):
                q.call_chain_text(store, runtime, "a.py", "missing.py")
        finally:
            store.close()

    def test_empty_graph_raises_graph_not_built_error(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.call_chain_text(store, runtime, "a.py", "b.py")
        finally:
            store.close()


class TestModuleInfoText:
    def test_happy_path(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(
            tmp_path,
            {
                "auth/login.py": "from billing.pay import x\n",
                "billing/pay.py": "x = 1\n",
            },
        )
        try:
            text = q.module_info_text(store, runtime, "auth")
            assert "Module: auth" in text
            assert "login.py" in text
            assert "Depends on:** billing" in text
        finally:
            store.close()

    def test_module_with_zero_dependents(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"src/lonely/mod.py": "x = 1\n"})
        try:
            text = q.module_info_text(store, runtime, "lonely")
            assert "Depended on by:** None" in text
        finally:
            store.close()

    def test_unknown_module_raises_value_error(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {"a.py": "x = 1\n"})
        try:
            with pytest.raises(ValueError, match="not found"):
                q.module_info_text(store, runtime, "missing")
        finally:
            store.close()

    def test_empty_graph_raises_graph_not_built_error(self, tmp_path: Path) -> None:
        store, runtime, _root = _build(tmp_path, {})
        try:
            with pytest.raises(GraphNotBuiltError):
                q.module_info_text(store, runtime, "anything")
        finally:
            store.close()


class TestHelpers:
    def test_short_name(self) -> None:
        assert q.short_name("src/foo/bar.py") == "bar.py"
        assert q.short_name("bar.py") == "bar.py"

    def test_resolve_module_name_case_insensitive(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(
            tmp_path, {"Auth/login.py": "x = 1\n", "Billing/pay.py": "y = 1\n"}
        )
        try:
            modules = store.get_modules()
            assert q.resolve_module_name(modules, "auth") is not None
        finally:
            store.close()

    def test_file_import_graph_includes_files_with_no_edges(self, tmp_path: Path) -> None:
        store, _runtime, _root = _build(tmp_path, {"isolated.py": "x = 1\n"})
        try:
            graph = q.file_import_graph(store)
            assert graph == {"isolated.py": []}
        finally:
            store.close()
