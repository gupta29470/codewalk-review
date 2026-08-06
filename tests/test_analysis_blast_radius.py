"""Tests for codewalk.analysis.blast_radius."""

from __future__ import annotations

from codewalk.analysis.blast_radius import (
    build_reverse_graph,
    calculate_full_blast_map,
    calculate_risk_level,
    get_blast_radius,
)


class TestBuildReverseGraph:
    def test_reverses_edges(self) -> None:
        graph = {"a.py": ["b.py", "c.py"], "b.py": ["c.py"], "c.py": []}
        reverse = build_reverse_graph(graph)
        assert reverse["c.py"] == ["a.py", "b.py"]
        assert reverse["b.py"] == ["a.py"]
        assert reverse["a.py"] == []

    def test_ignores_external_unresolved_deps(self) -> None:
        graph = {"a.py": ["os", "b.py"], "b.py": []}
        reverse = build_reverse_graph(graph)
        assert "os" not in reverse
        assert reverse["b.py"] == ["a.py"]


class TestCalculateRiskLevel:
    def test_empty_graph_is_none(self) -> None:
        assert calculate_risk_level(0, 0) == "none"

    def test_critical_by_ratio(self) -> None:
        assert calculate_risk_level(6, 10) == "critical"  # 60%

    def test_critical_by_absolute_count(self) -> None:
        assert calculate_risk_level(20, 1000) == "critical"

    def test_high_by_ratio(self) -> None:
        assert calculate_risk_level(3, 10) == "high"  # 30%

    def test_moderate_by_ratio(self) -> None:
        assert calculate_risk_level(2, 10) == "moderate"  # 20%

    def test_low(self) -> None:
        assert calculate_risk_level(1, 100) == "low"


class TestGetBlastRadius:
    def test_direct_and_transitive_dependents(self) -> None:
        # pipeline.py -> scanner.py -> filter.py (pipeline depends on scanner, scanner on filter)
        graph = {"pipeline.py": ["scanner.py"], "scanner.py": ["filter.py"], "filter.py": []}
        result = get_blast_radius("filter.py", graph)
        assert result.direct == ["scanner.py"]
        assert result.transitive == ["pipeline.py"]
        assert result.affected_files == 2

    def test_target_not_in_graph_returns_empty_result(self) -> None:
        graph = {"a.py": []}
        result = get_blast_radius("missing.py", graph)
        assert result.affected_files == 0
        assert result.risk_level == "none"
        assert result.impact_tree == {}

    def test_test_files_excluded_by_default(self) -> None:
        graph = {"utils.py": [], "main.py": ["utils.py"], "utils.test.py": ["utils.py"]}
        result = get_blast_radius("utils.py", graph)
        assert "utils.test.py" not in result.direct
        assert result.direct == ["main.py"]

    def test_test_files_included_when_requested(self) -> None:
        graph = {"utils.py": [], "utils.test.py": ["utils.py"]}
        result = get_blast_radius("utils.py", graph, exclude_test_stories=False)
        assert "utils.test.py" in result.direct

    def test_no_dependents_yields_low_risk(self) -> None:
        graph = {"isolated.py": []}
        result = get_blast_radius("isolated.py", graph)
        assert result.affected_files == 0
        assert result.risk_level == "low"

    def test_self_referential_file_does_not_infinite_loop(self) -> None:
        # A file that (incorrectly) imports itself must not cause runaway BFS.
        graph = {"a.py": ["a.py"]}
        result = get_blast_radius("a.py", graph)
        assert result.affected_files == 0


class TestCalculateFullBlastMap:
    def test_ranks_by_affected_files_descending(self) -> None:
        graph = {
            "core.py": [],
            "layer1.py": ["core.py"],
            "layer2.py": ["layer1.py"],
        }
        blast_map = calculate_full_blast_map(graph)
        assert blast_map.blast_map[0].file == "core.py"
        assert blast_map.blast_map[0].affected_files == 2

    def test_empty_graph(self) -> None:
        blast_map = calculate_full_blast_map({})
        assert blast_map.blast_map == []
        assert blast_map.stats.total_files == 0
        assert blast_map.highest_risk == ""

    def test_stats_bucket_counts_match_entries(self) -> None:
        graph = {"a.py": [], "b.py": ["a.py"]}
        blast_map = calculate_full_blast_map(graph)
        total_bucketed = (
            blast_map.stats.critical_files
            + blast_map.stats.high_files
            + blast_map.stats.moderate_files
            + blast_map.stats.low_files
        )
        assert total_bucketed == len(blast_map.blast_map)
