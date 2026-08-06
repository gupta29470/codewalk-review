"""DuckDB-backed graph store: schema, population from analysis results, and
read queries for symbols/files/modules/calls.

Persists at `<repo_root>/.codewalk/graph.duckdb`. No ChromaDB/embeddings
coupling at all -- there is no `chunks` table.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from codewalk.analysis.code_parser import GRAMMAR_MAP, Symbol, parse_file
from codewalk.analysis.module_detector import ModuleDetectionResult, ModuleInfo
from codewalk.errors import ConfigError, GraphCorruptedError, GraphLockError, ParseError
from codewalk.graph.call_extractor import extract_calls_batch
from codewalk.ingestion.scanner import ScannedFile
from codewalk.log import get_logger

logger = get_logger(__name__)

_LOCK_MESSAGE_MARKERS = ("could not set lock", "lock")
_PID_PATTERN = re.compile(r"PID\s+(\d+)")


def _stable_id(*parts: str) -> str:
    """Deterministic hash ID from input parts -- no DB round-trip needed."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _symbol_id(file_path: str, item: Symbol, index_in_file: int) -> str:
    """Deterministic symbol ID: stable across re-runs as long as the file's
    parsed symbol list doesn't change, so re-population doesn't churn IDs."""
    qualified_name = f"{file_path}:{item.name}"
    return _stable_id(qualified_name, file_path, str(item.start_line), str(index_in_file))


def _symbol_row(item: Symbol, symbol_id: str, file_id: str, file_path: str) -> tuple[object, ...]:
    qualified_name = f"{file_path}:{item.name}"
    return (
        symbol_id,
        item.name,
        qualified_name,
        file_id,
        item.kind,
        item.start_line,
        item.end_line,
        item.parent_class,
    )


def _metadata_row(symbol_id: str, meta: SymbolMetadata) -> tuple[object, ...]:
    return (
        symbol_id,
        meta.kind,
        meta.http_method,
        meta.http_path,
        meta.event_name,
        meta.cli_command,
    )


@dataclass
class GraphStats:
    files: int = 0
    imports: int = 0
    symbols: int = 0
    symbol_calls: int = 0
    modules: int = 0


@dataclass(frozen=True)
class SymbolInfo:
    symbol_id: str
    name: str
    qualified_name: str
    symbol_type: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SymbolMatch:
    """A symbol lookup result: enough to locate it in the repo and on disk."""

    symbol_id: str
    name: str
    qualified_name: str
    file_path: str
    symbol_type: str
    start_line: int
    end_line: int
    parent_class: str | None


@dataclass(frozen=True)
class CallerInfo:
    caller: str
    caller_qualified: str
    file: str
    line: int


@dataclass(frozen=True)
class CalleeInfo:
    callee: str
    callee_qualified: str
    file: str
    line: int


@dataclass
class SymbolMetadata:
    kind: str | None = None
    http_method: str | None = None
    http_path: str | None = None
    event_name: str | None = None
    cli_command: str | None = None


_ROUTE_INDICATORS = (
    "route",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "api_view",
    "requestmapping",
    "getmapping",
    "postmapping",
    "putmapping",
    "deletemapping",
    "patchmapping",
)
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_CLI_INDICATORS = ("cli.command", "click.command", "add_parser")
_EVENT_INDICATORS = ("on_event", "event_handler", "subscribe", "listener", "on_")
_CRON_INDICATORS = ("cron", "schedule", "scheduled", "crontab", "interval")
_SERVICE_SUFFIXES = ("Service", "Manager", "Handler", "Controller")
_MODEL_SUFFIXES = ("Model", "Entity", "Schema")


def _infer_route_metadata(decorators: list[str], dec_text: str) -> SymbolMetadata | None:
    if not any(indicator in dec_text for indicator in _ROUTE_INDICATORS):
        return None
    for dec in decorators:
        dlower = dec.lower()
        method = next((m.upper() for m in _HTTP_METHODS if m in dlower), None)
        path_match = re.search(r"['\"]([^'\"]+)['\"]", dec)
        if path_match is not None:
            return SymbolMetadata(kind="route", http_method=method, http_path=path_match.group(1))
    return SymbolMetadata(kind="route")


def _infer_cli_metadata(name: str, dec_text: str) -> SymbolMetadata | None:
    if not any(indicator in dec_text for indicator in _CLI_INDICATORS):
        return None
    match = re.search(r"['\"]([^'\"]+)['\"]", dec_text)
    return SymbolMetadata(kind="cli", cli_command=match.group(1) if match else name)


def _infer_event_metadata(dec_text: str) -> SymbolMetadata | None:
    if not any(indicator in dec_text for indicator in _EVENT_INDICATORS):
        return None
    match = re.search(r"['\"]([^'\"]+)['\"]", dec_text)
    return SymbolMetadata(kind="event", event_name=match.group(1) if match else None)


def _infer_class_metadata(name: str, bases: list[str]) -> SymbolMetadata | None:
    names = [name, *bases]
    if any(n.endswith(_SERVICE_SUFFIXES) for n in names):
        return SymbolMetadata(kind="service")
    if any(n.endswith(_MODEL_SUFFIXES) or n in ("Base", "Model") for n in names):
        return SymbolMetadata(kind="model")
    return None


def _infer_symbol_metadata(item: Symbol) -> SymbolMetadata:
    """Infer entry-point/route/cli/event/service metadata from decorators, name, bases."""
    dec_text = " ".join(item.decorators).lower()

    route = _infer_route_metadata(item.decorators, dec_text)
    if route is not None:
        return route

    cli = _infer_cli_metadata(item.name, dec_text)
    if cli is not None:
        return cli

    event = _infer_event_metadata(dec_text)
    if event is not None:
        return event

    if any(indicator in dec_text for indicator in _CRON_INDICATORS):
        return SymbolMetadata(kind="cron")

    if item.name.lower() == "main":
        return SymbolMetadata(kind="entrypoint")

    if item.kind == "class":
        class_meta = _infer_class_metadata(item.name, item.bases)
        if class_meta is not None:
            return class_meta

    return SymbolMetadata()


class GraphStore:
    """Persistent graph storage backed by DuckDB.

    Usage:
        store = GraphStore(".codewalk/graph.duckdb")
        store.populate_from_analysis(files, import_graph, module_result)
        # Data persists across restarts -- no rebuild needed until refreshed.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        retries: int = 5,
        retry_delay: float = 1.0,
        max_wait_seconds: float = 10.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db_path = Path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"cannot create directory for graph database at {self.db_path.parent}: {exc}"
            ) from exc

        self._sleep = sleep_fn
        self._now = now_fn
        self.conn = self._connect_with_retry(retries, retry_delay, max_wait_seconds)
        # DuckDB connections are not safe to query concurrently from multiple
        # threads (interleaved execute()/fetch calls on one connection can
        # return corrupted result rows). A single Workspace -- and thus its
        # GraphStore -- can be shared across MCP tool calls running on
        # different threads, so every query below is serialized through this
        # lock, with the fetch happening atomically inside the same critical
        # section as the execute() (never left for the caller to do later).
        self._query_lock = threading.Lock()
        self._create_tables()

    def _run(self, sql: str, params: list[Any] | None = None) -> None:
        """Thread-safe execute with no result consumed (DDL/DML)."""
        with self._query_lock:
            if params is not None:
                self.conn.execute(sql, params)
            else:
                self.conn.execute(sql)

    def _fetchall(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Thread-safe execute + fetchall, atomic under the lock."""
        with self._query_lock:
            if params is not None:
                cursor = self.conn.execute(sql, params)
            else:
                cursor = self.conn.execute(sql)
            return cursor.fetchall()

    def _fetchone(self, sql: str, params: list[Any] | None = None) -> tuple[Any, ...] | None:
        """Thread-safe execute + fetchone, atomic under the lock."""
        with self._query_lock:
            if params is not None:
                cursor = self.conn.execute(sql, params)
            else:
                cursor = self.conn.execute(sql)
            return cursor.fetchone()

    def _executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        """Thread-safe executemany (bulk insert)."""
        with self._query_lock:
            self.conn.executemany(sql, params)

    def _connect_with_retry(
        self, retries: int, retry_delay: float, max_wait_seconds: float
    ) -> duckdb.DuckDBPyConnection:
        """Connect to DuckDB, retrying on lock conflicts within a wall-clock budget.

        Raises:
            GraphLockError: another live process holds the lock, or the retry
                budget (attempts or wall-clock time) was exhausted.
            GraphCorruptedError: the file exists but DuckDB can't open it for
                a reason other than a lock (corruption, invalid format, ...).
        """
        start = self._now()
        last_error: duckdb.IOException | None = None

        for attempt in range(1, retries + 1):
            try:
                return duckdb.connect(str(self.db_path))
            except duckdb.IOException as exc:
                last_error = exc
                if not self._is_lock_error(exc):
                    raise GraphCorruptedError(
                        f"graph database at {self.db_path} could not be opened: {exc}. "
                        f"Delete it and run codewalk_refresh_analysis to rebuild."
                    ) from exc

                pid = self._extract_pid(exc)
                if pid is not None and self._is_process_dead(pid):
                    logger.warning("lock holder PID %d is dead, cleaning stale lock files", pid)
                    self._cleanup_stale_lock_files()
                    continue  # OS lock is already released; retry immediately.

                elapsed = self._now() - start
                if attempt >= retries or elapsed >= max_wait_seconds:
                    raise self._lock_error(pid, exc) from exc

                logger.warning(
                    "DuckDB lock conflict (attempt %d/%d, %.1fs elapsed), retrying in %.1fs...",
                    attempt,
                    retries,
                    elapsed,
                    retry_delay,
                )
                self._sleep(retry_delay)

        # Unreachable in practice (loop always returns or raises), but keeps
        # mypy happy and is a safe fallback if it somehow is reached.
        raise self._lock_error(self._extract_pid(last_error) if last_error else None, last_error)

    @staticmethod
    def _is_lock_error(exc: duckdb.IOException) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _LOCK_MESSAGE_MARKERS)

    @staticmethod
    def _extract_pid(exc: duckdb.IOException | None) -> int | None:
        if exc is None:
            return None
        match = _PID_PATTERN.search(str(exc))
        return int(match.group(1)) if match else None

    @staticmethod
    def _is_process_dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False  # process exists but we can't signal it -- treat as unknown/alive
        return False

    def _cleanup_stale_lock_files(self) -> None:
        for ext in (".wal", ".tmp"):
            stale = Path(f"{self.db_path}{ext}")
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                # Best-effort hygiene only -- the OS-level lock is already
                # released now that the holder process is confirmed dead, so
                # a cleanup failure here must not block reconnecting.
                logger.warning("could not remove stale lock file %s: %s", stale, exc)

    def _lock_error(self, pid: int | None, cause: duckdb.IOException | None) -> GraphLockError:
        message = f"DuckDB lock conflict on '{self.db_path}'. Another process is holding the lock."
        if pid is not None:
            message += (
                f"\n\nConflicting process: PID {pid}\n\nTo fix this:"
                f"\n  1. Stop the other Codewalk process (MCP server, etc.)"
                f"\n  2. Or run: kill {pid}"
                f"\n  3. Then retry your command"
            )
        else:
            message += (
                f"\n\nTo fix this:\n  1. Stop any running Codewalk processes"
                f"\n  2. Or delete the lock: rm -f '{self.db_path}.wal' '{self.db_path}.tmp'"
                f"\n  3. Then retry your command"
            )
        return GraphLockError(message)

    def _create_tables(self) -> None:
        self._run("""
            CREATE TABLE IF NOT EXISTS files (
                file_id VARCHAR PRIMARY KEY,
                path VARCHAR UNIQUE,
                module VARCHAR,
                language VARCHAR
            );

            CREATE TABLE IF NOT EXISTS imports (
                source_file_id VARCHAR REFERENCES files(file_id),
                target_file_id VARCHAR REFERENCES files(file_id),
                PRIMARY KEY (source_file_id, target_file_id)
            );

            CREATE TABLE IF NOT EXISTS symbols (
                symbol_id VARCHAR PRIMARY KEY,
                name VARCHAR,
                qualified_name VARCHAR,
                file_id VARCHAR REFERENCES files(file_id),
                symbol_type VARCHAR,
                start_line INTEGER,
                end_line INTEGER,
                parent_class VARCHAR
            );

            CREATE TABLE IF NOT EXISTS symbol_metadata (
                symbol_id VARCHAR PRIMARY KEY REFERENCES symbols(symbol_id),
                kind VARCHAR,
                http_method VARCHAR,
                http_path VARCHAR,
                event_name VARCHAR,
                cli_command VARCHAR
            );

            CREATE TABLE IF NOT EXISTS class_hierarchy (
                class_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                parent_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                PRIMARY KEY (class_symbol_id, parent_symbol_id)
            );

            CREATE TABLE IF NOT EXISTS class_members (
                class_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                member_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                PRIMARY KEY (class_symbol_id, member_symbol_id)
            );

            CREATE TABLE IF NOT EXISTS symbol_calls (
                caller_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                callee_symbol_id VARCHAR REFERENCES symbols(symbol_id),
                line INTEGER,
                PRIMARY KEY (caller_symbol_id, callee_symbol_id, line)
            );

            CREATE TABLE IF NOT EXISTS modules (
                name VARCHAR PRIMARY KEY,
                file_count INTEGER
            );

            CREATE TABLE IF NOT EXISTS module_deps (
                source VARCHAR,
                target VARCHAR,
                PRIMARY KEY (source, target)
            );
        """)

    def populate_from_analysis(
        self,
        files: list[ScannedFile],
        import_graph: dict[str, list[str]],
        module_result: ModuleDetectionResult,
    ) -> list[str]:
        """Populate all tables from scan/analysis results.

        Never raises for a single file's parse failure -- returns a list of
        warnings instead (e.g. a file that couldn't be read for symbol
        extraction contributes no symbols but doesn't abort the whole build).
        """
        self._clear_all_tables()

        self._populate_files(files, module_result)
        self._populate_imports(import_graph)
        warnings, hierarchy_rows, member_rows, metadata_rows = self._populate_symbols(files)
        self._populate_symbol_metadata(metadata_rows)
        self._populate_class_hierarchy(hierarchy_rows)
        self._populate_class_members(member_rows)
        self._populate_symbol_calls(files)
        self._populate_modules(module_result)

        stats = self.get_stats()
        logger.info(
            "populated graph: %d files, %d imports, %d symbols, %d modules",
            stats.files,
            stats.imports,
            stats.symbols,
            stats.modules,
        )
        return warnings

    def _clear_all_tables(self) -> None:
        # Reverse FK order: children before parents. Table names are a fixed
        # internal tuple, never user input.
        for table in (
            "symbol_calls",
            "class_members",
            "class_hierarchy",
            "symbol_metadata",
            "symbols",
            "imports",
            "module_deps",
            "modules",
            "files",
        ):
            self._run(f"DELETE FROM {table}")  # noqa: S608 -- fixed internal table names

    def _populate_files(
        self, files: list[ScannedFile], module_result: ModuleDetectionResult
    ) -> None:
        if not files:
            return
        file_to_module = {
            file_path: module_name
            for module_name, info in module_result.modules.items()
            for file_path in info.files
        }
        self._executemany(
            "INSERT INTO files (file_id, path, module, language) VALUES (?, ?, ?, ?)",
            [
                (
                    _stable_id(f.file_path),
                    f.file_path,
                    file_to_module.get(f.file_path, "root"),
                    f.language,
                )
                for f in files
            ],
        )

    def _populate_imports(self, import_graph: dict[str, list[str]]) -> None:
        known_ids = {row[0] for row in self._fetchall("SELECT file_id FROM files")}

        rows: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source, targets in import_graph.items():
            source_id = _stable_id(source)
            if source_id not in known_ids:
                continue
            for target in targets:
                target_id = _stable_id(target)
                key = (source_id, target_id)
                if target_id in known_ids and key not in seen:
                    seen.add(key)
                    rows.append(key)

        if rows:
            self._executemany(
                "INSERT INTO imports (source_file_id, target_file_id) VALUES (?, ?)", rows
            )

    def _populate_symbols(
        self, files: list[ScannedFile]
    ) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]], list[tuple[object, ...]]]:
        """Parse every file's symbols and insert them.

        Returns (warnings, class_hierarchy_rows, class_member_rows, metadata_rows).
        """
        warnings: list[str] = []
        symbol_rows: list[tuple[object, ...]] = []
        metadata_rows: list[tuple[object, ...]] = []
        parsed_by_file: dict[str, list[Symbol]] = {}
        class_ids_by_file: dict[str, dict[str, str]] = {}

        for f in files:
            if f.language not in GRAMMAR_MAP:
                continue
            try:
                items = parse_file(f.absolute_path, f.language)
            except ParseError as exc:
                warnings.append(f"skipped symbols for {f.file_path}: {exc}")
                continue

            parsed_by_file[f.file_path] = items
            file_id = _stable_id(f.file_path)
            file_class_ids: dict[str, str] = {}
            for idx, item in enumerate(items):
                symbol_id = _symbol_id(f.file_path, item, idx)
                symbol_rows.append(_symbol_row(item, symbol_id, file_id, f.file_path))
                if item.kind == "class":
                    file_class_ids[item.name] = symbol_id
                metadata_rows.append(_metadata_row(symbol_id, _infer_symbol_metadata(item)))
            class_ids_by_file[file_id] = file_class_ids

        if symbol_rows:
            self._executemany(
                "INSERT INTO symbols "
                "(symbol_id, name, qualified_name, file_id, symbol_type, "
                "start_line, end_line, parent_class) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                symbol_rows,
            )

        hierarchy_rows, member_rows = self._build_class_relations(parsed_by_file, class_ids_by_file)
        return warnings, hierarchy_rows, member_rows, metadata_rows

    @staticmethod
    def _build_class_relations(
        parsed_by_file: dict[str, list[Symbol]], class_ids_by_file: dict[str, dict[str, str]]
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Derive class-hierarchy and class-membership edges from parsed symbols."""
        hierarchy_rows: list[tuple[str, str]] = []
        member_rows: list[tuple[str, str]] = []

        for file_path, items in parsed_by_file.items():
            file_id = _stable_id(file_path)
            file_class_ids = class_ids_by_file.get(file_id, {})
            for idx, item in enumerate(items):
                symbol_id = _symbol_id(file_path, item, idx)
                if item.kind == "class":
                    for base_name in item.bases:
                        parent_id = file_class_ids.get(base_name)
                        if parent_id:
                            hierarchy_rows.append((symbol_id, parent_id))
                elif item.kind == "function" and item.parent_class:
                    class_id = file_class_ids.get(item.parent_class)
                    if class_id:
                        member_rows.append((class_id, symbol_id))

        return hierarchy_rows, member_rows

    def _populate_symbol_metadata(self, rows: list[tuple[object, ...]]) -> None:
        if not rows:
            return
        self._executemany(
            "INSERT INTO symbol_metadata "
            "(symbol_id, kind, http_method, http_path, event_name, cli_command) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _populate_class_hierarchy(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        self._executemany(
            "INSERT OR IGNORE INTO class_hierarchy "
            "(class_symbol_id, parent_symbol_id) VALUES (?, ?)",
            rows,
        )

    def _populate_class_members(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        self._executemany(
            "INSERT OR IGNORE INTO class_members (class_symbol_id, member_symbol_id) VALUES (?, ?)",
            rows,
        )

    def _populate_symbol_calls(self, files: list[ScannedFile]) -> None:
        all_calls = extract_calls_batch(files)
        if not all_calls:
            return

        symbol_by_qname: dict[str, str] = {}
        symbols_by_name: dict[str, list[tuple[str, str]]] = {}
        for sid, qname, name, fpath in self._fetchall(
            "SELECT s.symbol_id, s.qualified_name, s.name, f.path "
            "FROM symbols s JOIN files f ON s.file_id = f.file_id"
        ):
            symbol_by_qname[qname] = sid
            symbols_by_name.setdefault(name, []).append((sid, fpath))

        rows: list[tuple[str, str, int]] = []
        resolved = 0
        unresolved = 0
        for call in all_calls:
            caller_id = symbol_by_qname.get(call.caller)
            if caller_id is None:
                unresolved += 1
                continue

            caller_file = call.caller.rsplit(":", 1)[0]
            candidates = symbols_by_name.get(call.callee_name, [])
            callee_id = next((sid for sid, fpath in candidates if fpath == caller_file), None)
            if callee_id is None and candidates:
                callee_id = candidates[0][0]
            if callee_id is None:
                unresolved += 1
                continue

            rows.append((caller_id, callee_id, call.line))
            resolved += 1

        if rows:
            self._executemany(
                "INSERT OR IGNORE INTO symbol_calls (caller_symbol_id, callee_symbol_id, line) "
                "VALUES (?, ?, ?)",
                rows,
            )
        logger.info(
            "symbol calls: %d resolved, %d unresolved (stdlib/3rd-party)", resolved, unresolved
        )

    def _populate_modules(self, module_result: ModuleDetectionResult) -> None:
        if module_result.modules:
            self._executemany(
                "INSERT INTO modules (name, file_count) VALUES (?, ?)",
                [(name, info.file_count) for name, info in module_result.modules.items()],
            )

        rows = [
            (source, target)
            for source, targets in module_result.module_graph.items()
            for target in targets
        ]
        if rows:
            self._executemany("INSERT INTO module_deps (source, target) VALUES (?, ?)", rows)

    # ─── Read queries ───────────────────────────────────────────────

    def get_import_edges(self) -> list[tuple[str, str]]:
        """All file-level import edges as (source_path, target_path) tuples."""
        return self._fetchall(
            "SELECT sf.path, tf.path FROM imports i "
            "JOIN files sf ON i.source_file_id = sf.file_id "
            "JOIN files tf ON i.target_file_id = tf.file_id"
        )

    def get_module_dep_edges(self) -> list[tuple[str, str]]:
        return self._fetchall("SELECT source, target FROM module_deps")

    def get_module_file(self, file_path: str) -> str | None:
        result = self._fetchone(
            "SELECT module FROM files WHERE file_id = ?", [_stable_id(file_path)]
        )
        return result[0] if result else None

    def get_files_in_module(self, module_name: str) -> list[str]:
        return [
            row[0]
            for row in self._fetchall("SELECT path FROM files WHERE module = ?", [module_name])
        ]

    def get_modules(self) -> dict[str, ModuleInfo]:
        """Reconstruct per-module file lists + language breakdown from persisted data.

        Fully reconstructible from `files`/`modules` alone -- no separate
        in-memory cache of the original `ModuleDetectionResult` is needed.
        """
        modules: dict[str, ModuleInfo] = {}
        rows = self._fetchall("SELECT name, file_count FROM modules")
        for name, file_count in rows:
            files = sorted(self.get_files_in_module(name))
            languages = dict(
                self._fetchall(
                    "SELECT language, COUNT(*) FROM files WHERE module = ? GROUP BY language",
                    [name],
                )
            )
            modules[name] = ModuleInfo(files=files, languages=languages, file_count=file_count)
        return modules

    def get_symbols_in_file(self, file_path: str) -> list[SymbolInfo]:
        rows = self._fetchall(
            "SELECT symbol_id, name, qualified_name, symbol_type, start_line, end_line "
            "FROM symbols WHERE file_id = ? ORDER BY start_line",
            [_stable_id(file_path)],
        )
        return [SymbolInfo(*row) for row in rows]

    def find_symbols_by_name(self, name: str) -> list[SymbolMatch]:
        """Case-insensitive exact-name symbol lookup, across all files."""
        rows = self._fetchall(
            "SELECT s.symbol_id, s.name, s.qualified_name, f.path, s.symbol_type, "
            "s.start_line, s.end_line, s.parent_class "
            "FROM symbols s JOIN files f ON s.file_id = f.file_id "
            "WHERE LOWER(s.name) = LOWER(?) ORDER BY f.path, s.start_line",
            [name],
        )
        return [SymbolMatch(*row) for row in rows]

    def find_symbols_containing(self, substring: str, limit: int = 8) -> list[SymbolMatch]:
        """Case-insensitive substring symbol match, for "did you mean" suggestions."""
        rows = self._fetchall(
            "SELECT s.symbol_id, s.name, s.qualified_name, f.path, s.symbol_type, "
            "s.start_line, s.end_line, s.parent_class "
            "FROM symbols s JOIN files f ON s.file_id = f.file_id "
            "WHERE LOWER(s.name) LIKE LOWER(?) ORDER BY f.path, s.start_line LIMIT ?",
            [f"%{substring}%", limit],
        )
        return [SymbolMatch(*row) for row in rows]

    def get_all_files(self) -> list[str]:
        return [row[0] for row in self._fetchall("SELECT path FROM files")]

    def get_importers(self, file_path: str) -> list[str]:
        return [
            row[0]
            for row in self._fetchall(
                "SELECT f.path FROM imports i JOIN files f ON i.source_file_id = f.file_id "
                "WHERE i.target_file_id = ?",
                [_stable_id(file_path)],
            )
        ]

    def get_imports(self, file_path: str) -> list[str]:
        return [
            row[0]
            for row in self._fetchall(
                "SELECT f.path FROM imports i JOIN files f ON i.target_file_id = f.file_id "
                "WHERE i.source_file_id = ?",
                [_stable_id(file_path)],
            )
        ]

    def _count(self, table: str) -> int:
        """`SELECT COUNT(*)` for a fixed, internal table name."""
        row = self._fetchone(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        return int(row[0]) if row else 0

    def get_stats(self) -> GraphStats:
        return GraphStats(
            files=self._count("files"),
            imports=self._count("imports"),
            symbols=self._count("symbols"),
            symbol_calls=self._count("symbol_calls"),
            modules=self._count("modules"),
        )

    def get_callers_of_symbol(self, qualified_name: str) -> list[CallerInfo]:
        """Who calls this symbol? (name, qualified name, file, call-site line)."""
        result = self._fetchone(
            "SELECT symbol_id FROM symbols WHERE qualified_name = ?", [qualified_name]
        )
        if not result:
            return []
        rows = self._fetchall(
            "SELECT s.name, s.qualified_name, f.path, sc.line "
            "FROM symbol_calls sc "
            "JOIN symbols s ON sc.caller_symbol_id = s.symbol_id "
            "JOIN files f ON s.file_id = f.file_id "
            "WHERE sc.callee_symbol_id = ? ORDER BY f.path, sc.line",
            [result[0]],
        )
        return [CallerInfo(*row) for row in rows]

    def get_callees_of_symbol(self, qualified_name: str) -> list[CalleeInfo]:
        """What does this symbol call? (name, qualified name, file, call-site line)."""
        result = self._fetchone(
            "SELECT symbol_id FROM symbols WHERE qualified_name = ?", [qualified_name]
        )
        if not result:
            return []
        rows = self._fetchall(
            "SELECT s.name, s.qualified_name, f.path, sc.line "
            "FROM symbol_calls sc "
            "JOIN symbols s ON sc.callee_symbol_id = s.symbol_id "
            "JOIN files f ON s.file_id = f.file_id "
            "WHERE sc.caller_symbol_id = ? ORDER BY sc.line",
            [result[0]],
        )
        return [CalleeInfo(*row) for row in rows]

    def close(self) -> None:
        """Close the DuckDB connection."""
        self.conn.close()
