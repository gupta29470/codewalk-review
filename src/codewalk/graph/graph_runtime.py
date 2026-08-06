"""igraph-based fast graph operations: cycles, centrality, shortest paths.

Loads edges from `GraphStore` (DuckDB) into `igraph` (C-speed). Rebuilt on
every startup/refresh -- milliseconds even for large codebases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import igraph as ig

from codewalk.graph.graph_store import GraphStore
from codewalk.log import get_logger

logger = get_logger(__name__)


@dataclass
class CycleReport:
    has_cycles: bool = False
    cycle_groups: list[list[str]] = field(default_factory=list)
    edges_to_break: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CentralityEntry:
    file: str
    score: float


@dataclass
class CentralityReport:
    betweenness: list[CentralityEntry] = field(default_factory=list)
    pagerank: list[CentralityEntry] = field(default_factory=list)


@dataclass
class GraphInfo:
    vertices: int
    edges: int
    is_dag: bool


@dataclass
class RuntimeStats:
    file_graph: GraphInfo
    module_graph: GraphInfo


def _build_graph(edges: list[tuple[str, str]]) -> ig.Graph:
    """Build a directed igraph from (source, target) tuples."""
    if not edges:
        return ig.Graph(directed=True)
    return ig.Graph.TupleList(edges, directed=True)


def _find_vertex(graph: ig.Graph, name: str) -> int | None:
    """Find a vertex index by name. Returns None if not present."""
    try:
        result: int = graph.vs.find(name=name).index
        return result
    except ValueError:
        return None


class GraphRuntime:
    """In-memory igraph view of a `GraphStore`'s file- and module-level edges.

    Two separate graphs:
        file_graph   -- file-level import edges (a.py -> b.py means a imports b)
        module_graph -- module-level dependency edges
    """

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.file_graph: ig.Graph = _build_graph(store.get_import_edges())
        self.module_graph: ig.Graph = _build_graph(store.get_module_dep_edges())
        logger.info(
            "built file_graph (%d vertices, %d edges), module_graph (%d vertices, %d edges)",
            self.file_graph.vcount(),
            self.file_graph.ecount(),
            self.module_graph.vcount(),
            self.module_graph.ecount(),
        )

    def rebuild(self) -> None:
        """Rebuild both graphs from the underlying `GraphStore`. Call after re-analysis."""
        self.file_graph = _build_graph(self.store.get_import_edges())
        self.module_graph = _build_graph(self.store.get_module_dep_edges())
        logger.info(
            "rebuilt file_graph (%d vertices, %d edges), module_graph (%d vertices, %d edges)",
            self.file_graph.vcount(),
            self.file_graph.ecount(),
            self.module_graph.vcount(),
            self.module_graph.ecount(),
        )

    def get_blast_radius(self, file_path: str) -> list[str]:
        """All files affected if `file_path` changes (transitive reverse deps).

        Returns an empty list if `file_path` isn't in the graph (e.g. it has
        no import edges at all) rather than raising.
        """
        start = _find_vertex(self.file_graph, file_path)
        if start is None:
            return []
        affected_indices = self.file_graph.neighborhood(start, order=999, mode="in")
        return [self.file_graph.vs[idx]["name"] for idx in affected_indices if idx != start]

    def topological_sort(self) -> list[str]:
        """Files in dependency order (leaf dependencies first).

        igraph only contains files that appear in at least one import edge.
        Files with zero import relationships are appended at the end (sorted)
        so every indexed file is included, not just connected ones.
        """
        if self.file_graph.vcount() == 0:
            return sorted(self.store.get_all_files())

        sorted_files = self._topological_order_or_fallback()

        all_files = self.store.get_all_files()
        graph_files = set(sorted_files)
        orphans = sorted(f for f in all_files if f not in graph_files)
        return sorted_files + orphans

    def _topological_order_or_fallback(self) -> list[str]:
        if not self.file_graph.is_dag():
            logger.warning("cycle detected in file_graph -- using in-degree sort fallback")
            degrees = self.file_graph.indegree()
            order = sorted(range(len(degrees)), key=lambda i: degrees[i])
        else:
            # mode="in": for edge A->B (A imports B), B comes first --
            # dependencies before dependents, i.e. correct reading order.
            order = self.file_graph.topological_sorting(mode="in")
        return [self.file_graph.vs[index]["name"] for index in order]

    def detect_cycles(self) -> CycleReport:
        """Detect circular import dependencies in the file graph."""
        if self.file_graph.vcount() == 0 or self.file_graph.is_dag():
            return CycleReport()

        components = self.file_graph.components(mode="STRONG")
        cycle_groups = [
            [self.file_graph.vs[index]["name"] for index in group]
            for group in components
            if len(group) > 1  # single-vertex components aren't cycles
        ]

        feedback_arcs = self.file_graph.feedback_arc_set()
        edges_to_break = [
            (
                self.file_graph.vs[self.file_graph.es[e].source]["name"],
                self.file_graph.vs[self.file_graph.es[e].target]["name"],
            )
            for e in feedback_arcs
        ]

        return CycleReport(
            has_cycles=True, cycle_groups=cycle_groups, edges_to_break=edges_to_break
        )

    def centrality(self, top_n: int = 10) -> CentralityReport:
        """Top files by betweenness and PageRank centrality."""
        if self.file_graph.vcount() == 0:
            return CentralityReport()

        names = self.file_graph.vs["name"]
        betweenness = self.file_graph.betweenness()
        pagerank = self.file_graph.pagerank()

        top_betweenness = sorted(
            zip(names, betweenness, strict=True), key=lambda x: x[1], reverse=True
        )
        top_pagerank = sorted(zip(names, pagerank, strict=True), key=lambda x: x[1], reverse=True)

        return CentralityReport(
            betweenness=[CentralityEntry(f, round(s, 4)) for f, s in top_betweenness[:top_n]],
            pagerank=[CentralityEntry(f, round(s, 6)) for f, s in top_pagerank[:top_n]],
        )

    def shortest_path(self, source: str, target: str) -> list[str]:
        """Shortest import chain from `source` to `target` file.

        Returns an empty list if either endpoint is missing from the graph,
        or no path exists between them.
        """
        source_index = _find_vertex(self.file_graph, source)
        target_index = _find_vertex(self.file_graph, target)
        if source_index is None or target_index is None:
            return []

        paths = self.file_graph.get_shortest_paths(source_index, target_index)
        if not paths or not paths[0]:
            return []
        return [self.file_graph.vs[index]["name"] for index in paths[0]]

    def get_graph_stats(self) -> RuntimeStats:
        return RuntimeStats(
            file_graph=GraphInfo(
                vertices=self.file_graph.vcount(),
                edges=self.file_graph.ecount(),
                is_dag=self.file_graph.is_dag() if self.file_graph.vcount() > 0 else True,
            ),
            module_graph=GraphInfo(
                vertices=self.module_graph.vcount(),
                edges=self.module_graph.ecount(),
                is_dag=self.module_graph.is_dag() if self.module_graph.vcount() > 0 else True,
            ),
        )
