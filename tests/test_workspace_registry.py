"""Tests for codewalk.workspace.WorkspaceRegistry.

This is the direct regression suite for "switch repo A -> B, does state
correctly update?" -- the exact bug class upstream's global-singleton
`_graph_store`/`_repo_path` pattern was vulnerable to. The registry fixes it
by construction (no "currently active repo" global at all), verified here.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import duckdb
import pytest

from codewalk.errors import RepoNotConfiguredError
from codewalk.workspace import Workspace, WorkspaceRegistry


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        full = root / relative_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return root


class TestBasicGetOrBuild:
    def test_repeated_calls_return_the_same_cached_workspace(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
        registry = WorkspaceRegistry()
        try:
            first = registry.get_or_build(root)
            second = registry.get_or_build(root)
            assert first is second
        finally:
            registry.close_all()

    def test_nonexistent_repo_raises(self, tmp_path: Path) -> None:
        registry = WorkspaceRegistry()
        with pytest.raises(RepoNotConfiguredError):
            registry.get_or_build(tmp_path / "does_not_exist")


class TestRepoSwitching:
    """The A -> B -> A regression suite."""

    def test_switching_between_two_repos_does_not_cross_contaminate(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"only_in_a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"only_in_b.py": "y = 2\n"})
        registry = WorkspaceRegistry(max_size=3)
        try:
            ws_a = registry.get_or_build(repo_a)
            assert ws_a.graph_store.get_all_files() == ["only_in_a.py"]

            # Without closing A, request B -- must reflect B's data, not A's.
            ws_b = registry.get_or_build(repo_b)
            assert ws_b.graph_store.get_all_files() == ["only_in_b.py"]

            # A must still be correct too (not clobbered by B).
            ws_a_again = registry.get_or_build(repo_a)
            assert ws_a_again.graph_store.get_all_files() == ["only_in_a.py"]
        finally:
            registry.close_all()

    def test_a_to_b_to_a_survives_eviction_with_identical_results(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"only_in_a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"only_in_b.py": "y = 2\n"})
        # max_size=1 forces A to be evicted the moment B is requested.
        registry = WorkspaceRegistry(max_size=1)
        try:
            for _ in range(10):  # run repeatedly to catch any residual shared state
                ws_a = registry.get_or_build(repo_a)
                assert ws_a.graph_store.get_all_files() == ["only_in_a.py"]

                ws_b = registry.get_or_build(repo_b)
                assert ws_b.graph_store.get_all_files() == ["only_in_b.py"]

                ws_a_reopened = registry.get_or_build(repo_a)
                assert ws_a_reopened.graph_store.get_all_files() == ["only_in_a.py"]
        finally:
            registry.close_all()

    def test_two_repos_open_simultaneously_under_cap(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"b.py": "y = 2\n"})
        registry = WorkspaceRegistry(max_size=2)
        try:
            ws_a = registry.get_or_build(repo_a)
            ws_b = registry.get_or_build(repo_b)
            assert ws_a.graph_store.get_all_files() == ["a.py"]
            assert ws_b.graph_store.get_all_files() == ["b.py"]
            assert ws_a is registry.get_or_build(repo_a)
            assert ws_b is registry.get_or_build(repo_b)
        finally:
            registry.close_all()


class TestEviction:
    def test_forced_eviction_closes_the_oldest_workspace(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"b.py": "y = 2\n"})
        registry = WorkspaceRegistry(max_size=1)
        try:
            ws_a = registry.get_or_build(repo_a)
            db_path_a = ws_a.graph_store.db_path

            registry.get_or_build(repo_b)  # forces A's eviction

            # A's DuckDB connection must really be closed -- a brand new,
            # independent connection to the same file should succeed immediately.
            direct_conn = duckdb.connect(str(db_path_a))
            direct_conn.close()
        finally:
            registry.close_all()

    def test_evicted_repo_reopens_transparently(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"b.py": "y = 2\n"})
        registry = WorkspaceRegistry(max_size=1)
        try:
            registry.get_or_build(repo_a)
            registry.get_or_build(repo_b)  # evicts A

            reopened_a = registry.get_or_build(repo_a)
            assert reopened_a.graph_store.get_all_files() == ["a.py"]
        finally:
            registry.close_all()


class TestPathNormalization:
    def test_symlinked_path_and_real_path_share_one_workspace(self, tmp_path: Path) -> None:
        real_root = _make_repo(tmp_path / "real_repo", {"a.py": "x = 1\n"})
        symlink_root = tmp_path / "symlinked_repo"
        symlink_root.symlink_to(real_root, target_is_directory=True)

        registry = WorkspaceRegistry()
        try:
            via_real = registry.get_or_build(real_root)
            via_symlink = registry.get_or_build(symlink_root)
            assert via_real is via_symlink
        finally:
            registry.close_all()


class TestDeletedRepo:
    def test_deleted_repo_raises_on_next_access_not_file_not_found(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
        registry = WorkspaceRegistry()
        try:
            registry.get_or_build(root)
            shutil.rmtree(root)
            with pytest.raises(RepoNotConfiguredError):
                registry.get_or_build(root)
        finally:
            registry.close_all()


class TestConcurrentAccess:
    def test_concurrent_get_or_build_for_same_repo_builds_only_once(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
        registry = WorkspaceRegistry()
        build_calls = {"count": 0}
        original_build = Workspace.open_or_build

        def counting_open_or_build(repo_root: Path, config: object = None) -> Workspace:
            build_calls["count"] += 1
            return original_build(repo_root, config)  # type: ignore[arg-type]

        Workspace.open_or_build = classmethod(  # type: ignore[method-assign]
            lambda cls, repo_root, config=None: counting_open_or_build(repo_root, config)
        )
        try:
            results: list[Workspace] = []
            barrier = threading.Barrier(4)

            def worker() -> None:
                barrier.wait(timeout=5)
                results.append(registry.get_or_build(root))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert build_calls["count"] == 1
            assert len({id(ws) for ws in results}) == 1
        finally:
            Workspace.open_or_build = original_build  # type: ignore[method-assign]
            registry.close_all()

    def test_query_blocks_until_in_flight_build_finishes(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
        registry = WorkspaceRegistry()
        real_refresh = Workspace.refresh
        try:
            registry.get_or_build(root)  # prime the workspace + its per-repo lock

            refresh_started = threading.Event()
            allow_refresh_to_finish = threading.Event()
            order: list[str] = []
            order_lock = threading.Lock()

            def slow_refresh(self: Workspace) -> object:
                with order_lock:
                    order.append("refresh-start")
                refresh_started.set()
                allow_refresh_to_finish.wait(timeout=5)
                result = real_refresh(self)
                with order_lock:
                    order.append("refresh-end")
                return result

            Workspace.refresh = slow_refresh  # type: ignore[method-assign]

            def do_refresh() -> None:
                registry.refresh(root)

            def do_get() -> None:
                assert refresh_started.wait(timeout=5), "refresh never started"
                with order_lock:
                    order.append("get-called")
                registry.get_or_build(root)  # must block until refresh releases the lock
                with order_lock:
                    order.append("get-returned")

            t_refresh = threading.Thread(target=do_refresh)
            t_get = threading.Thread(target=do_get)
            t_refresh.start()
            t_get.start()

            assert refresh_started.wait(timeout=5), "refresh never started"
            time.sleep(0.1)  # give do_get a chance to reach the blocking lock acquire
            allow_refresh_to_finish.set()

            t_refresh.join(timeout=10)
            t_get.join(timeout=10)

            assert order.index("refresh-end") < order.index("get-returned")
        finally:
            Workspace.refresh = real_refresh  # type: ignore[method-assign]
            registry.close_all()


class TestCloseAll:
    def test_close_all_closes_every_cached_workspace(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path / "repo_a", {"a.py": "x = 1\n"})
        repo_b = _make_repo(tmp_path / "repo_b", {"b.py": "y = 2\n"})
        registry = WorkspaceRegistry(max_size=2)
        ws_a = registry.get_or_build(repo_a)
        ws_b = registry.get_or_build(repo_b)

        registry.close_all()

        # Both connections must really be closed -- fresh direct connections succeed.
        duckdb.connect(str(ws_a.graph_store.db_path)).close()
        duckdb.connect(str(ws_b.graph_store.db_path)).close()
