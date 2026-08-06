"""Shared pytest fixtures (populated as phases add fixture repos/factories)."""

from __future__ import annotations

from pathlib import Path

from codewalk.analysis.dependency_graph import build_dependency_graph
from codewalk.analysis.module_detector import detect_modules
from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore
from codewalk.ingestion.scanner import ScannedFile, detect_language
from codewalk.review.diff_parser import ChangedLine, DiffFile, DiffHunk


def write_repo_files(root: Path, files: dict[str, str]) -> list[ScannedFile]:
    """Write `files` (relative path -> text content) under `root`.

    Returns `ScannedFile` entries for each (mirroring what
    `ingestion.scanner.scan_repo` would produce) so analysis-layer tests can
    build small fixture repos without going through a full disk scan.
    """
    scanned = []
    for relative_path, content in files.items():
        full_path = root / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        scanned.append(
            ScannedFile(
                file_path=relative_path,
                absolute_path=full_path,
                language=detect_language(full_path),
                size_bytes=full_path.stat().st_size,
            )
        )
    return scanned


def build_graph(tmp_path: Path, files: dict[str, str]) -> tuple[GraphStore, GraphRuntime, Path]:
    """Write `files` under `tmp_path/repo`, populate a real `GraphStore`, and
    build a `GraphRuntime` on top of it. Shared by review-layer tests that
    need a small but real graph rather than a mocked one."""
    root = tmp_path / "repo"
    scanned = write_repo_files(root, files)
    dep_result = build_dependency_graph(scanned)
    module_result = detect_modules(scanned, dep_graph=dep_result.graph)
    store = GraphStore(root / ".codewalk" / "graph.duckdb")
    store.populate_from_analysis(scanned, dep_result.graph, module_result)
    runtime = GraphRuntime(store)
    return store, runtime, root


def make_diff_file(
    file_path: str,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    is_new_file: bool = False,
    is_deleted: bool = False,
    start_line: int = 1,
) -> DiffFile:
    """Build a minimal single-hunk `DiffFile` for review-layer tests.

    `added`/`removed` are lists of line contents (without diff markers).
    """
    added = added or []
    removed = removed or []
    lines = [
        ChangedLine(line_number=start_line + i, content=c, change_type="removed")
        for i, c in enumerate(removed)
    ]
    lines += [
        ChangedLine(line_number=start_line + i, content=c, change_type="added")
        for i, c in enumerate(added)
    ]
    hunk = DiffHunk(
        start_line=start_line,
        end_line=start_line + len(added),
        lines=lines,
        source_start=start_line,
        source_length=len(removed),
    )
    return DiffFile(
        file_path=file_path,
        language=detect_language(Path(file_path)),
        hunks=[hunk],
        is_new_file=is_new_file,
        is_deleted=is_deleted,
        added_lines=len(added),
        removed_lines=len(removed),
    )
