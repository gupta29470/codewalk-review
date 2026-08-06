"""Extract call sites (function/method invocations) from source files.

Feeds the `symbol_calls` table in `graph.graph_store`. Best-effort: callee
names are extracted syntactically (no type resolution), so calls to
identically-named methods on unrelated classes can't always be disambiguated
-- `graph_store` picks a same-file candidate when available, else the first
match by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from codewalk.analysis.code_parser import (
    NODE_TYPES,
    LanguageSpec,
    extract_name,
    get_parser_for_language,
)
from codewalk.errors import ParseError
from codewalk.ingestion.scanner import ScannedFile
from codewalk.log import get_logger

logger = get_logger(__name__)

CALL_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"call"}),
    "javascript": frozenset({"call_expression"}),
    "typescript": frozenset({"call_expression"}),
    "java": frozenset({"method_invocation"}),
    "go": frozenset({"call_expression"}),
    "rust": frozenset({"call_expression"}),
    "ruby": frozenset({"call", "method_call"}),
    "c": frozenset({"call_expression"}),
    "cpp": frozenset({"call_expression"}),
    "csharp": frozenset({"invocation_expression"}),
    "php": frozenset({"function_call_expression", "method_call_expression"}),
    "kotlin": frozenset({"call_expression"}),
    "swift": frozenset({"call_expression"}),
}

_FUNCTION_FIELDS = ("function", "name", "method")
_NAME_FIELDS = ("property", "attribute", "field", "name")
_LEAF_IDENTIFIER_TYPES = frozenset(
    {"identifier", "simple_identifier", "property_identifier", "field_identifier", "name"}
)
_MEMBER_TYPES = frozenset(
    {
        "attribute",  # Python: obj.method
        "member_expression",  # JS/TS: obj.method
        "selector_expression",  # Go: pkg.Func
        "field_expression",  # Rust/C: obj.method
        "member_access_expression",  # C#: obj.Method
        "scoped_identifier",  # Rust: mod::func
        "qualified_name",  # PHP: Ns\func
        "navigation_expression",  # Kotlin: obj.method
    }
)


def _extract_callee_name(call_node: Node) -> str | None:
    """Extract the callee function/method name from a call expression node.

    Handles common patterns across languages: `foo()` -> "foo",
    `self.foo()` -> "foo", `obj.method()` -> "method", `pkg::func()` -> "func".
    """
    func_node = _find_function_field(call_node) or _first_positional_callee_node(call_node)
    if func_node is None:
        return None

    if func_node.type in _LEAF_IDENTIFIER_TYPES and func_node.text is not None:
        return func_node.text.decode("utf-8")

    if func_node.type in _MEMBER_TYPES:
        return _extract_member_name(func_node)

    return None


def _find_function_field(call_node: Node) -> Node | None:
    for field_name in _FUNCTION_FIELDS:
        func_node = call_node.child_by_field_name(field_name)
        if func_node is not None:
            return func_node
    return None


def _first_positional_callee_node(node: Node) -> Node | None:
    """Fallback for grammars with no field name linking the call to its callee
    (e.g. Kotlin's call_expression): scan direct children positionally for
    the first leaf identifier or member-access expression."""
    for child in node.children:
        if child.type in _LEAF_IDENTIFIER_TYPES or child.type in _MEMBER_TYPES:
            return child
    return None


def _extract_member_name(func_node: Node) -> str | None:
    """Pull the rightmost member name out of e.g. `obj.method` or `mod::func`."""
    for field_name in _NAME_FIELDS:
        name_child = func_node.child_by_field_name(field_name)
        if name_child is not None and name_child.text is not None:
            return name_child.text.decode("utf-8")
    for child in reversed(func_node.children):
        if child.type in _LEAF_IDENTIFIER_TYPES and child.text is not None:
            return child.text.decode("utf-8")
    return None


@dataclass(frozen=True)
class CallSite:
    """A single call site: `caller` (qualified scope) called `callee_name` at `line`."""

    caller: str  # e.g. "views.py:login"
    callee_name: str  # unresolved name, e.g. "authenticate"
    line: int


def extract_calls_from_file(
    file_path: Path,
    language: str,
    identifier_path: str | None = None,
) -> list[CallSite]:
    """Extract all call sites from a source file.

    Scope tracking: calls inside a function -> caller is that function; calls
    inside a class but outside methods -> caller is the class; calls at
    module level -> caller is "<identifier_path>:<module>".

    Uses an iterative DFS (no Python recursion-limit risk on large files).

    Raises:
        ParseError: if the file can't be read.

    Returns an empty list (not an error) for languages with no grammar or no
    recognized call-expression node types.
    """
    spec = NODE_TYPES.get(language)
    call_types = CALL_TYPES.get(language)
    if spec is None or not call_types:
        return []

    parser = get_parser_for_language(language)
    if parser is None:
        return []

    try:
        source = file_path.read_bytes()
    except OSError as exc:
        raise ParseError(f"could not read {file_path}: {exc}") from exc

    tree = parser.parse(source)
    all_def_types = spec.function_types | spec.class_types

    id_path = identifier_path or str(file_path)
    module_scope = f"{id_path}:<module>"

    results: list[CallSite] = []
    seen: set[tuple[str, str, int]] = set()
    stack: list[tuple[Node, str]] = [(tree.root_node, module_scope)]

    while stack:
        node, scope = stack.pop()
        current_scope = _scope_for_node(node, scope, spec, all_def_types, id_path)

        if node.type in call_types:
            call_site = _build_call_site(node, current_scope, seen)
            if call_site is not None:
                results.append(call_site)

        for child in reversed(node.children):
            stack.append((child, current_scope))

    return results


def _scope_for_node(
    node: Node, scope: str, spec: LanguageSpec, all_def_types: frozenset[str], id_path: str
) -> str:
    """Return the enclosing scope name for `node` (unchanged unless it's a def)."""
    if node.type not in all_def_types:
        return scope
    is_function = node.type in spec.function_types
    name_field = spec.function_name_field if is_function else spec.class_name_field
    name = extract_name(node, name_field)
    return f"{id_path}:{name}"


def _build_call_site(
    node: Node, current_scope: str, seen: set[tuple[str, str, int]]
) -> CallSite | None:
    """Build a deduplicated `CallSite` for a call-expression node, if resolvable."""
    callee = _extract_callee_name(node)
    if callee is None:
        return None

    caller_short = current_scope.rsplit(":", 1)[-1]
    if callee == caller_short:
        return None  # self-recursion by short name is usually noise, not a real edge

    line = node.start_point[0] + 1
    key = (current_scope, callee, line)
    if key in seen:
        return None
    seen.add(key)
    return CallSite(caller=current_scope, callee_name=callee, line=line)


def extract_calls_batch(files: list[ScannedFile]) -> list[CallSite]:
    """Extract call sites from every file with a supported grammar.

    Args:
        files: scanned files, e.g. from `ingestion.scanner.scan_repo`.

    A file that fails to parse (unreadable) is skipped with a warning logged
    -- one bad file must never abort call extraction for the rest of the repo.
    """
    all_calls: list[CallSite] = []
    parsed = 0
    skipped = 0

    for file_info in files:
        if file_info.language not in CALL_TYPES:
            skipped += 1
            continue
        try:
            calls = extract_calls_from_file(
                file_info.absolute_path, file_info.language, identifier_path=file_info.file_path
            )
        except ParseError as exc:
            logger.warning("skipped call extraction for %s: %s", file_info.file_path, exc)
            skipped += 1
            continue
        all_calls.extend(calls)
        parsed += 1

    logger.info(
        "extracted %d call sites from %d files (%d skipped -- no grammar/unreadable)",
        len(all_calls),
        parsed,
        skipped,
    )
    return all_calls
