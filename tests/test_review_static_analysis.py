"""Tests for review.static_analysis: graph-based deterministic risk annotations."""

from __future__ import annotations

from pathlib import Path

from codewalk.review.static_analysis import run_static_analysis
from tests.conftest import build_graph, make_diff_file


def test_no_graph_at_all_degrades_gracefully_not_crash() -> None:
    diff_files = [make_diff_file("a.py", added=["x = 1"] * 60)]
    result = run_static_analysis(diff_files)

    assert result.total_added == 60
    annotation = result.risk_annotations["a.py"]
    assert annotation.fan_in == 50  # capped diff-size proxy
    assert annotation.affected_files == []
    assert annotation.cycle_participation is False
    assert annotation.is_bottleneck is False


def test_empty_diff_files_returns_empty_result() -> None:
    result = run_static_analysis([])
    assert result.risk_annotations == {}
    assert result.total_added == 0
    assert result.total_removed == 0


def test_totals_sum_across_files() -> None:
    diff_files = [
        make_diff_file("a.py", added=["x = 1", "y = 2"]),
        make_diff_file("b.py", added=["z = 3"], removed=["z = 0"]),
    ]
    result = run_static_analysis(diff_files)
    assert result.total_added == 3
    assert result.total_removed == 1


def test_risk_annotation_uses_blast_radius_from_graph_runtime(tmp_path: Path) -> None:
    _store, runtime, _root = build_graph(
        tmp_path,
        {
            "utils.py": "def helper():\n    return 1\n",
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
        },
    )
    diff_files = [make_diff_file("utils.py", added=["def helper():", "    return 2"])]
    result = run_static_analysis(diff_files, graph_runtime=runtime)

    annotation = result.risk_annotations["utils.py"]
    assert "main.py" in annotation.affected_files
    assert annotation.fan_in == 1


def test_risk_annotation_file_with_no_graph_edges_uses_diff_size_fallback(tmp_path: Path) -> None:
    _store, runtime, _root = build_graph(tmp_path, {"isolated.py": "x = 1\n"})
    diff_files = [make_diff_file("isolated.py", added=["x = 2"])]
    result = run_static_analysis(diff_files, graph_runtime=runtime)

    annotation = result.risk_annotations["isolated.py"]
    assert annotation.affected_files == []
    assert annotation.fan_in >= 0


def test_cycle_participation_flagged(tmp_path: Path) -> None:
    _store, runtime, _root = build_graph(
        tmp_path,
        {
            "a.py": "from b import bee\n\n\ndef aye():\n    return bee()\n",
            "b.py": "from a import aye\n\n\ndef bee():\n    return 1\n",
        },
    )
    diff_files = [make_diff_file("a.py", added=["x = 1"])]
    result = run_static_analysis(diff_files, graph_runtime=runtime)

    annotation = result.risk_annotations["a.py"]
    assert annotation.cycle_participation is True


def test_to_prompt_text_empty_when_nothing_noteworthy() -> None:
    diff_files = [make_diff_file("quiet.py", added=["x = 1"])]
    result = run_static_analysis(diff_files)
    annotation = result.risk_annotations["quiet.py"]
    # A tiny lone file with no graph shouldn't trip any "high" flags.
    assert annotation.is_high_fan_in is False
    assert annotation.is_high_pagerank is False
    assert annotation.to_prompt_text() == ""


def test_to_prompt_text_mentions_affected_files_and_cycle(tmp_path: Path) -> None:
    _store, runtime, _root = build_graph(
        tmp_path,
        {
            "a.py": "from b import bee\n\n\ndef aye():\n    return bee()\n",
            "b.py": "from a import aye\n\n\ndef bee():\n    return 1\n",
        },
    )
    diff_files = [make_diff_file("a.py", added=["x = 1"])]
    result = run_static_analysis(diff_files, graph_runtime=runtime)
    text = result.risk_annotations["a.py"].to_prompt_text()
    assert "affected file" in text
    assert "circular dependency" in text


def test_fan_in_falls_back_to_graph_store_symbol_callers(tmp_path: Path) -> None:
    store, runtime, _root = build_graph(
        tmp_path,
        {
            "utils.py": "def helper():\n    return 1\n",
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
        },
    )
    diff_files = [make_diff_file("utils.py", added=["def helper():", "    return 2"])]
    result = run_static_analysis(diff_files, graph_runtime=runtime, graph_store=store)
    annotation = result.risk_annotations["utils.py"]
    assert annotation.fan_in >= 1
