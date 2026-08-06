"""Tests for codewalk.analysis.reading_order."""

from __future__ import annotations

from codewalk.analysis.reading_order import generate_reading_order, has_cycle, topological_sort


class TestHasCycle:
    def test_no_cycle(self) -> None:
        assert has_cycle({"main.py": ["utils.py"], "utils.py": []}) is False

    def test_direct_two_node_cycle(self) -> None:
        assert has_cycle({"a.py": ["b.py"], "b.py": ["a.py"]}) is True

    def test_longer_cycle(self) -> None:
        assert has_cycle({"a.py": ["b.py"], "b.py": ["c.py"], "c.py": ["a.py"]}) is True

    def test_self_import_is_a_cycle(self) -> None:
        assert has_cycle({"a.py": ["a.py"]}) is True

    def test_empty_graph_has_no_cycle(self) -> None:
        assert has_cycle({}) is False

    def test_external_unresolved_imports_do_not_count_as_cycles(self) -> None:
        assert has_cycle({"a.py": ["os", "sys"]}) is False

    def test_cycle_in_one_branch_does_not_hide_behind_acyclic_branch(self) -> None:
        graph = {
            "main.py": ["utils.py", "a.py"],
            "utils.py": [],
            "a.py": ["b.py"],
            "b.py": ["a.py"],
        }
        assert has_cycle(graph) is True


class TestTopologicalSort:
    def test_dependencies_come_before_dependents(self) -> None:
        graph = {"main.py": ["utils.py"], "utils.py": []}
        order = topological_sort(graph)
        assert order.index("utils.py") < order.index("main.py")

    def test_deterministic_tie_breaking(self) -> None:
        graph = {"c.py": [], "a.py": [], "b.py": []}
        assert topological_sort(graph) == ["a.py", "b.py", "c.py"]

    def test_circular_dependency_still_returns_all_files(self) -> None:
        graph = {"a.py": ["b.py"], "b.py": ["a.py"]}
        order = topological_sort(graph)
        assert set(order) == {"a.py", "b.py"}

    def test_empty_graph(self) -> None:
        assert topological_sort({}) == []

    def test_diamond_dependency(self) -> None:
        # main depends on left+right, both depend on base.
        graph = {
            "main.py": ["left.py", "right.py"],
            "left.py": ["base.py"],
            "right.py": ["base.py"],
            "base.py": [],
        }
        order = topological_sort(graph)
        assert order.index("base.py") < order.index("left.py")
        assert order.index("base.py") < order.index("right.py")
        assert order.index("left.py") < order.index("main.py")
        assert order.index("right.py") < order.index("main.py")


class TestGenerateReadingOrder:
    def test_no_dependencies_message(self) -> None:
        result = generate_reading_order({"config.py": []})
        assert result.order[0].why == "No internal dependencies"
        assert result.total_files == 1
        assert result.has_cycles is False

    def test_depends_on_and_used_by_annotations(self) -> None:
        graph = {"main.py": ["utils.py"], "utils.py": []}
        result = generate_reading_order(graph)
        utils_item = next(item for item in result.order if item.file == "utils.py")
        assert utils_item.why == "No internal dependencies | Used by: main.py"
        main_item = next(item for item in result.order if item.file == "main.py")
        assert main_item.why == "Depends on: utils.py"

    def test_has_cycles_detected(self) -> None:
        graph = {"a.py": ["b.py"], "b.py": ["a.py"]}
        result = generate_reading_order(graph)
        assert result.has_cycles is True
        assert result.total_files == 2

    def test_positions_are_1_indexed_and_sequential(self) -> None:
        graph = {"a.py": [], "b.py": ["a.py"], "c.py": ["b.py"]}
        result = generate_reading_order(graph)
        assert [item.position for item in result.order] == [1, 2, 3]

    def test_empty_graph(self) -> None:
        result = generate_reading_order({})
        assert result.order == []
        assert result.total_files == 0
        assert result.has_cycles is False
