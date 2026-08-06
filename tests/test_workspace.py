"""Tests for codewalk.workspace.Workspace (single-repo lifecycle)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codewalk import workspace as workspace_module
from codewalk.workspace import Workspace


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for relative_path, content in files.items():
        full = root / relative_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return root


class TestBuild:
    def test_build_populates_graph(self, tmp_path: Path) -> None:
        root = _make_repo(
            tmp_path,
            {
                "utils.py": "def helper():\n    return 1\n",
                "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        ws = Workspace.build(root)
        try:
            stats = ws.graph_store.get_stats()
            assert stats.files == 2
            assert stats.imports == 1
            assert ws.graph_runtime.file_graph.vcount() == 2
            assert ws.last_build_warnings is not None
            assert ws.last_build_warnings.all() == []
        finally:
            ws.close()

    def test_build_records_fingerprint(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        try:
            assert ws.fingerprint.file_count == 1
            assert ws.is_stale() is False
        finally:
            ws.close()

    def test_build_saves_fingerprint_to_disk(self, tmp_path: Path) -> None:
        from codewalk.staleness import load_fingerprint

        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        try:
            loaded = load_fingerprint(root)
            assert loaded == ws.fingerprint
        finally:
            ws.close()

    def test_empty_repo_does_not_crash(self, tmp_path: Path) -> None:
        root = tmp_path / "empty_repo"
        root.mkdir()
        ws = Workspace.build(root)
        try:
            assert ws.graph_store.get_stats().files == 0
        finally:
            ws.close()


class TestOpenOrBuild:
    def test_reopens_without_rescanning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        ws.close()

        scan_calls = {"count": 0}
        original_scan_repo = workspace_module.scan_repo

        def counting_scan_repo(*args: object, **kwargs: object) -> object:
            scan_calls["count"] += 1
            return original_scan_repo(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(workspace_module, "scan_repo", counting_scan_repo)

        reopened = Workspace.open_or_build(root)
        try:
            assert scan_calls["count"] == 0
            assert reopened.graph_store.get_stats().files == 1
        finally:
            reopened.close()

    def test_falls_back_to_build_when_no_existing_db(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.open_or_build(root)
        try:
            assert ws.graph_store.get_stats().files == 1
            assert ws.last_build_warnings is not None
        finally:
            ws.close()

    def test_reopened_workspace_restores_fingerprint(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        original = Workspace.build(root)
        original_fingerprint = original.fingerprint
        original.close()

        reopened = Workspace.open_or_build(root)
        try:
            assert reopened.fingerprint == original_fingerprint
        finally:
            reopened.close()


class TestRefresh:
    def test_refresh_picks_up_new_file(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        try:
            assert ws.graph_store.get_stats().files == 1

            (root / "b.py").write_text("y = 2\n", encoding="utf-8")
            warnings = ws.refresh()

            assert ws.graph_store.get_stats().files == 2
            assert ws.graph_runtime.file_graph.vcount() >= 0  # rebuilt, doesn't crash
            assert warnings.all() == []
        finally:
            ws.close()

    def test_refresh_updates_fingerprint(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        try:
            before = ws.fingerprint
            (root / "b.py").write_text("y = 2\n", encoding="utf-8")
            ws.refresh()
            assert ws.fingerprint.file_count != before.file_count
        finally:
            ws.close()


class TestClose:
    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, {"a.py": "x = 1\n"})
        ws = Workspace.build(root)
        ws.close()
        ws.close()  # must not raise
