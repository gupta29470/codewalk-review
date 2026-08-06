"""Tests for codewalk.graph.graph_store."""

from __future__ import annotations

import multiprocessing
import multiprocessing.synchronize
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from codewalk.analysis.dependency_graph import build_dependency_graph
from codewalk.analysis.module_detector import detect_modules
from codewalk.errors import ConfigError, GraphCorruptedError, GraphLockError
from codewalk.graph.graph_store import GraphStore
from tests.conftest import write_repo_files


def _build_store(tmp_path: Path, files: dict[str, str]) -> tuple[GraphStore, Path]:
    root = tmp_path / "repo"
    scanned = write_repo_files(root, files)
    dep_result = build_dependency_graph(scanned)
    module_result = detect_modules(scanned, dep_graph=dep_result.graph)
    db_path = root / ".codewalk" / "graph.duckdb"
    store = GraphStore(db_path)
    store.populate_from_analysis(scanned, dep_result.graph, module_result)
    return store, db_path


class TestBuildAndQuery:
    def test_two_file_repo_end_to_end(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        try:
            stats = store.get_stats()
            assert stats.files == 2
            assert stats.imports == 1
            assert stats.symbols == 2
            assert "utils.py" in store.get_all_files()
            assert store.get_importers("utils.py") == ["main.py"]
            assert store.get_imports("main.py") == ["utils.py"]
        finally:
            store.close()

    def test_empty_repo_does_not_crash(self, tmp_path: Path) -> None:
        store, _ = _build_store(tmp_path, {})
        try:
            stats = store.get_stats()
            assert stats.files == 0
            assert stats.symbols == 0
        finally:
            store.close()

    def test_reopen_existing_db_is_idempotent(self, tmp_path: Path) -> None:
        store, db_path = _build_store(tmp_path, {"main.py": "x = 1\n"})
        store.close()

        reopened = GraphStore(db_path)
        try:
            assert reopened.get_stats().files == 1
        finally:
            reopened.close()

    def test_class_hierarchy_and_members_populated(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path,
            {
                "mod.py": (
                    "class Base:\n"
                    "    pass\n"
                    "\n\n"
                    "class Child(Base):\n"
                    "    def run(self):\n"
                    "        pass\n"
                )
            },
        )
        try:
            symbols = store.get_symbols_in_file("mod.py")
            names = {s.name for s in symbols}
            assert names == {"Base", "Child", "run"}
        finally:
            store.close()

    def test_symbol_metadata_entrypoint_detection(self, tmp_path: Path) -> None:
        store, _ = _build_store(tmp_path, {"mod.py": "def main():\n    pass\n"})
        try:
            row = store.conn.execute(
                "SELECT kind FROM symbol_metadata sm JOIN symbols s ON sm.symbol_id = s.symbol_id "
                "WHERE s.name = 'main'"
            ).fetchone()
            assert row is not None
            assert row[0] == "entrypoint"
        finally:
            store.close()

    def test_symbol_metadata_service_class_detection(self, tmp_path: Path) -> None:
        store, _ = _build_store(tmp_path, {"mod.py": "class UserService:\n    pass\n"})
        try:
            row = store.conn.execute(
                "SELECT kind FROM symbol_metadata sm JOIN symbols s ON sm.symbol_id = s.symbol_id "
                "WHERE s.name = 'UserService'"
            ).fetchone()
            assert row is not None
            assert row[0] == "service"
        finally:
            store.close()

    def test_symbol_metadata_model_class_detection(self, tmp_path: Path) -> None:
        store, _ = _build_store(tmp_path, {"mod.py": "class UserModel:\n    pass\n"})
        try:
            row = store.conn.execute(
                "SELECT kind FROM symbol_metadata sm JOIN symbols s ON sm.symbol_id = s.symbol_id "
                "WHERE s.name = 'UserModel'"
            ).fetchone()
            assert row is not None
            assert row[0] == "model"
        finally:
            store.close()

    def test_symbol_metadata_route_decorator_detection(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path, {"mod.py": "@app.get('/users')\ndef list_users():\n    pass\n"}
        )
        try:
            row = store.conn.execute(
                "SELECT kind, http_method, http_path FROM symbol_metadata sm "
                "JOIN symbols s ON sm.symbol_id = s.symbol_id WHERE s.name = 'list_users'"
            ).fetchone()
            assert row == ("route", "GET", "/users")
        finally:
            store.close()

    def test_symbol_metadata_cli_decorator_detection(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path, {"mod.py": "@cli.command('sync')\ndef do_sync():\n    pass\n"}
        )
        try:
            row = store.conn.execute(
                "SELECT kind, cli_command FROM symbol_metadata sm "
                "JOIN symbols s ON sm.symbol_id = s.symbol_id WHERE s.name = 'do_sync'"
            ).fetchone()
            assert row == ("cli", "sync")
        finally:
            store.close()

    def test_symbol_with_no_special_metadata_has_null_kind(self, tmp_path: Path) -> None:
        store, _ = _build_store(tmp_path, {"mod.py": "def plain_helper():\n    pass\n"})
        try:
            row = store.conn.execute(
                "SELECT kind FROM symbol_metadata sm "
                "JOIN symbols s ON sm.symbol_id = s.symbol_id WHERE s.name = 'plain_helper'"
            ).fetchone()
            assert row == (None,)
        finally:
            store.close()

    def test_get_callers_and_callees_of_symbol(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        try:
            callers = store.get_callers_of_symbol("utils.py:helper")
            assert any(c.caller == "run" for c in callers)
            callees = store.get_callees_of_symbol("main.py:run")
            assert any(c.callee == "helper" for c in callees)
        finally:
            store.close()

    def test_unreadable_file_produces_warning_not_crash(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        scanned = write_repo_files(root, {"main.py": "x = 1\n"})
        scanned[0].absolute_path.unlink()
        dep_result = build_dependency_graph(scanned)
        module_result = detect_modules(scanned, dep_graph=dep_result.graph)
        store = GraphStore(root / ".codewalk" / "graph.duckdb")
        try:
            warnings = store.populate_from_analysis(scanned, dep_result.graph, module_result)
            assert any("main.py" in w for w in warnings)
        finally:
            store.close()

    def test_module_lookup_helpers(self, tmp_path: Path) -> None:
        store, _ = _build_store(
            tmp_path, {"src/auth/login.py": "x = 1\n", "src/billing/pay.py": "x = 1\n"}
        )
        try:
            assert store.get_module_file("src/auth/login.py") == "auth"
            assert store.get_files_in_module("auth") == ["src/auth/login.py"]
        finally:
            store.close()


class TestUnwritableDirectory:
    def test_unwritable_codewalk_dir_raises_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere regardless of permissions")

        def fail_mkdir(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        with pytest.raises(ConfigError):
            GraphStore(tmp_path / "nested" / "graph.duckdb")


class TestLockHandling:
    def test_corruption_error_is_not_treated_as_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_connect(path: str) -> None:
            raise duckdb.IOException("database disk image is malformed")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        with pytest.raises(GraphCorruptedError):
            GraphStore(tmp_path / "graph.duckdb")

    def test_dead_pid_cleans_up_and_retries_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "graph.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        wal = Path(f"{db_path}.wal")
        wal.write_text("stale")

        # A PID that is essentially guaranteed not to exist.
        dead_pid = 999_999

        attempts = {"count": 0}
        real_connect = duckdb.connect

        def fake_connect(path: str) -> object:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise duckdb.IOException(f"Could not set lock on file, PID {dead_pid}")
            return real_connect(path)

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        store = GraphStore(db_path, retries=3, retry_delay=0.01)
        try:
            # Confirms the dead-PID branch ran (cleanup + immediate retry,
            # not a slept-then-retry): exactly one failed attempt, then success.
            assert attempts["count"] == 2
        finally:
            store.close()

    def test_permission_error_from_os_kill_falls_back_to_normal_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "graph.duckdb"
        attempts = {"count": 0}
        real_connect = duckdb.connect

        def fake_connect(path: str) -> object:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise duckdb.IOException("Could not set lock on file, PID 123")
            return real_connect(path)

        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError("cannot signal")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        monkeypatch.setattr(os, "kill", fake_kill)
        sleeps: list[float] = []
        store = GraphStore(
            db_path, retries=3, retry_delay=0.01, sleep_fn=sleeps.append, now_fn=time.monotonic
        )
        try:
            assert attempts["count"] == 2
            assert sleeps == [0.01]  # went through the normal backoff path, not the dead-PID path
        finally:
            store.close()

    def test_unparseable_pid_still_terminates_with_lock_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_connect(path: str) -> None:
            raise duckdb.IOException("Could not set lock on file (no pid info here)")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        with pytest.raises(GraphLockError):
            GraphStore(tmp_path / "graph.duckdb", retries=2, retry_delay=0.0)

    def test_alive_pid_retries_then_raises_lock_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        current_pid = os.getpid()  # definitely alive

        def fake_connect(path: str) -> None:
            raise duckdb.IOException(f"Could not set lock on file, PID {current_pid}")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        with pytest.raises(GraphLockError) as exc_info:
            GraphStore(tmp_path / "graph.duckdb", retries=2, retry_delay=0.0)
        assert str(current_pid) in str(exc_info.value)

    def test_wall_clock_budget_exceeded_raises_before_attempt_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_connect(path: str) -> None:
            raise duckdb.IOException("Could not set lock on file, PID 123456789")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))

        fake_time = {"value": 0.0}

        def fake_now() -> float:
            return fake_time["value"]

        def fake_sleep(seconds: float) -> None:
            fake_time["value"] += 100.0  # jump far past any wall-clock budget

        with pytest.raises(GraphLockError):
            GraphStore(
                tmp_path / "graph.duckdb",
                retries=1000,
                retry_delay=1.0,
                max_wait_seconds=10.0,
                sleep_fn=fake_sleep,
                now_fn=fake_now,
            )

    def test_cleanup_failure_does_not_raise_and_still_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "graph.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        dead_pid = 999_998
        attempts = {"count": 0}
        real_connect = duckdb.connect

        def fake_connect(path: str) -> object:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise duckdb.IOException(f"Could not set lock on file, PID {dead_pid}")
            return real_connect(path)

        def fake_unlink(self: Path, missing_ok: bool = False) -> None:
            raise OSError("permission denied removing lock file")

        monkeypatch.setattr(duckdb, "connect", fake_connect)
        monkeypatch.setattr(Path, "unlink", fake_unlink)

        store = GraphStore(db_path, retries=3, retry_delay=0.01)
        try:
            assert attempts["count"] == 2  # still retried despite cleanup failure
        finally:
            store.close()


def _hold_duckdb_lock(
    db_path: str,
    ready_flag: multiprocessing.synchronize.Event,
    release_flag: multiprocessing.synchronize.Event,
) -> None:
    """Child process: open the DB and hold the connection until told to release."""
    conn = duckdb.connect(db_path)
    ready_flag.set()
    release_flag.wait(timeout=10)
    conn.close()


@pytest.mark.integration
class TestRealConcurrentProcesses:
    def test_second_process_retries_until_first_releases_lock(self, tmp_path: Path) -> None:
        db_path = tmp_path / "graph.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        holder = ctx.Process(target=_hold_duckdb_lock, args=(str(db_path), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=10), "holder process never opened the database"

            def release_after_delay(seconds: float) -> None:
                release.set()

            store = GraphStore(
                db_path,
                retries=20,
                retry_delay=0.2,
                max_wait_seconds=15.0,
                sleep_fn=release_after_delay,
            )
            store.close()
        finally:
            release.set()
            holder.join(timeout=10)


class TestConcurrentThreadedQueries:
    """A single `GraphStore` (and its one DuckDB connection) shared across
    threads -- e.g. one cached `Workspace` serving multiple concurrent MCP
    tool calls. Every query must be serialized so no thread ever observes a
    result row from a different thread's query."""

    def test_concurrent_get_symbols_in_file_no_corrupted_rows(self, tmp_path: Path) -> None:
        files = {f"f{i}.py": f"def func_{i}():\n    return {i}\n" for i in range(20)}
        store, _db_path = _build_store(tmp_path, files)

        errors: list[Exception] = []

        def _query(idx: int) -> None:
            try:
                for _ in range(20):
                    symbols = store.get_symbols_in_file(f"f{idx}.py")
                    assert len(symbols) == 1
                    assert symbols[0].name == f"func_{idx}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_query, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_mixed_reads_no_corruption(self, tmp_path: Path) -> None:
        files = {
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "utils.py": "def helper():\n    return 1\n",
        }
        store, _db_path = _build_store(tmp_path, files)

        errors: list[Exception] = []

        def _repeat(action: Callable[[], object]) -> None:
            try:
                for _ in range(30):
                    action()
            except Exception as exc:
                errors.append(exc)

        actions = [
            lambda: store.get_symbols_in_file("utils.py"),
            lambda: store.get_callers_of_symbol("utils.py:helper"),
            lambda: store.get_stats(),
        ]
        threads = [threading.Thread(target=_repeat, args=(action,)) for action in actions * 3]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
