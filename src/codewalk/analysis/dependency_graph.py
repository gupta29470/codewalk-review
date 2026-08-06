"""Dependency graph builder: extract imports/requires across 13 languages.

Per-file import extraction failures (unreadable file) are captured as
warnings on the result rather than raised -- one bad file must never abort
building the graph for the rest of the repo.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from codewalk.analysis.code_parser import get_parser_for_language
from codewalk.errors import ParseError
from codewalk.ingestion.scanner import ScannedFile
from codewalk.log import get_logger

logger = get_logger(__name__)

IMPORT_NODE_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"import_statement", "import_from_statement"}),
    "javascript": frozenset({"import_statement", "call_expression"}),
    "typescript": frozenset({"import_statement", "call_expression"}),
    "java": frozenset({"import_declaration"}),
    "go": frozenset({"import_declaration", "import_spec"}),
    "rust": frozenset({"use_declaration"}),
    "ruby": frozenset({"call"}),  # require() / require_relative()
    "php": frozenset({"namespace_use_declaration"}),
    "c": frozenset({"preproc_include"}),
    "cpp": frozenset({"preproc_include"}),
    "csharp": frozenset({"using_directive"}),
    "kotlin": frozenset({"import"}),
    "swift": frozenset({"import_declaration"}),
}


def extract_imports(file_path: Path, language: str) -> list[str]:
    """Parse a file with tree-sitter and extract all raw import strings.

    Returns a list of raw import strings, e.g.:
        Python:  ["os", "pathlib.Path", "codewalk.config"]
        JS/TS:   ["express", "./auth_service"]

    Returns an empty list (not an error) for languages with no import
    concept or no installed grammar.

    Raises:
        ParseError: if `file_path` cannot be read.
    """
    if language not in IMPORT_NODE_TYPES:
        return []

    parser = get_parser_for_language(language)
    if parser is None:
        return []

    try:
        source = file_path.read_bytes()
    except OSError as exc:
        raise ParseError(f"could not read {file_path}: {exc}") from exc

    tree = parser.parse(source)
    target_types = IMPORT_NODE_TYPES[language]

    imports: list[str] = []
    for node in _walk_for_imports(tree.root_node, target_types):
        imports.extend(_extract_raw_import(node, language))
    return imports


def _walk_for_imports(node: Node, target_types: frozenset[str]) -> Iterator[Node]:
    """Walk the AST, yielding import-like nodes (only `require(...)` calls, not
    every call_expression)."""
    if node.type in target_types:
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            is_require = func is not None and func.text is not None and _text(func) == "require"
            if is_require:
                yield node
        else:
            yield node
    for child in node.children:
        yield from _walk_for_imports(child, target_types)


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _extract_raw_import(node: Node, language: str) -> list[str]:
    """Extract the module/path string(s) an import AST node refers to.

    Returns a list because e.g. Python's `import os, sys` declares two.
    """
    handler = _IMPORT_EXTRACTORS.get(language)
    if handler is None:
        return []
    return handler(node)


def _single(text: str) -> list[str]:
    return [text] if text else []


def _extract_python_import(node: Node) -> list[str]:
    if node.type == "import_statement":
        return [_text(child) for child in node.children if child.type == "dotted_name"]

    if node.type == "import_from_statement":
        return _extract_python_import_from(node)

    return []


def _extract_python_import_from(node: Node) -> list[str]:
    """Handle `from <module> import ...`, including relative imports."""
    module_node = None
    for child in node.children:
        if child.type in ("relative_import", "dotted_name"):
            module_node = child
            break
    if module_node is None:
        return []

    module_text = _text(module_node)
    if module_node.type == "relative_import" and module_text.rstrip(".") == "":
        # "from . import a, b" -> each imported name is a submodule of the package.
        return [
            f"{module_text}{_text(child)}"
            for child in node.children
            if child.type == "dotted_name" and child is not module_node
        ]

    return [module_text]


def _extract_js_import(node: Node) -> list[str]:
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "string":
                return _single(_text(child).strip("'\""))
        return []

    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func is not None and _text(func) == "require":
            args = node.child_by_field_name("arguments")
            if args is not None:
                for arg in args.children:
                    if arg.type == "string":
                        return _single(_text(arg).strip("'\""))
        return []

    return []


def _extract_java_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type == "scoped_identifier":
            return _single(_text(child))
    return []


def _extract_go_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type == "interpreted_string_literal":
            return _single(_text(child).strip('"'))
    return []


def _extract_c_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type in ("string_literal", "system_lib_string"):
            return _single(_text(child).strip('"<>'))
    return []


def _extract_rust_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier", "use_wildcard"):
            return _single(_text(child))
    return []


def _extract_csharp_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type in ("qualified_name", "identifier"):
            return _single(_text(child))
    return []


def _extract_php_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type == "namespace_use_clause":
            for grandchild in child.children:
                if grandchild.type == "qualified_name":
                    return _single(_text(grandchild))
    return []


def _extract_ruby_import(node: Node) -> list[str]:
    if node.type == "call" and _text(node).startswith("require"):
        for child in node.children:
            if child.type == "argument_list":
                for arg in child.children:
                    if arg.type == "string":
                        return _single(_text(arg).strip("'\""))
    return []


def _extract_kotlin_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type == "qualified_identifier":
            return _single(_text(child))
    return []


def _extract_swift_import(node: Node) -> list[str]:
    for child in node.children:
        if child.type == "identifier":
            return _single(_text(child))
    return []


_IMPORT_EXTRACTORS = {
    "python": _extract_python_import,
    "javascript": _extract_js_import,
    "typescript": _extract_js_import,
    "java": _extract_java_import,
    "go": _extract_go_import,
    "c": _extract_c_import,
    "cpp": _extract_c_import,
    "rust": _extract_rust_import,
    "csharp": _extract_csharp_import,
    "php": _extract_php_import,
    "ruby": _extract_ruby_import,
    "kotlin": _extract_kotlin_import,
    "swift": _extract_swift_import,
}


# ─── Import resolution: raw string -> actual repo file, per language ──────


def _suffix_match(as_path: str, extensions: list[str], all_files: frozenset[str]) -> str:
    """Try progressively shorter suffixes of `as_path` against `all_files`.

    Handles the case where the scanned repo root is itself a subdirectory of
    the "real" import root, e.g. `import codewalk.errors` resolving to a repo
    that was scanned starting inside `src/`, so `all_files` only has
    `errors.py` relative to `src/codewalk/`.
    """
    parts = as_path.split("/")
    for i in range(1, len(parts)):
        suffix = "/".join(parts[i:])
        for ext in extensions:
            candidate = f"{suffix}{ext}"
            if candidate in all_files:
                return candidate
    return ""


def _resolve_java_like(raw_import: str, all_files: frozenset[str], ext: str) -> str:
    """Shared resolver for Java/Kotlin: dotted package name -> file path."""
    as_path = raw_import.replace(".", "/")
    suffix = f"{as_path}{ext}"
    if suffix in all_files:
        return suffix
    for candidate in all_files:
        if candidate.endswith(suffix):
            return candidate
    return _suffix_match(as_path, [ext], all_files) or raw_import


def _resolve_python_import(raw_import: str, all_files: frozenset[str], source_file: str) -> str:
    if raw_import.startswith("."):
        if not source_file:
            return raw_import

        source_dir = posixpath.dirname(source_file)
        level = 0
        for ch in raw_import:
            if ch == ".":
                level += 1
            else:
                break
        remainder = raw_import[level:].replace(".", "/")

        parts = source_dir.split("/")
        if len(parts) < level - 1:
            return raw_import
        base_parts = parts[: len(parts) - (level - 1)] if level > 1 else parts
        base = "/".join(base_parts)
        as_path = f"{base}/{remainder}" if remainder and base else (remainder or base)
    else:
        as_path = raw_import.replace(".", "/")

    for candidate in (f"{as_path}.py", f"{as_path}/__init__.py"):
        if candidate in all_files:
            return candidate
    return _suffix_match(as_path, [".py", "/__init__.py"], all_files) or raw_import


def _resolve_js_import(raw_import: str, all_files: frozenset[str], source_file: str) -> str:
    if not raw_import.startswith("."):
        return raw_import

    source_dir = posixpath.dirname(source_file)
    resolved_base = posixpath.normpath(posixpath.join(source_dir, raw_import))

    known_extensions = (".ts", ".js", ".tsx", ".jsx", ".mjs", ".cjs")
    if any(resolved_base.endswith(ext) for ext in known_extensions):
        return _resolve_js_import_with_extension(resolved_base, all_files, raw_import)
    return _resolve_js_import_without_extension(resolved_base, all_files, raw_import)


def _resolve_js_import_with_extension(
    resolved_base: str, all_files: frozenset[str], raw_import: str
) -> str:
    if resolved_base in all_files:
        return resolved_base
    # TS convention: source imports './foo.js' but the actual file is foo.ts.
    swaps = {".js": ".ts", ".jsx": ".tsx", ".mjs": ".mts", ".cjs": ".cts"}
    for old_ext, new_ext in swaps.items():
        if resolved_base.endswith(old_ext):
            swapped = resolved_base[: -len(old_ext)] + new_ext
            if swapped in all_files:
                return swapped
    return raw_import


def _resolve_js_import_without_extension(
    resolved_base: str, all_files: frozenset[str], raw_import: str
) -> str:
    for ext in (".ts", ".js", ".tsx", ".jsx"):
        candidate = f"{resolved_base}{ext}"
        if candidate in all_files:
            return candidate
    for ext in (".ts", ".js", ".tsx", ".jsx"):
        candidate = f"{resolved_base}/index{ext}"
        if candidate in all_files:
            return candidate
    return raw_import


def _resolve_go_import(raw_import: str, all_files: frozenset[str]) -> str:
    parts = raw_import.strip("/").split("/")
    last_part = parts[-1] if parts else ""

    best_match: str | None = None
    best_depth = 0
    for file in all_files:
        if not file.endswith(".go"):
            continue
        parent_parts = file.split("/")[:-1]
        if last_part not in parent_parts:
            continue
        for i in range(len(parent_parts)):
            suffix = parent_parts[i:]
            if len(suffix) > best_depth and parts[-len(suffix) :] == suffix:
                best_match = file
                best_depth = len(suffix)
                break
    return best_match or raw_import


def _resolve_rust_import(raw_import: str, all_files: frozenset[str], source_file: str) -> str:
    if not raw_import.startswith("crate"):
        return raw_import

    source_dir = posixpath.dirname(source_file)
    crate_root = ""
    parts = source_dir.split("/")
    for i in range(len(parts), 0, -1):
        prefix = "/".join(parts[:i])
        if f"{prefix}/Cargo.toml" in all_files:
            crate_root = prefix
            break

    crate_src = f"{crate_root}/src" if crate_root else "src"
    as_path = raw_import.replace("crate::", f"{crate_src}/").replace("::", "/")

    for candidate in (f"{as_path}.rs", f"{as_path}/mod.rs"):
        if candidate in all_files:
            return candidate

    parent = posixpath.dirname(as_path)
    if parent:
        for candidate in (f"{parent}.rs", f"{parent}/mod.rs"):
            if candidate in all_files:
                return candidate
    return raw_import


def _resolve_ruby_import(raw_import: str, all_files: frozenset[str]) -> str:
    if raw_import.startswith("."):
        candidate = f"{raw_import.lstrip('./')}.rb"
        if candidate in all_files:
            return candidate
    return raw_import


def _resolve_c_import(raw_import: str, all_files: frozenset[str]) -> str:
    if raw_import in all_files:
        return raw_import
    for prefix in ("include/", "src/"):
        candidate = f"{prefix}{raw_import}"
        if candidate in all_files:
            return candidate
    return raw_import


def _resolve_csharp_import(raw_import: str, all_files: frozenset[str]) -> str:
    as_path = raw_import.replace(".", "/")
    candidate = f"{as_path}.cs"
    if candidate in all_files:
        return candidate
    return _suffix_match(as_path, [".cs"], all_files) or raw_import


def _resolve_php_import(raw_import: str, all_files: frozenset[str]) -> str:
    as_path = raw_import.replace("\\", "/")
    for candidate in (f"{as_path}.php", f"src/{as_path}.php"):
        if candidate in all_files:
            return candidate
    return _suffix_match(as_path, [".php"], all_files) or raw_import


_RESOLVERS: dict[str, Callable[[str, frozenset[str], str], str]] = {
    "python": _resolve_python_import,
    "javascript": _resolve_js_import,
    "typescript": _resolve_js_import,
    "java": lambda raw, files, _src: _resolve_java_like(raw, files, ".java"),
    "kotlin": lambda raw, files, _src: _resolve_java_like(raw, files, ".kt"),
    "go": lambda raw, files, _src: _resolve_go_import(raw, files),
    "rust": _resolve_rust_import,
    "ruby": lambda raw, files, _src: _resolve_ruby_import(raw, files),
    "c": lambda raw, files, _src: _resolve_c_import(raw, files),
    "cpp": lambda raw, files, _src: _resolve_c_import(raw, files),
    "csharp": lambda raw, files, _src: _resolve_csharp_import(raw, files),
    "php": lambda raw, files, _src: _resolve_php_import(raw, files),
    # swift: module-level imports only, no per-file resolution possible.
}


def resolve_import_to_file(
    raw_import: str,
    language: str,
    all_files: frozenset[str],
    source_file: str = "",
) -> str:
    """Try to resolve a raw import string to an actual file in the repo.

    Returns the matching repo-relative file path if found, otherwise returns
    `raw_import` unchanged (treated as an external/unresolved dependency).
    """
    resolver = _RESOLVERS.get(language)
    if resolver is None:
        return raw_import
    return resolver(raw_import, all_files, source_file)


@dataclass
class DependencyGraphStats:
    total_files: int
    total_edges: int
    unresolved: int


@dataclass
class DependencyGraphResult:
    graph: dict[str, list[str]]
    stats: DependencyGraphStats
    warnings: list[str] = field(default_factory=list)


def build_dependency_graph(files: list[ScannedFile]) -> DependencyGraphResult:
    """Build a repo-wide import dependency graph from scanned files.

    Never raises: a file whose imports can't be extracted (unreadable) is
    recorded as a warning and contributes no edges, rather than aborting the
    whole build.
    """
    all_file_paths = frozenset(f.file_path for f in files)

    graph: dict[str, list[str]] = {}
    warnings: list[str] = []
    total_edges = 0
    unresolved_count = 0

    for file_info in files:
        try:
            raw_imports = extract_imports(file_info.absolute_path, file_info.language)
        except ParseError as exc:
            warnings.append(f"skipped imports for {file_info.file_path}: {exc}")
            graph[file_info.file_path] = []
            continue

        resolved_imports = []
        for raw in raw_imports:
            resolved = resolve_import_to_file(
                raw, file_info.language, all_file_paths, source_file=file_info.file_path
            )
            resolved_imports.append(resolved)
            if resolved == raw:
                unresolved_count += 1

        graph[file_info.file_path] = resolved_imports
        total_edges += len(resolved_imports)

    logger.info(
        "built dependency graph: %d files, %d edges, %d unresolved",
        len(files),
        total_edges,
        unresolved_count,
    )
    return DependencyGraphResult(
        graph=graph,
        stats=DependencyGraphStats(
            total_files=len(files), total_edges=total_edges, unresolved=unresolved_count
        ),
        warnings=warnings,
    )
