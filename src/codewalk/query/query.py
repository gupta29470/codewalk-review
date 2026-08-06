"""Deterministic query/explain logic backing the MCP query tools.

No ChromaDB, no LLM calls. Symbol lookup uses the DuckDB `symbols` table
(exact-name, then substring fallback for suggestions); code snippets are read
live from disk using the graph snapshot's line ranges (DuckDB never stores
code text). Every function raises `GraphNotBuiltError` if the graph snapshot
is empty, and `ValueError` (with near-miss suggestions) for an unknown
symbol/module/file, rather than returning an ad-hoc "not found" string --
callers format those consistently at the MCP boundary.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from codewalk.analysis.blast_radius import calculate_full_blast_map, get_blast_radius
from codewalk.analysis.module_detector import ModuleInfo
from codewalk.analysis.reading_order import generate_reading_order
from codewalk.errors import GraphNotBuiltError
from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore, SymbolMatch
from codewalk.ingestion.tech_detect import detect_tech_stack
from codewalk.paths import resolve_within_repo

_MAX_CODE_SNIPPET_LINES = 400
_MAX_SUGGESTIONS = 5


def short_name(path: str) -> str:
    """Filename from a repo-relative path: 'src/foo/bar.py' -> 'bar.py'."""
    return path.rsplit("/", 1)[-1]


def _project_of(path: str) -> str:
    """Top-level directory of a repo-relative path, or 'root' if there isn't one."""
    parts = path.split("/", 1)
    return parts[0] if len(parts) > 1 else "root"


def _group_by_project(paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        groups[_project_of(p)].append(short_name(p))
    return dict(groups)


def _require_nonempty_graph(store: GraphStore) -> None:
    if store.get_stats().files == 0:
        raise GraphNotBuiltError(
            "no files in the graph snapshot -- run codewalk_analyze_codebase first"
        )


def file_import_graph(store: GraphStore) -> dict[str, list[str]]:
    """Reconstruct the plain `{file: [imported_files]}` dict from DuckDB.

    Includes every known file as a key (even with an empty edge list), which
    `analysis.blast_radius` and `analysis.reading_order` both expect.
    """
    graph: dict[str, list[str]] = {f: [] for f in store.get_all_files()}
    for source, target in store.get_import_edges():
        graph.setdefault(source, []).append(target)
    return graph


def module_dependency_graph(
    store: GraphStore, modules: dict[str, ModuleInfo]
) -> dict[str, list[str]]:
    """`{module: [modules it depends on]}`, including modules with no edges."""
    graph: dict[str, list[str]] = {name: [] for name in modules}
    for source, target in store.get_module_dep_edges():
        graph.setdefault(source, []).append(target)
    return graph


def resolve_module_name(modules: dict[str, ModuleInfo], module_name: str) -> str | None:
    """Case-insensitive module name lookup. Returns the actual stored name, or None."""
    for name in modules:
        if name.lower() == module_name.lower():
            return name
    return None


def _module_suggestions(modules: dict[str, ModuleInfo]) -> str:
    return ", ".join(sorted(modules)) if modules else "(no modules detected)"


def resolve_module_with_fallback(
    modules: dict[str, ModuleInfo], module_name: str
) -> tuple[str, ModuleInfo] | None:
    """Module lookup with sub-folder ("feature") fallback.

    If `module_name` isn't a top-level module, look for it as a sub-folder
    inside any module's files (e.g. "auth" resolving to "features/auth/...").
    """
    actual_name = resolve_module_name(modules, module_name)
    if actual_name is not None:
        return actual_name, modules[actual_name]

    needle = f"/{module_name.lower()}/"
    for mod_name, info in modules.items():
        matching_files = [f for f in info.files if needle in f"/{f.lower()}"]
        if matching_files:
            languages: Counter[str] = Counter()
            # Best-effort per-file language: we don't have it without a file
            # scan, so the feature-level result simply omits a breakdown.
            return mod_name, ModuleInfo(
                files=matching_files, languages=dict(languages), file_count=len(matching_files)
            )
    return None


def _read_code_snippet(repo_root: Path, file_path: str, start_line: int, end_line: int) -> str:
    """Read `file_path`'s `[start_line, end_line]` (1-indexed, inclusive).

    Clamps to the file's actual current length rather than raising -- the
    graph snapshot's line numbers can be stale if the file changed since the
    last build (see `Workspace.staleness_warning()`).
    """
    try:
        full_path = resolve_within_repo(repo_root, file_path)
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(source file could not be read -- it may have moved or been deleted)"

    if not lines:
        return "(file is empty)"

    start = max(1, start_line)
    end = min(len(lines), end_line, start + _MAX_CODE_SNIPPET_LINES)
    if start > len(lines):
        return "(recorded line range is past the end of the current file -- it may be stale)"
    return "\n".join(lines[start - 1 : end])


def _suggestions_for(store: GraphStore, name: str) -> list[SymbolMatch]:
    return store.find_symbols_containing(name, limit=_MAX_SUGGESTIONS)


def _not_found_error(kind: str, name: str, suggestions: list[SymbolMatch]) -> ValueError:
    if suggestions:
        names = ", ".join(sorted({s.name for s in suggestions}))
        return ValueError(f"No {kind} named '{name}' found. Did you mean: {names}?")
    return ValueError(f"No {kind} named '{name}' found.")


def _explain_symbol_text(store: GraphStore, repo_root: Path, name: str, kind: str) -> str:
    """Shared implementation for `explain_function_text`/`explain_class_text`."""
    _require_nonempty_graph(store)

    matches = store.find_symbols_by_name(name)
    if not matches:
        raise _not_found_error(kind, name, _suggestions_for(store, name))

    primary = matches[0]
    lines = [
        f"## {primary.symbol_type.capitalize()}: {primary.name}",
        f"**Location:** {primary.file_path}:{primary.start_line}-{primary.end_line}",
    ]

    if len(matches) > 1:
        others = ", ".join(f"{m.file_path}:{m.start_line}" for m in matches[1:])
        lines.append(f"**Also found at:** {others}")

    lines.append("")
    lines.append("```")
    snippet = _read_code_snippet(repo_root, primary.file_path, primary.start_line, primary.end_line)
    lines.append(snippet)
    lines.append("```")

    graph = file_import_graph(store)
    if primary.file_path in graph:
        radius = get_blast_radius(primary.file_path, graph)
        direct = ", ".join(short_name(f) for f in radius.direct) or "none"
        transitive = ", ".join(short_name(f) for f in radius.transitive) or "none"
        lines.append("")
        lines.append("### Blast Radius")
        lines.append(
            f"**Risk:** {radius.risk_level.upper()} -- {radius.affected_files} files affected"
        )
        lines.append(f"**Direct:** {direct} | **Transitive:** {transitive}")

    callers = store.get_callers_of_symbol(primary.qualified_name)
    if callers:
        lines.append("")
        lines.append(f"### Called by ({len(callers)})")
        for caller in callers[:10]:
            lines.append(f"  - {caller.caller}() at {caller.file}:{caller.line}")

    callees = store.get_callees_of_symbol(primary.qualified_name)
    if callees:
        lines.append("")
        lines.append(f"### Calls ({len(callees)})")
        for callee in callees[:10]:
            lines.append(f"  - {callee.callee}() at {callee.file}:{callee.line}")

    return "\n".join(lines)


def explain_function_text(store: GraphStore, repo_root: Path, function_name: str) -> str:
    """Look up a function/method in the graph snapshot and explain it with
    blast radius + callers/callees.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: no symbol named `function_name` exists (with suggestions).
    """
    return _explain_symbol_text(store, repo_root, function_name, "function")


def explain_class_text(store: GraphStore, repo_root: Path, class_name: str) -> str:
    """Look up a class/type in the graph snapshot and explain it with blast
    radius + callers/callees. Same lookup logic as `explain_function_text`,
    exposed separately for clearer tool routing/error wording.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: no symbol named `class_name` exists (with suggestions).
    """
    return _explain_symbol_text(store, repo_root, class_name, "class")


def lookup_symbol_text(store: GraphStore, repo_root: Path, query: str) -> str:
    """Deterministic symbol lookup: exact name match, falling back to a
    substring match, showing each match's code snippet.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: nothing matched `query` at all.
    """
    _require_nonempty_graph(store)

    matches = store.find_symbols_by_name(query)
    if not matches:
        matches = store.find_symbols_containing(query, limit=_MAX_SUGGESTIONS)
    if not matches:
        raise ValueError(f"No symbols matched for: '{query}'")

    lines = [f"## Symbol Lookup: '{query}'\n"]
    for i, match in enumerate(matches, 1):
        header = f"### Result {i}: {match.file_path} | {match.symbol_type}: {match.name}"
        header += f" (lines {match.start_line}-{match.end_line})"
        lines.append(header)
        snippet = _read_code_snippet(repo_root, match.file_path, match.start_line, match.end_line)
        lines.append(f"```\n{snippet}\n```")

    return "\n\n".join(lines)


def _entry_and_core_modules(module_graph: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    depended_on: set[str] = set()
    for deps in module_graph.values():
        depended_on.update(deps)
    entry_modules = sorted(m for m in module_graph if m not in depended_on)

    dep_counts: Counter[str] = Counter()
    for deps in module_graph.values():
        dep_counts.update(deps)
    core_modules = [name for name, _ in dep_counts.most_common(3)]
    return entry_modules, core_modules


def overview_text(store: GraphStore, runtime: GraphRuntime, repo_root: Path) -> str:
    """Project overview: tech stack, modules, dependency flow, riskiest files.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
    """
    _require_nonempty_graph(store)

    tech = detect_tech_stack(repo_root)
    modules = store.get_modules()
    module_graph = module_dependency_graph(store, modules)
    graph = file_import_graph(store)

    blast_map = calculate_full_blast_map(graph)
    risky_lines = []
    for entry in blast_map.blast_map[:30]:
        if entry.affected_files == 0:
            continue
        radius = get_blast_radius(entry.file, graph)
        direct = ", ".join(short_name(f) for f in radius.direct)
        risky_lines.append(
            f"  [{entry.risk_level.upper()}] {short_name(entry.file)} -- "
            f"{entry.affected_files} affected | breaks: {direct}"
        )
    risky_section = "\n".join(risky_lines) if risky_lines else "  No high-risk files"

    module_lines = []
    for name, info in sorted(modules.items()):
        lang_str = ", ".join(f"{lang}({count})" for lang, count in sorted(info.languages.items()))
        module_lines.append(f"  - {name} ({info.file_count} files): {lang_str}")
    modules_section = "\n".join(module_lines) if module_lines else "  (no modules detected)"

    entry_modules, core_modules = _entry_and_core_modules(module_graph)
    flow_lines = []
    for mod_name in sorted(module_graph):
        deps = module_graph[mod_name]
        if deps:
            flow_lines.append(f"  {mod_name} -> {', '.join(deps)}")
        else:
            flow_lines.append(f"  {mod_name} -> (standalone, no dependencies)")
    flow_section = "\n".join(flow_lines)

    centrality = runtime.centrality(top_n=5)
    centrality_section = ""
    if centrality.pagerank:
        names = [short_name(item.file) for item in centrality.pagerank]
        centrality_section = (
            f"\n\n### Key Files (PageRank)\nMost important files by transitive "
            f"dependency weight:\n  {', '.join(names)}"
        )

    cycles = runtime.detect_cycles()
    cycle_section = ""
    if cycles.has_cycles:
        cycle_section = (
            f"\n\n### \u26a0 Circular Dependencies\n{len(cycles.cycle_groups)} cycle group(s) "
            f"detected. Run `codewalk_find_circular_dependencies` for details."
        )

    return (
        f"## Project Overview\n\n"
        f"**Tech Stack:** {', '.join(tech) if tech else 'Not detected'}\n"
        f"**Total Files:** {store.get_stats().files}\n"
        f"**Total Modules:** {len(modules)}\n\n"
        f"### Modules\n{modules_section}\n\n"
        f"### Module Dependency Flow\n"
        f"**Entry points** (nothing depends on these): {', '.join(entry_modules) or 'None'}\n"
        f"**Core modules** (most depended on): {', '.join(core_modules) or 'None'}\n\n"
        f"{flow_section}\n\n"
        f"### Riskiest Files\n{risky_section}{centrality_section}{cycle_section}"
    )


def blast_radius_map_text(store: GraphStore, target: str = "") -> str:
    """Blast radius report for a target module, file, or the top 30 riskiest files.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: `target` doesn't match any module or file.
    """
    _require_nonempty_graph(store)
    graph = file_import_graph(store)
    modules = store.get_modules()

    if not target:
        return _blast_radius_top_n(graph)

    resolved = resolve_module_with_fallback(modules, target)
    if resolved is not None:
        return _blast_radius_for_module(graph, *resolved)

    matched = [f for f in graph if short_name(f) == target or f.endswith(target)]
    if matched:
        return _blast_radius_for_files(graph, sorted(matched), f"file '{target}'")

    raise ValueError(
        f"'{target}' not found as a module or file. Available modules: "
        f"{_module_suggestions(modules)}. Tip: use an exact file name like 'scanner.py'."
    )


def _blast_radius_for_module(
    graph: dict[str, list[str]], module_name: str, info: ModuleInfo
) -> str:
    direct_set: set[str] = set()
    transitive_set: set[str] = set()
    for file_path in sorted(info.files):
        if file_path not in graph:
            continue
        radius = get_blast_radius(file_path, graph)
        direct_set.update(radius.direct)
        transitive_set.update(radius.transitive)
    transitive_set -= direct_set

    from codewalk.analysis.blast_radius import calculate_risk_level

    affected = len(direct_set | transitive_set)
    risk = calculate_risk_level(affected, len(graph))

    lines = [f"**Aggregate runtime blast radius for module `{module_name}`**"]
    all_affected = list(direct_set | transitive_set)
    lines.append(f"- **Projects affected:** {len(_group_by_project(all_affected))}")
    lines.append(f"- **Direct runtime dependents:** {len(direct_set)}")
    lines.append(f"- **Transitive runtime dependents:** {len(transitive_set)}")
    lines.append("")

    if direct_set:
        lines.append("### Direct dependents (by project)")
        for project, files in sorted(_group_by_project(list(direct_set)).items()):
            lines.append(f"- **{project}**: {', '.join(sorted(set(files)))}")
    if transitive_set:
        lines.append("\n### Transitive dependents (by project)")
        for project, files in sorted(_group_by_project(list(transitive_set)).items()):
            lines.append(f"- **{project}**: {', '.join(sorted(set(files)))}")
    if not direct_set and not transitive_set:
        lines.append("No runtime dependents found (test/story files excluded).")

    header = f"## Blast Radius -- module '{module_name}'\n**Overall risk:** {risk.upper()}\n"
    return header + "\n" + "\n".join(lines)


def _blast_radius_for_files(
    graph: dict[str, list[str]], target_files: list[str], scope: str
) -> str:
    entries = [(f, get_blast_radius(f, graph)) for f in target_files]
    max_risk = "none"
    risk_order = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "none": 0}
    for _, radius in entries:
        if risk_order[radius.risk_level] > risk_order[max_risk]:
            max_risk = radius.risk_level

    lines = []
    for file_path, radius in entries:
        short_path = "/".join(file_path.split("/")[-2:])
        if radius.affected_files > 0:
            breaks = f"breaks: {', '.join(short_name(f) for f in radius.direct)}"
            if radius.transitive:
                breaks += f" -> then: {', '.join(short_name(f) for f in radius.transitive)}"
            lines.append(
                f"  [{radius.risk_level.upper()}] {short_path} -- {radius.affected_files} "
                f"affected | {breaks}"
            )
        else:
            lines.append(f"  [SAFE] {short_path} -- no dependents")

    header = (
        f"## Blast Radius -- {scope}\n"
        f"**Overall risk:** {max_risk.upper()}\n"
        f"**Files shown:** {len(lines)}\n"
    )
    return header + "\n" + "\n".join(lines)


def _blast_radius_top_n(graph: dict[str, list[str]]) -> str:
    blast_map = calculate_full_blast_map(graph)
    top = [entry for entry in blast_map.blast_map if entry.affected_files > 0][:30]
    files = [entry.file for entry in top]
    return _blast_radius_for_files(graph, files, "top 30 riskiest")


def find_circular_dependencies_text(runtime: GraphRuntime) -> str:
    """Detect circular import dependencies in the file graph."""
    report = runtime.detect_cycles()
    if not report.has_cycles:
        return "No circular dependencies detected."

    lines = [f"## Circular Dependencies\n{len(report.cycle_groups)} cycle group(s) detected.\n"]
    for i, group in enumerate(report.cycle_groups, 1):
        lines.append(f"### Cycle {i} ({len(group)} files)")
        lines.append(", ".join(sorted(group)))
    if report.edges_to_break:
        lines.append("\n### Suggested edges to break")
        for source, target in report.edges_to_break:
            lines.append(f"  - {source} -> {target}")
    return "\n".join(lines)


def reading_order_text(store: GraphStore, module_name: str = "") -> str:
    """Deterministic reading order: all files (or one module's files) in
    dependency order, annotated with blast-radius risk.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: `module_name` doesn't match any module.
    """
    _require_nonempty_graph(store)
    graph = file_import_graph(store)
    result = generate_reading_order(graph)

    items = result.order
    scope = "entire repo"
    if module_name:
        modules = store.get_modules()
        actual_name = resolve_module_name(modules, module_name)
        if actual_name is None:
            raise ValueError(
                f"Module '{module_name}' not found. Available: {_module_suggestions(modules)}"
            )
        module_files = set(modules[actual_name].files)
        items = [item for item in items if item.file in module_files]
        scope = f"module '{actual_name}'"

    lines = []
    for item in items:
        radius = get_blast_radius(item.file, graph)
        lines.append(
            f"{item.position}. [{radius.risk_level.upper()}] {item.file} "
            f"({radius.affected_files} affected) -- {item.why}"
        )

    header = f"## Reading Order -- {scope} ({len(items)} files)"
    return header + "\n" + "\n".join(lines)


def execution_flow_text(store: GraphStore, module_name: str = "") -> str:
    """Execution flow: module-to-module (no arg) or file-to-file (module given).

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: `module_name` doesn't match any module.
    """
    _require_nonempty_graph(store)
    modules = store.get_modules()
    module_graph = module_dependency_graph(store, modules)

    if not module_name:
        entry_modules, _ = _entry_and_core_modules(module_graph)
        lines = []
        for mod_name in sorted(module_graph):
            deps = module_graph[mod_name]
            file_count = modules[mod_name].file_count if mod_name in modules else "?"
            if deps:
                lines.append(f"  {mod_name} ({file_count} files) -> depends on: {', '.join(deps)}")
            else:
                lines.append(f"  {mod_name} ({file_count} files) -> (standalone)")
        return (
            f"## Execution Flow -- Module Level\n"
            f"**Entry modules** (nothing depends on these): {', '.join(entry_modules) or 'None'}\n"
            f"**Total modules:** {len(module_graph)}\n\n### Module Dependencies\n"
            + "\n".join(lines)
        )

    actual_name = resolve_module_name(modules, module_name)
    if actual_name is None:
        raise ValueError(
            f"Module '{module_name}' not found. Available: {_module_suggestions(modules)}"
        )

    graph = file_import_graph(store)
    module_file_set = set(modules[actual_name].files)
    target_files = sorted(f for f in graph if f in module_file_set)

    imported_in_module: set[str] = set()
    for fp in target_files:
        imported_in_module.update(d for d in graph.get(fp, []) if d in module_file_set)
    entry_files = [f for f in target_files if f not in imported_in_module]

    dep_lines = []
    for file_path in target_files:
        internal_deps = [d for d in graph.get(file_path, []) if d in graph]
        in_module = [d for d in internal_deps if d in module_file_set]
        cross_module = [d for d in internal_deps if d not in module_file_set]
        parts = []
        if in_module:
            parts.append(f"imports: {', '.join(short_name(d) for d in in_module)}")
        if cross_module:
            parts.append(f"external: {', '.join(short_name(d) for d in cross_module)}")
        detail = " | ".join(parts) if parts else "(no internal imports)"
        dep_lines.append(f"  {short_name(file_path)} -> {detail}")

    entry_names = [short_name(f) for f in entry_files]
    return (
        f"## Execution Flow -- {actual_name} (file level)\n"
        f"**Entry files** (nothing in this module imports these): {', '.join(entry_names)}\n"
        f"**Files:** {len(target_files)}\n\n### File Dependencies\n" + "\n".join(dep_lines)
    )


def architecture_health_text(store: GraphStore, runtime: GraphRuntime) -> str:
    """Overall architecture health: graph stats, bottlenecks, cycles,
    refactoring priorities (highest-blast-radius files).

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
    """
    _require_nonempty_graph(store)
    stats = runtime.get_graph_stats()
    centrality = runtime.centrality(top_n=5)
    cycles = runtime.detect_cycles()
    graph = file_import_graph(store)
    blast_map = calculate_full_blast_map(graph)

    lines = [
        "## Architecture Health",
        f"**Files:** {stats.file_graph.vertices} | **Import edges:** {stats.file_graph.edges}",
        f"**Modules:** {stats.module_graph.vertices} | "
        f"**Module edges:** {stats.module_graph.edges}",
        f"**Is DAG (no cycles):** {stats.file_graph.is_dag}",
        "",
    ]

    if centrality.betweenness:
        lines.append("### Bottleneck files (betweenness centrality)")
        for entry in centrality.betweenness:
            lines.append(f"  - {short_name(entry.file)}: {entry.score}")
        lines.append("")

    if cycles.has_cycles:
        lines.append(f"### \u26a0 {len(cycles.cycle_groups)} circular dependency group(s) detected")
    else:
        lines.append("### No circular dependencies")
    lines.append("")

    top_risk = [e for e in blast_map.blast_map if e.affected_files > 0][:10]
    if top_risk:
        lines.append("### Refactoring priorities (highest blast radius)")
        for risk_entry in top_risk:
            lines.append(
                f"  [{risk_entry.risk_level.upper()}] {short_name(risk_entry.file)} -- "
                f"{risk_entry.affected_files} affected"
            )

    return "\n".join(lines)


def call_chain_text(store: GraphStore, runtime: GraphRuntime, source: str, target: str) -> str:
    """Shortest file-level import chain from `source` to `target`.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: `source` or `target` doesn't match any known file.
    """
    _require_nonempty_graph(store)
    all_files = set(store.get_all_files())

    for label, name in (("source", source), ("target", target)):
        if name not in all_files:
            matches = [f for f in all_files if short_name(f) == short_name(name)]
            hint = ""
            if matches:
                hint = f" Did you mean: {', '.join(sorted(matches)[:_MAX_SUGGESTIONS])}?"
            raise ValueError(f"{label.capitalize()} file '{name}' not found in the graph.{hint}")

    if source == target:
        return f"'{source}' is its own trivial chain (source == target)."

    path = runtime.shortest_path(source, target)
    if not path:
        return f"No import chain found from '{source}' to '{target}'."

    return f"## Call Chain: {source} -> {target}\n" + " -> ".join(path)


def module_info_text(store: GraphStore, runtime: GraphRuntime, module_name: str) -> str:
    """Module details: files, languages, dependencies, hub files, coupling.

    Raises:
        GraphNotBuiltError: the graph snapshot is empty.
        ValueError: `module_name` doesn't match any module (or sub-folder).
    """
    _require_nonempty_graph(store)
    modules = store.get_modules()
    resolved = resolve_module_with_fallback(modules, module_name)
    if resolved is None:
        raise ValueError(
            f"Module '{module_name}' not found. Available: {_module_suggestions(modules)}"
        )
    actual_name, info = resolved

    module_graph = module_dependency_graph(store, modules)
    depends_on = module_graph.get(actual_name, [])
    depended_by = [other for other, deps in module_graph.items() if actual_name in deps]

    centrality = runtime.centrality(top_n=10)
    module_files = set(info.files)
    hub_files = [e for e in centrality.betweenness if e.file in module_files and e.score > 0]
    hub_section = ""
    if hub_files:
        names = [f"{short_name(h.file)} ({h.score})" for h in hub_files[:3]]
        hub_section = f"\n**Hub files:** {', '.join(names)}"

    outgoing = 0
    incoming = 0
    for file_path in info.files:
        outgoing += sum(1 for imp in store.get_imports(file_path) if imp not in module_files)
        incoming += sum(1 for imp in store.get_importers(file_path) if imp not in module_files)
    coupling_section = ""
    if outgoing or incoming:
        coupling_section = (
            f"\n**Coupling:** {outgoing} outgoing, {incoming} incoming cross-module edges"
        )

    file_names = [short_name(p) for p in sorted(info.files)]
    lang_items = sorted(info.languages.items())
    lang_str = ", ".join(f"{lang} ({count} files)" for lang, count in lang_items)

    return (
        "\n".join(
            [
                f"## Module: {actual_name}",
                f"**Files ({info.file_count}):** {', '.join(file_names)}",
                f"**Languages:** {lang_str or 'unknown'}",
                f"**Depends on:** {', '.join(depends_on) or 'None (standalone)'}",
                f"**Depended on by:** {', '.join(depended_by) or 'None'}",
            ]
        )
        + hub_section
        + coupling_section
    )
