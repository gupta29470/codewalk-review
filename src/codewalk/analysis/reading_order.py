"""Deterministic file reading order via topological sort of the dependency graph.

No LLM relevance tagging -- the host LLM (driving the review/query MCP
tools) does that reasoning itself from this deterministic ordering plus the
"why" annotations below.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

_WHITE, _GRAY, _BLACK = 0, 1, 2


def has_cycle(graph: dict[str, list[str]]) -> bool:
    """True if the internal (repo-relative) files in `graph` contain an import cycle.

    Uses a standard three-color DFS, restricted to internal files -- external/
    unresolved imports (raw strings not present as keys in `graph`) never
    contribute to a cycle since they have no outgoing edges we know about.
    """
    internal_files = set(graph.keys())
    color: dict[str, int] = dict.fromkeys(internal_files, _WHITE)

    def visit(node: str) -> bool:
        color[node] = _GRAY
        for dep in graph.get(node, []):
            if dep not in internal_files:
                continue
            if color[dep] == _GRAY:
                return True
            if color[dep] == _WHITE and visit(dep):
                return True
        color[node] = _BLACK
        return False

    return any(color[file] == _WHITE and visit(file) for file in internal_files)


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Sort files so dependencies come before dependents (Kahn's algorithm).

    Deterministic: ties are broken by sorting, so the same graph always
    produces the same order. Files involved in a circular dependency can't
    be fully ordered -- they're appended at the end (see `has_cycles` on the
    result of `generate_reading_order`).
    """
    internal_files = set(graph.keys())

    in_degree = {file: 0 for file in internal_files}
    dependents: dict[str, list[str]] = {file: [] for file in internal_files}

    for file, deps in graph.items():
        for dep in deps:
            if dep in internal_files:
                in_degree[file] += 1
                dependents[dep].append(file)

    queue = deque(sorted(file for file in internal_files if in_degree[file] == 0))
    result: list[str] = []

    while queue:
        current = queue.popleft()
        result.append(current)
        for dependent in sorted(dependents[current]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    remaining = [file for file in internal_files if file not in set(result)]
    result.extend(sorted(remaining))

    return result


@dataclass
class ReadingOrderItem:
    position: int
    file: str
    why: str


@dataclass
class ReadingOrderResult:
    order: list[ReadingOrderItem] = field(default_factory=list)
    total_files: int = 0
    has_cycles: bool = False


def generate_reading_order(graph: dict[str, list[str]]) -> ReadingOrderResult:
    """Generate a deterministic reading order from a dependency graph.

    Args:
        graph: the file-level import graph, e.g.
            `dependency_graph.build_dependency_graph(...).graph`.
    """
    sorted_files = topological_sort(graph)
    internal_files = set(graph.keys())
    cycles_present = has_cycle(graph)

    used_by: dict[str, list[str]] = {file: [] for file in internal_files}
    for file, file_deps in graph.items():
        for dep in file_deps:
            if dep in internal_files:
                used_by[dep].append(file.split("/")[-1])

    order = []
    for index, file_path in enumerate(sorted_files):
        deps_list = [dep for dep in graph.get(file_path, []) if dep in internal_files]
        users = used_by.get(file_path, [])

        if not deps_list:
            why = "No internal dependencies"
        else:
            dep_names = [dep.split("/")[-1] for dep in deps_list]
            why = f"Depends on: {', '.join(dep_names)}"
        if users:
            why += f" | Used by: {', '.join(users)}"

        order.append(ReadingOrderItem(position=index + 1, file=file_path, why=why))

    return ReadingOrderResult(order=order, total_files=len(sorted_files), has_cycles=cycles_present)
