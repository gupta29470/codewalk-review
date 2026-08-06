"""Tests for codewalk.graph.graph_runtime."""

from __future__ import annotations

from pathlib import Path

from codewalk.analysis.dependency_graph import build_dependency_graph
from codewalk.analysis.module_detector import detect_modules
from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore
from tests.conftest import write_repo_files


def _build_runtime(tmp_path: Path, files: dict[str, str]) -> tuple[GraphRuntime, GraphStore]:
    root = tmp_path / "repo"
    scanned = write_repo_files(root, files)
    dep_result = build_dependency_graph(scanned)
    module_result = detect_modules(scanned, dep_graph=dep_result.graph)
    store = GraphStore(root / ".codewalk" / "graph.duckdb")
    store.populate_from_analysis(scanned, dep_result.graph, module_result)
    return GraphRuntime(store), store


class TestBuildGraph:
    def test_vertex_and_edge_counts(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        try:
            assert runtime.file_graph.vcount() == 2
            assert runtime.file_graph.ecount() == 1
        finally:
            store.close()

    def test_empty_graph_does_not_crash(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {})
        try:
            assert runtime.file_graph.vcount() == 0
            assert runtime.centrality().betweenness == []
            assert runtime.detect_cycles().has_cycles is False
            assert runtime.topological_sort() == []
        finally:
            store.close()

    def test_rebuild_picks_up_new_edges(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        scanned = write_repo_files(root, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
        dep_result = build_dependency_graph(scanned)
        module_result = detect_modules(scanned, dep_graph=dep_result.graph)
        store = GraphStore(root / ".codewalk" / "graph.duckdb")
        store.populate_from_analysis(scanned, dep_result.graph, module_result)
        runtime = GraphRuntime(store)
        try:
            assert runtime.file_graph.ecount() == 0

            scanned2 = write_repo_files(root, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
            dep_result2 = build_dependency_graph(scanned2)
            module_result2 = detect_modules(scanned2, dep_graph=dep_result2.graph)
            store.populate_from_analysis(scanned2, dep_result2.graph, module_result2)
            runtime.rebuild()

            assert runtime.file_graph.ecount() == 1
        finally:
            store.close()


class TestBlastRadius:
    def test_transitive_dependents(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path,
            {
                "core.py": "x = 1\n",
                "mid.py": "from core import x\n",
                "top.py": "from mid import x\n",
            },
        )
        try:
            affected = set(runtime.get_blast_radius("core.py"))
            assert affected == {"mid.py", "top.py"}
        finally:
            store.close()

    def test_file_not_in_graph_returns_empty(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {"a.py": "x = 1\n"})
        try:
            assert runtime.get_blast_radius("missing.py") == []
        finally:
            store.close()


class TestTopologicalSort:
    def test_orphan_files_are_appended(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path,
            {
                "a.py": "from b import x\n",
                "b.py": "x = 1\n",
                "isolated.py": "y = 2\n",  # no import relationships at all
            },
        )
        try:
            order = runtime.topological_sort()
            assert order.index("b.py") < order.index("a.py")
            assert "isolated.py" in order
        finally:
            store.close()

    def test_cycle_falls_back_to_indegree_sort_without_crashing(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path, {"a.py": "from b import x\n", "b.py": "from a import y\n"}
        )
        try:
            order = runtime.topological_sort()
            assert set(order) == {"a.py", "b.py"}
        finally:
            store.close()


class TestDetectCycles:
    def test_no_cycle(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
        try:
            report = runtime.detect_cycles()
            assert report.has_cycles is False
            assert report.cycle_groups == []
        finally:
            store.close()

    def test_direct_cycle_detected(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path, {"a.py": "from b import x\n", "b.py": "from a import y\n"}
        )
        try:
            report = runtime.detect_cycles()
            assert report.has_cycles is True
            assert {"a.py", "b.py"} in [set(group) for group in report.cycle_groups]
            assert len(report.edges_to_break) >= 1
        finally:
            store.close()


class TestCentrality:
    def test_hub_file_ranks_highest_by_pagerank(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path,
            {
                "hub.py": "x = 1\n",
                "a.py": "from hub import x\n",
                "b.py": "from hub import x\n",
                "c.py": "from hub import x\n",
            },
        )
        try:
            report = runtime.centrality(top_n=1)
            assert report.pagerank[0].file == "hub.py"
        finally:
            store.close()


class TestShortestPath:
    def test_connected_files(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(
            tmp_path,
            {
                "a.py": "from b import x\n",
                "b.py": "from c import y\n",
                "c.py": "y = 1\n",
            },
        )
        try:
            path = runtime.shortest_path("a.py", "c.py")
            assert path == ["a.py", "b.py", "c.py"]
        finally:
            store.close()

    def test_disconnected_files_return_empty(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})
        try:
            assert runtime.shortest_path("a.py", "b.py") == []
        finally:
            store.close()

    def test_missing_endpoint_returns_empty(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {"a.py": "x = 1\n"})
        try:
            assert runtime.shortest_path("a.py", "missing.py") == []
            assert runtime.shortest_path("missing.py", "a.py") == []
        finally:
            store.close()


class TestGraphStats:
    def test_stats_reflect_graph_shape(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
        try:
            stats = runtime.get_graph_stats()
            assert stats.file_graph.vertices == 2
            assert stats.file_graph.edges == 1
            assert stats.file_graph.is_dag is True
        finally:
            store.close()

    def test_empty_graph_stats_defaults_to_dag_true(self, tmp_path: Path) -> None:
        runtime, store = _build_runtime(tmp_path, {})
        try:
            stats = runtime.get_graph_stats()
            assert stats.file_graph.vertices == 0
            assert stats.file_graph.is_dag is True
        finally:
            store.close()
