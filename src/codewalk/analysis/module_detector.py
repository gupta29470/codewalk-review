"""Automatic module/package boundary detection from directory structure.

Algorithm (preserved exactly from the reference implementation -- see
`_find_source_root` / `_find_module_depth` docstrings for the precise rules):

  1. Strip common "wrapper" directories (src/, lib/, app/, ...) that don't
     represent real module boundaries.
  2. Scan directory depths 1-5 to find the level where child folder names
     start repeating across different parents (signals internal structure
     like `bloc/`, `ui/` inside every feature) -- the level just above that
     repetition is the module boundary.
  3. Assign every file to a module name based on that depth.
  4. Derive a module-level dependency graph from the file-level one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from codewalk.ingestion.scanner import ScannedFile
from codewalk.log import get_logger

logger = get_logger(__name__)

_WRAPPER_DIRS = frozenset(
    {"src", "lib", "app", "source", "packages", "pkg", "internal", "cmd", "main"}
)
_MAX_DEPTH = 5
_MAX_MODULES_BEFORE_FALLBACK = 20


def _find_source_root(file_paths: list[str]) -> str:
    """Find wrapper directories to strip before detecting modules.

    Walks down the directory tree, stripping one level at a time if 50%+ of
    files share the same prefix AND it's a known wrapper name, OR it's the
    only subdirectory present (single-child collapse).

    Returns:
        The prefix to strip, e.g. "src/codewalk". Empty string if nothing
        should be stripped.
    """
    prefix_parts: list[str] = []
    remaining = list(file_paths)

    for _ in range(_MAX_DEPTH):
        dir_counts: Counter[str] = Counter()
        file_with_dirs = 0

        for file_path in remaining:
            parts = file_path.split("/")
            if len(parts) > 1:
                dir_counts[parts[0]] += 1
                file_with_dirs += 1

        if not dir_counts or file_with_dirs == 0:
            break

        top_dir, top_count = dir_counts.most_common(1)[0]
        wrapper = top_dir.lower() in _WRAPPER_DIRS
        single = len(dir_counts) == 1

        if (top_count / file_with_dirs >= 0.5 and wrapper) or single:
            prefix_parts.append(top_dir)
            prefix = "/".join(prefix_parts) + "/"
            remaining = [fp[len(prefix) :] for fp in remaining if fp.startswith(prefix)]
        else:
            break

    return "/".join(prefix_parts)


def _find_module_depth(file_paths: list[str], source_root: str) -> int:
    """Find the directory depth that represents the module boundary.

    Scans depths 1-5 looking for the level where child folder names start
    repeating across different parent directories (>50% shared). That
    repetition signals internal structure, so the level at which it first
    appears is used as the module boundary.

    Returns:
        Depth (number of path components from `source_root`) to use as the
        module name. Falls back to 1 if no depth ever has >=3 unique groups.
    """
    stripped = []
    for fp in file_paths:
        if source_root and fp.startswith(source_root + "/"):
            stripped.append(fp[len(source_root) + 1 :])
        else:
            stripped.append(fp)

    best_depth = 1

    for depth in range(1, _MAX_DEPTH + 1):
        names_at_depth = [
            "/".join(path.split("/")[:depth]) for path in stripped if len(path.split("/")) > depth
        ]
        if not names_at_depth:
            break

        unique = len(set(names_at_depth))
        cross_parent_repeat = _cross_parent_repeat_ratio(stripped, depth)

        if unique >= 3 and cross_parent_repeat > 0.5:
            best_depth = depth
            break
        if unique >= 3:
            best_depth = depth  # candidate; keep looking for a deeper boundary

    return best_depth


def _cross_parent_repeat_ratio(stripped_paths: list[str], depth: int) -> float:
    """Fraction of child folder names (at `depth`) that repeat under multiple parents."""
    parent_to_children: dict[str, set[str]] = defaultdict(set)
    for path in stripped_paths:
        parts = path.split("/")
        if len(parts) > depth + 1:
            parent = "/".join(parts[:depth])
            parent_to_children[parent].add(parts[depth])

    if len(parent_to_children) < 2:
        return 0.0

    all_children: list[str] = []
    for children in parent_to_children.values():
        all_children.extend(children)
    child_counts = Counter(all_children)
    repeated = sum(1 for count in child_counts.values() if count >= 2)
    total_unique = len(child_counts)
    return repeated / total_unique if total_unique else 0.0


@dataclass
class ModuleInfo:
    files: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    file_count: int = 0


def _assign_modules(
    files: list[ScannedFile], source_root: str, module_depth: int
) -> dict[str, ModuleInfo]:
    """Assign files to modules based on `source_root` and `module_depth`."""
    modules: dict[str, ModuleInfo] = {}
    language_counters: dict[str, Counter[str]] = defaultdict(Counter)

    for file_info in files:
        file_path = file_info.file_path

        if source_root and file_path.startswith(source_root + "/"):
            relative_path = file_path[len(source_root) + 1 :]
            depth = module_depth
        else:
            relative_path = file_path
            depth = 1  # files outside the source root always use depth 1

        parts = relative_path.split("/")
        if len(parts) > depth:
            module_name = "/".join(parts[:depth])
        elif len(parts) > 1:
            module_name = parts[0]
        else:
            module_name = "root"

        module = modules.setdefault(module_name, ModuleInfo())
        module.files.append(file_path)
        module.file_count += 1
        language_counters[module_name][file_info.language] += 1

    for module_name, module in modules.items():
        module.languages = dict(language_counters[module_name])

    return modules


@dataclass
class ModuleDetectionStats:
    total_modules: int
    total_files: int


@dataclass
class ModuleDetectionResult:
    source_root: str
    modules: dict[str, ModuleInfo]
    module_graph: dict[str, list[str]]
    stats: ModuleDetectionStats


def _build_module_graph(
    modules: dict[str, ModuleInfo], file_graph: dict[str, list[str]] | None
) -> dict[str, list[str]]:
    if not file_graph:
        return {module_name: [] for module_name in modules}

    file_to_module = {
        file_path: module_name for module_name, info in modules.items() for file_path in info.files
    }

    module_graph: dict[str, list[str]] = {}
    for module_name, info in modules.items():
        deps: set[str] = set()
        for file_path in info.files:
            for target in file_graph.get(file_path, []):
                target_module = file_to_module.get(target)
                if target_module and target_module != module_name:
                    deps.add(target_module)
        module_graph[module_name] = sorted(deps)
    return module_graph


def detect_modules(
    files: list[ScannedFile], dep_graph: dict[str, list[str]] | None = None
) -> ModuleDetectionResult:
    """Group files into logical modules and build a module-level dependency graph.

    Args:
        files: Scanned files (e.g. from `ingestion.scanner.scan_repo`).
        dep_graph: The file-level import graph from
            `dependency_graph.build_dependency_graph().graph`, if available.

    Returns:
        A `ModuleDetectionResult` with the detected source root, per-module
        file/language breakdown, and a module-to-module dependency graph.
    """
    file_paths = [f.file_path for f in files]

    source_root = _find_source_root(file_paths)
    module_depth = _find_module_depth(file_paths, source_root)
    modules = _assign_modules(files, source_root, module_depth)

    # Safety net: an overly aggressive depth can explode into too many
    # "modules" on unusual repo layouts -- fall back to depth 1.
    if len(modules) > _MAX_MODULES_BEFORE_FALLBACK and module_depth > 1:
        logger.warning(
            "too many modules (%d) at depth %d, falling back to depth 1", len(modules), module_depth
        )
        module_depth = 1
        modules = _assign_modules(files, source_root, module_depth)

    module_graph = _build_module_graph(modules, dep_graph)

    logger.info(
        "detected %d modules from %d files (source root: %s)",
        len(modules),
        len(files),
        source_root or "none",
    )
    return ModuleDetectionResult(
        source_root=source_root,
        modules=modules,
        module_graph=module_graph,
        stats=ModuleDetectionStats(total_modules=len(modules), total_files=len(files)),
    )
