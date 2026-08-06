"""Blast-radius analysis: BFS over the reversed dependency graph to answer
"if I change file X, what else is affected?".

This module operates purely on the plain `dict[str, list[str]]` dependency
graph (from `analysis.dependency_graph.build_dependency_graph().graph`) --
no DuckDB/igraph dependency. `graph.graph_runtime.GraphRuntime` (Phase 4)
may offer a faster igraph-backed path later; it reuses `calculate_risk_level`
from here so risk thresholds stay consistent across both.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# Path substrings identifying test-only, story-only, or mock consumers.
# Excluded from runtime blast-radius by default -- they aren't production
# downstream usage.
_TEST_STORY_PATTERNS = (
    ".test.",
    ".spec.",
    ".stories.",
    ".cy.",
    "/__fixtures__/",
    "/__mocks__/",
    "/test/",
    "/tests/",
)


def _is_test_or_story(path: str) -> bool:
    lower = path.lower()
    return any(pattern in lower for pattern in _TEST_STORY_PATTERNS)


def calculate_risk_level(affected: int, total: int) -> str:
    """Risk level from affected-file count and ratio to total files.

    critical -- >50% OR 20+ files
    high     -- >25% OR 10+ files
    moderate -- >10% OR 4+ files
    low      -- everything else
    none     -- total is 0 (empty graph)
    """
    if total == 0:
        return "none"
    ratio = affected / total
    if ratio > 0.5 or affected >= 20:
        return "critical"
    if ratio > 0.25 or affected >= 10:
        return "high"
    if ratio > 0.10 or affected >= 4:
        return "moderate"
    return "low"


def build_reverse_graph(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    """Reverse the dependency graph: edges go from "imported" -> "importer".

    Forward:  pipeline.py -> [scanner.py, chunker.py]   (pipeline imports them)
    Reversed: scanner.py -> [pipeline.py]               (scanner is imported BY pipeline)
    """
    internal_files = set(graph.keys())
    reverse: dict[str, list[str]] = {file: [] for file in internal_files}
    for file, deps in graph.items():
        for dep in deps:
            if dep in internal_files:
                reverse[dep].append(file)
    return reverse


def _bfs_impact_tree(target_file: str, reverse: dict[str, list[str]]) -> dict[str, int]:
    """BFS from `target_file` through the reverse graph -> {file: distance}."""
    visited = {target_file}
    queue: deque[tuple[str, int]] = deque()
    impact_tree: dict[str, int] = {}

    for dependent in reverse.get(target_file, []):
        if dependent not in visited:
            queue.append((dependent, 1))
            visited.add(dependent)

    while queue:
        current_file, depth = queue.popleft()
        impact_tree[current_file] = depth
        for dependent in reverse.get(current_file, []):
            if dependent not in visited:
                queue.append((dependent, depth + 1))
                visited.add(dependent)

    return impact_tree


def _filter_impact_tree(
    impact_tree: dict[str, int], exclude_test_stories: bool
) -> tuple[dict[str, int], list[str], list[str]]:
    """Return (filtered impact_tree, sorted direct deps, sorted transitive deps)."""
    filtered = (
        {file: d for file, d in impact_tree.items() if not _is_test_or_story(file)}
        if exclude_test_stories
        else dict(impact_tree)
    )
    direct = sorted(file for file, d in filtered.items() if d == 1)
    transitive = sorted(file for file, d in filtered.items() if d > 1)
    return filtered, direct, transitive


@dataclass
class BlastRadius:
    file: str
    direct: list[str] = field(default_factory=list)
    transitive: list[str] = field(default_factory=list)
    affected_files: int = 0
    risk_level: str = "none"
    impact_tree: dict[str, int] = field(default_factory=dict)


def get_blast_radius(
    target_file: str, graph: dict[str, list[str]], exclude_test_stories: bool = True
) -> BlastRadius:
    """Calculate the blast radius (downstream impact) for a single file."""
    internal_files = set(graph.keys())
    if target_file not in internal_files:
        return BlastRadius(file=target_file)

    reverse = build_reverse_graph(graph)
    raw_impact_tree = _bfs_impact_tree(target_file, reverse)
    impact_tree, direct, transitive = _filter_impact_tree(raw_impact_tree, exclude_test_stories)
    risk_level = calculate_risk_level(len(impact_tree), len(internal_files))

    return BlastRadius(
        file=target_file,
        direct=direct,
        transitive=transitive,
        affected_files=len(impact_tree),
        risk_level=risk_level,
        impact_tree=impact_tree,
    )


@dataclass
class BlastMapEntry:
    file: str
    affected_files: int
    risk_level: str
    direct_count: int
    transitive_count: int


@dataclass
class BlastMapStats:
    total_files: int
    critical_files: int
    high_files: int
    moderate_files: int
    low_files: int


@dataclass
class BlastMap:
    blast_map: list[BlastMapEntry]
    stats: BlastMapStats
    highest_risk: str


def calculate_full_blast_map(graph: dict[str, list[str]]) -> BlastMap:
    """Blast radius for every file in the graph, ranked by impact (descending)."""
    reverse = build_reverse_graph(graph)
    internal_files = set(graph.keys())
    total_files = len(internal_files)

    risk_counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0, "none": 0}
    entries: list[BlastMapEntry] = []

    for target_file in graph:
        raw_impact_tree = _bfs_impact_tree(target_file, reverse)
        impact_tree, direct, transitive = _filter_impact_tree(
            raw_impact_tree, exclude_test_stories=True
        )
        risk_level = calculate_risk_level(len(impact_tree), total_files)
        risk_counts[risk_level] = risk_counts.get(risk_level, 0) + 1
        entries.append(
            BlastMapEntry(
                file=target_file,
                affected_files=len(impact_tree),
                risk_level=risk_level,
                direct_count=len(direct),
                transitive_count=len(transitive),
            )
        )

    entries.sort(key=lambda entry: entry.affected_files, reverse=True)
    highest_risk = entries[0].file if entries else ""

    return BlastMap(
        blast_map=entries,
        stats=BlastMapStats(
            total_files=total_files,
            critical_files=risk_counts["critical"],
            high_files=risk_counts["high"],
            moderate_files=risk_counts["moderate"],
            low_files=risk_counts["low"],
        ),
        highest_risk=highest_risk,
    )
