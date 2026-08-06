"""Deterministic, graph-based risk annotations for changed files in a review.

Given already-parsed diff files and (optionally) a built `GraphRuntime` /
`GraphStore` for the repo, computes a risk score per changed file from graph
centrality, blast radius, and cycle membership. When no graph is available
yet (e.g. first-ever review before a graph has been built), degrades
gracefully to a diff-size-based proxy instead of raising.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore
from codewalk.log import get_logger
from codewalk.review.diff_parser import DiffFile

logger = get_logger(__name__)

# Diff size (added + removed lines) is capped when used as a fan-in proxy, so
# one enormous file can't single-handedly dominate every percentile threshold.
_DIFF_SIZE_FAN_IN_CAP = 50


@dataclass
class RiskAnnotation:
    """Pre-computed architecture risk signals for one changed file."""

    file_path: str
    risk_score: float
    fan_in: int = 0
    pagerank: float = 0.0
    cycle_participation: bool = False
    is_bottleneck: bool = False
    affected_files: list[str] = field(default_factory=list)
    is_high_fan_in: bool = False
    is_high_pagerank: bool = False

    def to_prompt_text(self) -> str:
        """One-line human-readable summary, or "" if nothing noteworthy."""
        parts = []
        if self.affected_files:
            parts.append(f"{len(self.affected_files)} affected file(s)")
        if self.is_high_fan_in:
            parts.append(f"{self.fan_in} direct callers")
        if self.is_high_pagerank:
            parts.append(f"PageRank {self.pagerank:.2f}")
        if self.cycle_participation:
            parts.append("in circular dependency")
        if self.is_bottleneck:
            parts.append("architectural bottleneck")
        if not parts:
            return ""
        summary = ", ".join(parts)
        return f"HIGH BLAST RADIUS ({self.file_path}): {summary}. Review with extra care."


@dataclass
class StaticAnalysisResult:
    """Output of the deterministic static analysis layer."""

    diff_files: list[DiffFile] = field(default_factory=list)
    risk_annotations: dict[str, RiskAnnotation] = field(default_factory=dict)
    total_added: int = 0
    total_removed: int = 0


def _percentile_threshold(values: list[float], percentile: float) -> float:
    """Return the value at `percentile` (0-100). Empty input returns +inf (nothing qualifies)."""
    if not values:
        return float("inf")
    sorted_values = sorted(values)
    idx = int(len(sorted_values) * percentile / 100)
    return sorted_values[min(idx, len(sorted_values) - 1)]


def _diff_size_fan_in(diff_file: DiffFile) -> int:
    return min(diff_file.added_lines + diff_file.removed_lines, _DIFF_SIZE_FAN_IN_CAP)


def _fan_in_from_graph_store(graph_store: GraphStore, file_path: str) -> int:
    """Best-effort direct-caller count via symbol call edges."""
    fan_in = 0
    for symbol in graph_store.get_symbols_in_file(file_path):
        callers = graph_store.get_callers_of_symbol(symbol.qualified_name)
        fan_in = max(fan_in, len(callers))
    return fan_in


def _compute_risk_annotations(
    diff_files: list[DiffFile],
    graph_runtime: GraphRuntime | None,
    graph_store: GraphStore | None,
) -> dict[str, RiskAnnotation]:
    if graph_runtime is None:
        logger.info("no graph available -- risk annotations use a diff-size proxy only")

    cycle_files: set[str] = set()
    pagerank_by_file: dict[str, float] = {}
    betweenness_by_file: dict[str, float] = {}
    all_pagerank: list[float] = []
    all_betweenness: list[float] = []

    if graph_runtime is not None:
        cycle_report = graph_runtime.detect_cycles()
        for group in cycle_report.cycle_groups:
            cycle_files.update(group)

        if graph_runtime.file_graph.vcount() > 0:
            names = graph_runtime.file_graph.vs["name"]
            all_pagerank = graph_runtime.file_graph.pagerank()
            all_betweenness = graph_runtime.file_graph.betweenness()
            pagerank_by_file = dict(zip(names, all_pagerank, strict=True))
            betweenness_by_file = dict(zip(names, all_betweenness, strict=True))

    pagerank_threshold = _percentile_threshold(all_pagerank, 90)
    betweenness_threshold = _percentile_threshold(all_betweenness, 90)

    # First pass: fan-in + affected files, so the fan-in threshold is relative to this diff.
    file_data: list[tuple[DiffFile, int, list[str]]] = []
    for df in diff_files:
        affected_files: list[str] = []
        fan_in = 0

        if graph_runtime is not None:
            affected_files = graph_runtime.get_blast_radius(df.file_path)
            fan_in = len(affected_files)

        if fan_in == 0 and graph_store is not None:
            fan_in = _fan_in_from_graph_store(graph_store, df.file_path)

        if fan_in == 0:
            fan_in = _diff_size_fan_in(df)

        file_data.append((df, fan_in, affected_files))

    fan_in_threshold = _percentile_threshold([fan_in for _, fan_in, _ in file_data], 75)

    annotations: dict[str, RiskAnnotation] = {}
    for df, fan_in, affected_files in file_data:
        pagerank = pagerank_by_file.get(df.file_path, min(fan_in / 500.0, 1.0))
        betweenness = betweenness_by_file.get(df.file_path, 0.0)

        score = (
            math.log(fan_in + 1) * 2.0
            + pagerank * 3.0
            + math.log(df.added_lines + df.removed_lines + 1) * 1.5
        )

        annotations[df.file_path] = RiskAnnotation(
            file_path=df.file_path,
            risk_score=score,
            fan_in=fan_in,
            pagerank=pagerank,
            cycle_participation=df.file_path in cycle_files,
            is_bottleneck=betweenness >= betweenness_threshold,
            affected_files=affected_files,
            is_high_fan_in=fan_in > fan_in_threshold,
            is_high_pagerank=pagerank >= pagerank_threshold,
        )

    return annotations


def run_static_analysis(
    diff_files: list[DiffFile],
    graph_runtime: GraphRuntime | None = None,
    graph_store: GraphStore | None = None,
) -> StaticAnalysisResult:
    """Compute deterministic risk annotations for already-parsed diff files."""
    total_added = sum(df.added_lines for df in diff_files)
    total_removed = sum(df.removed_lines for df in diff_files)

    risk_annotations = _compute_risk_annotations(diff_files, graph_runtime, graph_store)

    return StaticAnalysisResult(
        diff_files=diff_files,
        risk_annotations=risk_annotations,
        total_added=total_added,
        total_removed=total_removed,
    )
