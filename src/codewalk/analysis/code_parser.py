"""Multi-language symbol extraction: tree-sitter for most languages, Python's
own `ast` module for Python (more accurate, already gives us decorators,
base classes, and methods for free).

Grammar/parse failures degrade gracefully:
  - A language with no installed tree-sitter grammar -> logged and skipped
    (returns an empty symbol list), never raised. This is an expected,
    known-limitation case, not a failure.
  - An unreadable file or a genuine Python `SyntaxError` -> raises
    `ParseError`, which callers (dependency_graph, higher orchestration)
    catch per-file so one bad file never aborts a whole repo scan.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from tree_sitter import Language, Node, Parser

from codewalk.errors import ParseError
from codewalk.log import get_logger

logger = get_logger(__name__)

# Grammar packages, keyed by our internal language name. Dart is deliberately
# not supported (no tree-sitter-dart dependency in this project).
GRAMMAR_MAP: dict[str, str] = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "java": "tree_sitter_java",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "ruby": "tree_sitter_ruby",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "csharp": "tree_sitter_c_sharp",
    "php": "tree_sitter_php",
    "kotlin": "tree_sitter_kotlin",
    "swift": "tree_sitter_swift",
}


@dataclass(frozen=True)
class LanguageSpec:
    """Which tree-sitter node types represent functions/classes in a language."""

    function_types: frozenset[str]
    class_types: frozenset[str]
    function_name_field: str = "name"
    class_name_field: str = "name"


NODE_TYPES: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        frozenset({"function_definition"}), frozenset({"class_definition"}), "name"
    ),
    "javascript": LanguageSpec(
        frozenset({"function_declaration", "method_definition"}),
        frozenset({"class_declaration"}),
        "name",
    ),
    "typescript": LanguageSpec(
        frozenset({"function_declaration", "method_definition"}),
        frozenset({"class_declaration"}),
        "name",
    ),
    "java": LanguageSpec(
        frozenset({"method_declaration", "constructor_declaration"}),
        frozenset({"class_declaration", "interface_declaration"}),
        "name",
    ),
    "go": LanguageSpec(
        frozenset({"function_declaration", "method_declaration"}),
        frozenset(),
        "name",
    ),
    "rust": LanguageSpec(
        frozenset({"function_item"}),
        frozenset({"struct_item", "impl_item", "enum_item"}),
        "name",
    ),
    "ruby": LanguageSpec(frozenset({"method"}), frozenset({"class"}), "name"),
    "c": LanguageSpec(
        frozenset({"function_definition"}),
        frozenset({"struct_specifier"}),
        function_name_field="declarator",
    ),
    "cpp": LanguageSpec(
        frozenset({"function_definition"}),
        frozenset({"class_specifier", "struct_specifier"}),
        function_name_field="declarator",
    ),
    "csharp": LanguageSpec(
        frozenset({"method_declaration", "constructor_declaration"}),
        frozenset({"class_declaration", "interface_declaration"}),
        "name",
    ),
    "php": LanguageSpec(
        frozenset({"function_definition", "method_declaration"}),
        frozenset({"class_declaration", "interface_declaration"}),
        "name",
    ),
    "kotlin": LanguageSpec(
        frozenset({"function_declaration"}),
        frozenset({"class_declaration", "object_declaration"}),
        "name",
    ),
    "swift": LanguageSpec(
        frozenset({"function_declaration"}),
        frozenset({"class_declaration", "struct_declaration", "enum_declaration"}),
        "name",
    ),
}


@cache
def get_language(language: str) -> Language | None:
    """Load (and cache) a tree-sitter `Language` for `language`.

    Returns None -- never raises -- if there is no grammar mapped, or the
    mapped grammar package can't be imported/loaded in this environment.
    """
    module_name = GRAMMAR_MAP.get(language)
    if not module_name:
        return None

    try:
        grammar_module = importlib.import_module(module_name)
        if language == "typescript":
            return Language(grammar_module.language_typescript())
        if language == "php":
            return Language(grammar_module.language_php())
        return Language(grammar_module.language())
    except (ImportError, AttributeError) as exc:
        logger.warning("no usable tree-sitter grammar for %r: %s", language, exc)
        return None


def get_parser_for_language(language: str) -> Parser | None:
    """Create a fresh tree-sitter `Parser` for `language`, or None if unsupported."""
    lang = get_language(language)
    if lang is None:
        return None
    return Parser(lang)


# C/C++ function names are reached via a chain of "declarator" wrapper nodes
# (function_declarator, pointer_declarator, ...) around the actual
# identifier, e.g. `int *greet(int x)` -> pointer_declarator -> function_declarator
# -> identifier. Unwrap them to get to the real name.
_DECLARATOR_WRAPPER_TYPES = frozenset(
    {"function_declarator", "pointer_declarator", "reference_declarator", "array_declarator"}
)
_MAX_DECLARATOR_UNWRAP_DEPTH = 5


def extract_name(node: Node, name_field: str) -> str:
    """Pull the name out of a function/class node, with per-grammar fallbacks."""
    name_node = node.child_by_field_name(name_field)

    depth = 0
    while (
        name_node is not None
        and name_node.type in _DECLARATOR_WRAPPER_TYPES
        and depth < _MAX_DECLARATOR_UNWRAP_DEPTH
    ):
        name_node = name_node.child_by_field_name("declarator")
        depth += 1

    if name_node is not None and name_node.text is not None:
        return name_node.text.decode("utf-8")

    return "<anonymous>"


# Across every supported grammar, the parameter list is exposed via the
# "parameters" field -- either directly on the function/method node, or
# nested one level down (e.g. C/C++'s function_declarator). A couple of
# grammars don't expose it as a field at all, so we fall back to matching by
# node *type* (Kotlin), and finally to treating direct "parameter"-typed
# children of the function node itself as the parameter list (Swift, which
# has no wrapping container node at all).
_PARAMS_FIELD = "parameters"
_PARAM_CONTAINER_TYPES = frozenset(
    {
        "formal_parameters",
        "parameter_list",
        "function_value_parameters",
        "method_parameters",
        "parameters",
    }
)


def _find_params_container(node: Node) -> Node | None:
    direct = node.child_by_field_name(_PARAMS_FIELD)
    if direct is not None:
        return direct
    for child in node.children:
        nested = child.child_by_field_name(_PARAMS_FIELD)
        if nested is not None:
            return nested
    for child in node.children:
        if child.type in _PARAM_CONTAINER_TYPES:
            return child
    return None


def extract_params(node: Node) -> list[str]:
    """Pull parameter names from a function/method node."""
    params_node = _find_params_container(node)
    if params_node is not None:
        names = (
            _extract_param_name(child) for child in params_node.children if _is_param_node(child)
        )
        return [name for name in names if name]

    # No container at all (e.g. Swift): "parameter" nodes are direct
    # siblings of "func"/identifier/etc. under the function node itself.
    direct_params = [child for child in node.children if child.type == "parameter"]
    if direct_params:
        names = (_extract_param_name(child) for child in direct_params)
        return [name for name in names if name]

    return []


def _is_param_node(node: Node) -> bool:
    return node.type not in ("(", ")", ",", "comment")


def _extract_param_name(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None and name_node.text is not None:
        return name_node.text.decode("utf-8")
    if node.type == "identifier" and node.text is not None:
        return node.text.decode("utf-8")
    for sub in node.children:
        if sub.type == "identifier" and sub.text is not None:
            return sub.text.decode("utf-8")
    return ""


def walk_tree(
    node: Node, target_types: set[str], skip_children_types: set[str] | None = None
) -> Iterator[Node]:
    """Recursively walk the CST, yielding nodes whose type is in `target_types`.

    `skip_children_types`: if a matched node's type is in this set, its
    children are not recursed into (prevents nested duplicates in some
    grammars, e.g. a method wrapper containing an inner function node).
    """
    skip = skip_children_types or set()
    if node.type in target_types:
        yield node
        if node.type in skip:
            return
    for child in node.children:
        yield from walk_tree(child, target_types, skip)


def _extract_decorators(node: Node) -> list[str]:
    decorators: list[str] = []
    for child in node.children:
        if child.type in ("decorator", "annotation") and child.text is not None:
            text = child.text.decode("utf-8", errors="replace").strip()
            if text.startswith("@"):
                text = text[1:].strip()
            decorators.append(text)
    return decorators


_IDENTIFIER_LIKE_TYPES = frozenset(
    {
        "identifier",
        "simple_identifier",
        "type_identifier",
        "property_identifier",
        "field_identifier",
        "name",
    }
)


def _extract_identifier_names(node: Node) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for child in walk_tree(node, set(_IDENTIFIER_LIKE_TYPES)):
        if child.text is None:
            continue
        name = child.text.decode("utf-8", errors="replace")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


_CLASS_PARENT_FIELDS = (
    "bases",
    "superclass",
    "interfaces",
    "extended_types",
    "implemented_types",
    "base_class",
    "inheritance",
    "supertypes",
)


def _extract_class_parents(node: Node) -> list[str]:
    names: list[str] = []
    for field_name in _CLASS_PARENT_FIELDS:
        base_node = node.child_by_field_name(field_name)
        if base_node is not None:
            names.extend(_extract_identifier_names(base_node))

    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


@dataclass
class Symbol:
    """A single extracted function or class."""

    kind: Literal["function", "class"]
    name: str
    start_line: int
    end_line: int
    code: str
    decorators: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)  # functions only
    bases: list[str] = field(default_factory=list)  # classes only
    methods: list[str] = field(default_factory=list)  # classes only
    parent_class: str | None = None  # functions only, set if nested in a class


def _attach_parent_class_and_methods(items: list[Symbol]) -> list[Symbol]:
    """For functions inside a class's line range, set parent_class; fill class methods.

    Known limitation (matches the upstream behavior this was ported from): a
    function nested inside a *method* (not directly inside the class body)
    still falls within the class's line range and is misattributed as a
    class method. This is a line-range heuristic, not full scope analysis.
    """
    class_ranges = [(i.start_line, i.end_line, i.name) for i in items if i.kind == "class"]

    for item in items:
        if item.kind != "function":
            continue
        item.parent_class = None
        for start, end, class_name in class_ranges:
            if start <= item.start_line and item.end_line <= end:
                item.parent_class = class_name
                break

    class_methods: dict[str, list[str]] = {}
    for item in items:
        if item.kind == "function" and item.parent_class:
            class_methods.setdefault(item.parent_class, []).append(item.name)

    for item in items:
        if item.kind == "class" and not item.methods:
            item.methods = class_methods.get(item.name, [])

    return items


def _get_decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_get_ast_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _get_decorator_name(node.func)
    return ""


def _get_decorator_text(source: str, node: ast.expr) -> str:
    segment = ast.get_source_segment(source, node)
    if segment:
        return segment.strip().lstrip("@")
    return _get_decorator_name(node)


def _get_ast_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_get_ast_name(node.value)}.{node.attr}"
    return ""


def _parse_python_ast(file_path: Path) -> list[Symbol]:
    """Parse a Python file with the stdlib `ast` module.

    Raises:
        ParseError: if the file can't be read, or contains a genuine syntax error.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ParseError(f"could not read {file_path}: {exc}") from exc

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ParseError(f"syntax error in {file_path}: {exc}") from exc

    lines = source.splitlines()
    items: list[Symbol] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end_line = node.end_lineno or node.lineno
            items.append(
                Symbol(
                    kind="function",
                    name=node.name,
                    start_line=node.lineno,
                    end_line=end_line,
                    code="\n".join(lines[node.lineno - 1 : end_line]),
                    decorators=[_get_decorator_text(source, dec) for dec in node.decorator_list],
                    args=[arg.arg for arg in node.args.args],
                )
            )
        elif isinstance(node, ast.ClassDef):
            end_line = node.end_lineno or node.lineno
            items.append(
                Symbol(
                    kind="class",
                    name=node.name,
                    start_line=node.lineno,
                    end_line=end_line,
                    code="\n".join(lines[node.lineno - 1 : end_line]),
                    bases=[_get_ast_name(base) for base in node.bases],
                    methods=[
                        body.name
                        for body in node.body
                        if isinstance(body, ast.FunctionDef | ast.AsyncFunctionDef)
                    ],
                )
            )

    return items


def parse_file(file_path: Path, language: str) -> list[Symbol]:
    """Parse a source file into its functions and classes.

    Raises:
        ParseError: file unreadable, or (Python only) a genuine syntax error.
            Callers should catch this per-file so one bad file never aborts
            a whole-repo scan.

    Returns:
        An empty list for languages with no installed grammar (logged, not
        an error) or with no recognized function/class node types.
    """
    if language == "python":
        items = _parse_python_ast(file_path)
        return _attach_parent_class_and_methods(items)

    spec = NODE_TYPES.get(language)
    if spec is None:
        return []

    parser = get_parser_for_language(language)
    if parser is None:
        return []

    try:
        source = file_path.read_bytes()
    except OSError as exc:
        raise ParseError(f"could not read {file_path}: {exc}") from exc

    tree = parser.parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    all_target_types = spec.function_types | spec.class_types

    items = []
    for node in walk_tree(tree.root_node, set(all_target_types), set(spec.function_types)):
        is_function = node.type in spec.function_types
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        name_field = spec.function_name_field if is_function else spec.class_name_field
        name = extract_name(node, name_field)
        code = "\n".join(lines[start_line - 1 : end_line])
        decorators = _extract_decorators(node)

        if is_function:
            items.append(
                Symbol(
                    kind="function",
                    name=name,
                    start_line=start_line,
                    end_line=end_line,
                    code=code,
                    decorators=decorators,
                    args=extract_params(node),
                )
            )
        else:
            items.append(
                Symbol(
                    kind="class",
                    name=name,
                    start_line=start_line,
                    end_line=end_line,
                    code=code,
                    decorators=decorators,
                    bases=_extract_class_parents(node),
                )
            )

    return _attach_parent_class_and_methods(items)
