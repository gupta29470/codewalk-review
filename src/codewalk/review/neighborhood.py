"""Neighborhood expansion: pull in callers, tests, and interfaces around changed files.

Gives the host LLM extra context beyond the raw diff -- who calls the
changed code, whether tests exist for it, and what types/interfaces it
depends on. Everything here is best-effort: a missing graph or an
unreadable file degrades to fewer snippets, never a crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from codewalk.graph.graph_store import GraphStore
from codewalk.review.diff_parser import DiffFile

_DEFAULT_MAX_SNIPPETS = 20
_DEFAULT_MAX_TOKENS = 30_000
_DEEP_MIN_SNIPPETS = 30
_DEEP_MIN_TOKENS = 60_000
_TEST_CANDIDATE_LIMIT = 5
_INTERFACE_READ_LINES = 200
_TOKEN_CHARS_PER_TOKEN = 3


@dataclass
class NeighborhoodSnippet:
    """One contextual snippet from a neighbor of a changed file."""

    file_path: str
    content: str
    source: str  # "caller" | "test" | "interface"


@dataclass
class NeighborhoodResult:
    """Collection of snippets surrounding the changed files."""

    snippets: list[NeighborhoodSnippet] = field(default_factory=list)


def _read_lines(path: Path, start: int, end: int) -> str:
    """Read lines [start, end] (1-based, inclusive) from a file. "" on any failure."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""

    start_idx = max(0, start - 1)
    end_idx = max(start_idx, end)
    return "\n".join(lines[start_idx:end_idx])


def _find_test_files(all_files: list[str], symbol_name: str) -> list[str]:
    """Match candidate test file paths for a changed source file, by naming convention."""
    escaped_base = re.escape(symbol_name)
    patterns = [
        re.compile(rf"^{escaped_base}\.test\..*$"),
        re.compile(rf"^{escaped_base}\.spec\..*$"),
        re.compile(rf"^.*/{escaped_base}\.test\..*$"),
        re.compile(rf"^.*/{escaped_base}\.spec\..*$"),
        re.compile(rf"^test_{escaped_base}\..*$"),
        re.compile(rf"^.*/test_{escaped_base}\..*$"),
        re.compile(rf"^{escaped_base}_test\..*$"),
        re.compile(rf"^.*/{escaped_base}_test\..*$"),
    ]

    candidates: list[str] = []
    for file_path in all_files:
        if any(pattern.match(file_path) for pattern in patterns):
            candidates.append(file_path)
            if len(candidates) >= _TEST_CANDIDATE_LIMIT:
                break
    return candidates


def _find_callers(
    repo_root: Path,
    diff_file: DiffFile,
    graph_store: GraphStore | None,
    deep: bool,
) -> list[NeighborhoodSnippet]:
    """Find snippets around the call sites of symbols defined in `diff_file`."""
    if graph_store is None:
        return []

    symbols = graph_store.get_symbols_in_file(diff_file.file_path)
    max_callers_per_symbol = 10 if deep else 5
    before, after = (10, 50) if deep else (5, 25)

    snippets: list[NeighborhoodSnippet] = []
    seen_files: set[str] = set()
    for symbol in symbols:
        if not symbol.qualified_name:
            continue
        callers = graph_store.get_callers_of_symbol(symbol.qualified_name)
        for caller in callers[:max_callers_per_symbol]:
            if caller.file in seen_files:
                continue
            seen_files.add(caller.file)
            start = max(1, caller.line - before)
            end = caller.line + after
            content = _read_lines(repo_root / caller.file, start, end)
            if content:
                snippets.append(
                    NeighborhoodSnippet(file_path=caller.file, content=content, source="caller")
                )
    return snippets


def _find_tests(
    repo_root: Path, diff_file: DiffFile, graph_store: GraphStore | None
) -> list[NeighborhoodSnippet]:
    """Find test files for a changed file, using the indexed file list."""
    if graph_store is None:
        return []

    base = Path(diff_file.file_path).stem
    all_files = graph_store.get_all_files()
    test_files = _find_test_files(all_files, base)

    snippets: list[NeighborhoodSnippet] = []
    for rel_path in test_files:
        content = _read_lines(repo_root / rel_path, 1, _INTERFACE_READ_LINES)
        if content:
            snippets.append(NeighborhoodSnippet(file_path=rel_path, content=content, source="test"))
    return snippets


def _extensions_for_file(file_path: str) -> tuple[str, ...]:
    """Likely extensions for relative-import resolution, based on the source language."""
    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        return (".py",)
    if suffix == ".go":
        return (".go",)
    if suffix == ".dart":
        return (".dart",)
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return (".ts", ".tsx", ".js", ".jsx")
    return (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".dart")


def _resolve_relative_import(
    full_path: Path, module: str, extensions: tuple[str, ...]
) -> Path | None:
    resolved = (full_path.parent / module).resolve()
    for ext in extensions:
        candidate = Path(str(resolved) + ext)
        if candidate.exists():
            return candidate
        index_candidate = resolved / f"index{ext}"
        if index_candidate.exists():
            return index_candidate
    return None


def _relative_import_module(line: str) -> str | None:
    """Return the module path if `line` is a relative `from`-style import, else None."""
    stripped = line.strip()
    if not (stripped.startswith(("import ", "from ")) or stripped.startswith("import(")):
        return None
    if "from" not in stripped:
        return None
    parts = stripped.split("from")
    if len(parts) != 2:
        return None
    module = parts[1].strip().strip("\"';")
    return module if module.startswith(".") else None


def _find_interfaces(repo_root: Path, diff_file: DiffFile) -> list[NeighborhoodSnippet]:
    """Find same-repo files imported (via relative import) by the changed file."""
    full_path = repo_root / diff_file.file_path
    if not full_path.exists():
        return []
    try:
        source = full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    extensions = _extensions_for_file(diff_file.file_path)
    snippets: list[NeighborhoodSnippet] = []
    seen: set[str] = set()

    for line in source.splitlines():
        module = _relative_import_module(line)
        if module is None:
            continue

        candidate = _resolve_relative_import(full_path, module, extensions)
        if candidate is None:
            continue
        rel = str(candidate.relative_to(repo_root))
        if rel in seen:
            continue
        seen.add(rel)
        content = _read_lines(candidate, 1, _INTERFACE_READ_LINES)
        if content:
            snippets.append(NeighborhoodSnippet(file_path=rel, content=content, source="interface"))

    return snippets


def _is_test_file(file_path: str) -> bool:
    lower = file_path.lower()
    if ".test." in lower or ".spec." in lower:
        return True
    return lower.startswith("test_") or lower.endswith("_test.py")


def _snippet_priority(snippet: NeighborhoodSnippet, relevant_files: set[str]) -> int:
    """Lower is more relevant; used as a sort key."""
    if snippet.file_path in relevant_files:
        return 0
    if snippet.source == "test" or _is_test_file(snippet.file_path):
        return 1
    if snippet.source == "caller":
        return 2
    return 3


def expand_neighborhood(
    repo_root: Path,
    diff_files: list[DiffFile],
    graph_store: GraphStore | None = None,
    relevant_files: set[str] | None = None,
    max_snippets: int = _DEFAULT_MAX_SNIPPETS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    deep: bool = False,
) -> NeighborhoodResult:
    """Expand neighborhood context (callers, tests, interfaces) for changed files.

    Args:
        repo_root: Repository root.
        diff_files: Changed files in the current review scope.
        graph_store: Optional graph store; without one, only interface
            expansion (which needs no index) still runs.
        relevant_files: Files considered most relevant (e.g. other changed
            files) -- snippets touching them are prioritized.
        max_snippets: Hard cap on the number of snippets returned.
        max_tokens: Hard token budget for total neighborhood context.
        deep: When True (typically a single-file batch), widens the caller
            window and raises the snippet/token budget.
    """
    if deep:
        max_snippets = max(max_snippets, _DEEP_MIN_SNIPPETS)
        max_tokens = max(max_tokens, _DEEP_MIN_TOKENS)

    if relevant_files is None:
        relevant_files = {df.file_path for df in diff_files}

    snippets: list[NeighborhoodSnippet] = []
    seen: set[tuple[str, str]] = set()

    def _add_all(new_snippets: list[NeighborhoodSnippet]) -> None:
        for snippet in new_snippets:
            key = (snippet.file_path, snippet.source)
            if key not in seen:
                seen.add(key)
                snippets.append(snippet)

    for df in diff_files:
        _add_all(_find_callers(repo_root, df, graph_store, deep))
        _add_all(_find_tests(repo_root, df, graph_store))
        _add_all(_find_interfaces(repo_root, df))

    snippets.sort(key=lambda s: _snippet_priority(s, relevant_files))

    kept: list[NeighborhoodSnippet] = []
    tokens_used = 0
    for snippet in snippets[:max_snippets]:
        snippet_tokens = len(snippet.content) // _TOKEN_CHARS_PER_TOKEN
        if tokens_used + snippet_tokens > max_tokens:
            break
        kept.append(snippet)
        tokens_used += snippet_tokens

    return NeighborhoodResult(snippets=kept)
