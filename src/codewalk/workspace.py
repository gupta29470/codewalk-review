"""`Workspace`: ties ingestion + analysis + graph together for one repo, and
`WorkspaceRegistry`: an LRU cache of workspaces keyed by normalized repo root.

Replaces the upstream pattern of global mutable module-level state
(`_graph_store`, `_repo_path`, ...) entirely. There is no "currently active
repo" global to forget to update -- every caller resolves a repo root fresh
and asks the registry for that repo's `Workspace`, which fixes the classic
A -> B -> A stale-handle bug by construction rather than by remembering to
call `close()` at the right spot.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from codewalk.analysis.dependency_graph import build_dependency_graph
from codewalk.analysis.module_detector import detect_modules
from codewalk.codewalk_config import CodewalkConfig, load_codewalk_yaml
from codewalk.errors import RepoNotConfiguredError
from codewalk.graph.graph_runtime import GraphRuntime
from codewalk.graph.graph_store import GraphStore
from codewalk.ingestion.scanner import scan_repo
from codewalk.log import get_logger
from codewalk.paths import graph_db_path
from codewalk.staleness import (
    RepoFingerprint,
    compute_fingerprint,
    format_staleness_warning,
    is_stale,
    load_fingerprint,
    save_fingerprint,
)

logger = get_logger(__name__)

DEFAULT_MAX_WORKSPACES = 3


@dataclass
class BuildWarnings:
    """Recoverable per-file warnings collected during a build/refresh."""

    scan: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    def all(self) -> list[str]:
        return [*self.scan, *self.dependencies, *self.symbols]


def _analyze_and_populate(
    repo_root: Path, config: CodewalkConfig, store: GraphStore
) -> tuple[BuildWarnings, RepoFingerprint]:
    """Scan, build the dependency/module graphs, and populate `store`.

    Shared by `Workspace.build()` (fresh `GraphStore`) and `Workspace.refresh()`
    (existing `GraphStore`, repopulated in place).
    """
    scan_result = scan_repo(repo_root, config)
    dep_result = build_dependency_graph(scan_result.files)
    module_result = detect_modules(scan_result.files, dep_graph=dep_result.graph)
    symbol_warnings = store.populate_from_analysis(
        scan_result.files, dep_result.graph, module_result
    )

    fingerprint = compute_fingerprint(repo_root, scan_result.files)
    save_fingerprint(repo_root, fingerprint)

    warnings = BuildWarnings(
        scan=scan_result.warnings, dependencies=dep_result.warnings, symbols=symbol_warnings
    )
    return warnings, fingerprint


class Workspace:
    """A repo's live graph store/runtime, plus staleness bookkeeping."""

    def __init__(
        self,
        repo_root: Path,
        graph_store: GraphStore,
        graph_runtime: GraphRuntime,
        fingerprint: RepoFingerprint,
        config: CodewalkConfig,
        build_warnings: BuildWarnings | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.graph_store = graph_store
        self.graph_runtime = graph_runtime
        self.fingerprint = fingerprint
        self.config = config
        self.last_build_warnings = build_warnings
        self._closed = False

    @classmethod
    def build(cls, repo_root: Path, config: CodewalkConfig | None = None) -> Workspace:
        """Full build: scan -> dependency graph -> modules -> populate a fresh
        `GraphStore` -> `GraphRuntime`. Always rescans, even if a graph DB
        already exists at `repo_root` (use `open_or_build()` to avoid that)."""
        cfg = config or load_codewalk_yaml(repo_root)
        store = GraphStore(graph_db_path(repo_root))
        warnings, fingerprint = _analyze_and_populate(repo_root, cfg, store)
        runtime = GraphRuntime(store)
        logger.info("built workspace for %s", repo_root)
        return cls(repo_root, store, runtime, fingerprint, cfg, build_warnings=warnings)

    @classmethod
    def open_or_build(cls, repo_root: Path, config: CodewalkConfig | None = None) -> Workspace:
        """Reopen an existing graph DB without rescanning if one is already on
        disk; otherwise fall back to a full `build()`."""
        db_path = graph_db_path(repo_root)
        if not db_path.exists():
            return cls.build(repo_root, config)

        cfg = config or load_codewalk_yaml(repo_root)
        store = GraphStore(db_path)
        runtime = GraphRuntime(store)
        fingerprint = load_fingerprint(repo_root) or RepoFingerprint(
            git_head=None, file_count=store.get_stats().files, total_size_bytes=0
        )
        logger.info("reopened existing workspace for %s (no rescan)", repo_root)
        return cls(repo_root, store, runtime, fingerprint, cfg, build_warnings=None)

    def refresh(self) -> BuildWarnings:
        """Rescan and repopulate this workspace's existing `GraphStore` in place."""
        warnings, fingerprint = _analyze_and_populate(self.repo_root, self.config, self.graph_store)
        self.graph_runtime.rebuild()
        self.fingerprint = fingerprint
        self.last_build_warnings = warnings
        logger.info("refreshed workspace for %s", self.repo_root)
        return warnings

    def is_stale(self) -> bool:
        """True if the repo's git HEAD has moved since this workspace was built."""
        return is_stale(self.fingerprint, self.repo_root)

    def staleness_warning(self) -> str | None:
        """A one-line staleness warning, or None if not stale (or unknown)."""
        return format_staleness_warning(self.fingerprint, self.repo_root)

    def close(self) -> None:
        """Close the underlying `GraphStore` connection. Idempotent."""
        if not self._closed:
            self.graph_store.close()
            self._closed = True


def _normalize_repo_root(repo_root: Path | str) -> str:
    """Canonicalize a repo root for use as a registry key.

    Deliberately does NOT lowercase manually: `Path.resolve()` already
    case-corrects on case-insensitive-but-case-preserving filesystems (e.g.
    macOS APFS/HFS+), and manually lowercasing would wrongly conflate two
    genuinely distinct directories on a case-sensitive filesystem (Linux).
    """
    return str(Path(repo_root).resolve())


class WorkspaceRegistry:
    """LRU cache of `Workspace` instances, one per normalized repo root.

    Bounded to `max_size` (default 3) so a long-lived MCP process touching
    many repos doesn't accumulate open DuckDB handles forever; eviction
    closes the evicted workspace and is transparent to callers (a later
    request for the same repo reopens it from disk via `open_or_build()`).
    """

    def __init__(self, max_size: int = DEFAULT_MAX_WORKSPACES) -> None:
        self._max_size = max_size
        self._workspaces: OrderedDict[str, Workspace] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def get_or_build(
        self, repo_root: Path | str, config: CodewalkConfig | None = None
    ) -> Workspace:
        """Return the (possibly cached) `Workspace` for `repo_root`.

        Blocks if a build/refresh for this same repo root is already in
        flight, then returns the freshly built result rather than racing it.

        Raises:
            RepoNotConfiguredError: `repo_root` doesn't exist, or a
                previously cached workspace's repo directory has since been
                deleted from disk.
        """
        root = Path(repo_root).resolve()
        if not root.is_dir():
            raise RepoNotConfiguredError(f"repo_path does not exist or is not a directory: {root}")

        key = _normalize_repo_root(root)
        with self._lock_for(key):
            cached = self._workspaces.get(key)
            if cached is not None:
                if not cached.repo_root.is_dir():
                    self._evict_locked(key)
                    raise RepoNotConfiguredError(
                        f"repo no longer exists on disk: {cached.repo_root}"
                    )
                self._workspaces.move_to_end(key)
                return cached

            workspace = Workspace.open_or_build(root, config)
            self._insert_locked(key, workspace)
            return workspace

    def refresh(self, repo_root: Path | str, config: CodewalkConfig | None = None) -> Workspace:
        """Force a full rescan for `repo_root`, building a workspace if needed."""
        root = Path(repo_root).resolve()
        if not root.is_dir():
            raise RepoNotConfiguredError(f"repo_path does not exist or is not a directory: {root}")

        key = _normalize_repo_root(root)
        with self._lock_for(key):
            cached = self._workspaces.get(key)
            if cached is not None:
                cached.refresh()
                self._workspaces.move_to_end(key)
                return cached

            workspace = Workspace.build(root, config)
            self._insert_locked(key, workspace)
            return workspace

    def _insert_locked(self, key: str, workspace: Workspace) -> None:
        """Insert/replace an entry and evict the LRU tail if over capacity.

        Caller must already hold `self._lock_for(key)`.
        """
        with self._meta_lock:
            self._workspaces[key] = workspace
            self._workspaces.move_to_end(key)
            while len(self._workspaces) > self._max_size:
                _evicted_key, evicted = self._workspaces.popitem(last=False)
                evicted.close()

    def _evict_locked(self, key: str) -> None:
        with self._meta_lock:
            workspace = self._workspaces.pop(key, None)
        if workspace is not None:
            workspace.close()

    def close_all(self) -> None:
        """Close every cached workspace and clear the registry."""
        with self._meta_lock:
            workspaces = list(self._workspaces.values())
            self._workspaces.clear()
        for workspace in workspaces:
            workspace.close()
