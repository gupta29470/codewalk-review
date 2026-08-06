"""Tests for codewalk.staleness."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from codewalk.staleness import (
    RepoFingerprint,
    _cached_github_check,
    _check_behind_github,
    _install_root,
    _wrap_tool_fn,
    compute_fingerprint,
    current_git_head,
    format_staleness_warning,
    github_staleness_banner,
    is_stale,
    load_fingerprint,
    save_fingerprint,
)
from tests.conftest import write_repo_files


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


class TestCurrentGitHead:
    def test_non_git_repo_returns_none(self, tmp_path: Path) -> None:
        assert current_git_head(tmp_path) is None

    def test_git_repo_with_no_commits_returns_none(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        assert current_git_head(tmp_path) is None

    def test_git_repo_with_a_commit_returns_sha(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "initial")
        head = current_git_head(tmp_path)
        assert head is not None
        assert len(head) == 40  # full SHA-1 hex digest


class TestComputeFingerprint:
    def test_records_file_count_and_size(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
        fingerprint = compute_fingerprint(tmp_path, files)
        assert fingerprint.file_count == 2
        assert fingerprint.total_size_bytes == sum(f.size_bytes for f in files)

    def test_non_git_repo_has_none_head(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"a.py": "x = 1\n"})
        fingerprint = compute_fingerprint(tmp_path, files)
        assert fingerprint.git_head is None


class TestIsStale:
    def test_freshly_built_is_not_stale(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "initial")
        fingerprint = RepoFingerprint(
            git_head=current_git_head(tmp_path), file_count=1, total_size_bytes=1
        )
        assert is_stale(fingerprint, tmp_path) is False

    def test_new_commit_makes_it_stale(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "initial")
        fingerprint = RepoFingerprint(
            git_head=current_git_head(tmp_path), file_count=1, total_size_bytes=1
        )

        (tmp_path / "b.txt").write_text("y")
        _commit_all(tmp_path, "second")

        assert is_stale(fingerprint, tmp_path) is True

    def test_no_git_repo_is_unknown_not_stale(self, tmp_path: Path) -> None:
        """No git HEAD at all (never a git repo) -> staleness is "unknown",
        which this function reports as False, not a false positive."""
        fingerprint = RepoFingerprint(git_head=None, file_count=0, total_size_bytes=0)
        assert is_stale(fingerprint, tmp_path) is False

    def test_fingerprint_head_set_but_repo_now_has_no_head_is_not_stale(
        self, tmp_path: Path
    ) -> None:
        """Degenerate case: fingerprint recorded a SHA but the repo's .git was
        since removed -- current_git_head() returns None -> unknown, not stale."""
        fingerprint = RepoFingerprint(git_head="deadbeef" * 5, file_count=0, total_size_bytes=0)
        assert is_stale(fingerprint, tmp_path) is False


class TestFormatStalenessWarning:
    def test_none_when_not_stale(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "initial")
        fingerprint = RepoFingerprint(
            git_head=current_git_head(tmp_path), file_count=1, total_size_bytes=1
        )
        assert format_staleness_warning(fingerprint, tmp_path) is None

    def test_contains_both_short_shas_when_stale(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "a.txt").write_text("x")
        _commit_all(tmp_path, "initial")
        old_head = current_git_head(tmp_path)
        assert old_head is not None
        fingerprint = RepoFingerprint(git_head=old_head, file_count=1, total_size_bytes=1)

        (tmp_path / "b.txt").write_text("y")
        _commit_all(tmp_path, "second")
        new_head = current_git_head(tmp_path)
        assert new_head is not None

        warning = format_staleness_warning(fingerprint, tmp_path)
        assert warning is not None
        assert old_head[:7] in warning
        assert new_head[:7] in warning
        assert "codewalk_refresh_analysis" in warning

    def test_none_when_unknown(self, tmp_path: Path) -> None:
        fingerprint = RepoFingerprint(git_head=None, file_count=0, total_size_bytes=0)
        assert format_staleness_warning(fingerprint, tmp_path) is None


class TestSaveAndLoadFingerprint:
    def test_round_trip(self, tmp_path: Path) -> None:
        fingerprint = RepoFingerprint(git_head="abc123", file_count=5, total_size_bytes=1000)
        save_fingerprint(tmp_path, fingerprint)
        loaded = load_fingerprint(tmp_path)
        assert loaded == fingerprint

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_fingerprint(tmp_path) is None

    def test_corrupted_json_returns_none_not_raises(self, tmp_path: Path) -> None:
        from codewalk.paths import fingerprint_path

        path = fingerprint_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert load_fingerprint(tmp_path) is None

    def test_missing_keys_returns_none_not_raises(self, tmp_path: Path) -> None:
        from codewalk.paths import fingerprint_path

        path = fingerprint_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"git_head": "abc"}', encoding="utf-8")
        assert load_fingerprint(tmp_path) is None

    def test_save_creates_codewalk_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "not_yet_created"
        fingerprint = RepoFingerprint(git_head=None, file_count=0, total_size_bytes=0)
        save_fingerprint(nested, fingerprint)
        assert load_fingerprint(nested) == fingerprint

    def test_save_leaves_no_leftover_tmp_file(self, tmp_path: Path) -> None:
        fingerprint = RepoFingerprint(git_head="abc", file_count=1, total_size_bytes=1)
        save_fingerprint(tmp_path, fingerprint)
        from codewalk.paths import fingerprint_path

        tmp_marker = fingerprint_path(tmp_path).with_suffix(".json.tmp")
        assert not tmp_marker.exists()


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(root: Path, filename: str, message: str) -> None:
    (root / filename).write_text(message)
    _run(["add", "-A"], root)
    _run(["commit", "-q", "-m", message], root)


class TestCheckBehindGithub:
    def test_non_git_repo_returns_none(self, tmp_path: Path) -> None:
        assert _check_behind_github(tmp_path) is None

    def test_dot_git_dir_with_no_commits_returns_none(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)  # .git exists, but there are zero commits yet
        assert _check_behind_github(tmp_path) is None

    def test_clone_with_no_upstream_configured_returns_none(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")

        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)
        # A fresh clone's default branch DOES track origin, so detach HEAD to
        # simulate "no upstream configured".
        _run(["checkout", "-q", "--detach"], clone)

        assert _check_behind_github(clone) is None

    def test_up_to_date_clone_returns_none(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")

        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)

        assert _check_behind_github(clone) is None

    def test_behind_clone_reports_commit_count_and_shas(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")

        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)

        # Push two more commits to "origin" after the clone was made.
        _commit(origin, "b.txt", "second")
        _commit(origin, "c.txt", "third")

        status = _check_behind_github(clone)
        assert status is not None
        assert status["behind_count"] == 2
        assert len(status["local_sha"]) == 7
        assert len(status["remote_sha"]) == 7
        assert status["upstream"].endswith("/master") or status["upstream"].endswith("/main")

    def test_ahead_only_clone_returns_none(self, tmp_path: Path) -> None:
        """Local has new commits origin doesn't have yet -- not "behind", so no banner."""
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")

        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)
        _commit(clone, "local_only.txt", "local work")

        assert _check_behind_github(clone) is None


class TestInstallRoot:
    def test_returns_none_outside_a_codewalk_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codewalk.staleness as mod

        monkeypatch.setattr(mod, "__file__", str(tmp_path / "nested" / "staleness.py"))
        assert _install_root() is None

    def test_returns_real_repo_root_for_this_install(self) -> None:
        root = _install_root()
        assert root is not None
        assert (root / "src" / "codewalk").is_dir()


class TestGithubStalenessBanner:
    def test_up_to_date_repo_yields_empty_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")
        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)

        monkeypatch.setattr("codewalk.staleness._install_root", lambda: clone)
        monkeypatch.setattr("codewalk.staleness._github_cache", None)

        assert github_staleness_banner() == ""

    def test_behind_repo_yields_nonempty_banner_with_pull_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")
        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)
        _commit(origin, "b.txt", "second")

        monkeypatch.setattr("codewalk.staleness._install_root", lambda: clone)
        monkeypatch.setattr("codewalk.staleness._github_cache", None)

        banner = github_staleness_banner()
        assert "1 commit behind" in banner
        assert "git pull" in banner

    def test_multiple_commits_behind_uses_plural(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")
        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)
        _commit(origin, "b.txt", "second")
        _commit(origin, "c.txt", "third")

        monkeypatch.setattr("codewalk.staleness._install_root", lambda: clone)
        monkeypatch.setattr("codewalk.staleness._github_cache", None)

        banner = github_staleness_banner()
        assert "2 commits behind" in banner

    def test_result_is_cached_within_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        _init_git_repo(origin)
        _commit(origin, "a.txt", "initial")
        clone = tmp_path / "clone"
        _run(["clone", "-q", str(origin), str(clone)], tmp_path)
        _commit(origin, "b.txt", "second")

        monkeypatch.setattr("codewalk.staleness._install_root", lambda: clone)
        monkeypatch.setattr("codewalk.staleness._github_cache", None)

        first = github_staleness_banner()
        # New commits landed on origin, but the cached result should still win.
        _commit(origin, "c.txt", "third")
        second = github_staleness_banner()
        assert first == second


class TestWrapToolFn:
    def test_wraps_only_once(self) -> None:
        def fn(x: int) -> str:
            return f"result-{x}"

        wrapped = _wrap_tool_fn(fn)
        twice_wrapped = _wrap_tool_fn(wrapped)
        assert wrapped is twice_wrapped

    def test_prepends_banner_when_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("codewalk.staleness.github_staleness_banner", lambda: "STALE BANNER")

        def fn() -> str:
            return "tool output"

        wrapped = _wrap_tool_fn(fn)
        result = wrapped()
        assert result.startswith("STALE BANNER")
        assert "tool output" in result

    def test_no_banner_when_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("codewalk.staleness.github_staleness_banner", lambda: "")

        def fn() -> str:
            return "tool output"

        wrapped = _wrap_tool_fn(fn)
        assert wrapped() == "tool output"

    def test_passes_through_non_string_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("codewalk.staleness.github_staleness_banner", lambda: "STALE BANNER")

        def fn() -> dict[str, int]:
            return {"a": 1}

        wrapped = _wrap_tool_fn(fn)
        assert wrapped() == {"a": 1}


def test_cached_github_check_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("codewalk.staleness._check_behind_github", boom)
    monkeypatch.setattr("codewalk.staleness._github_cache", None)
    assert _cached_github_check() is None


def test_install_github_staleness_wrappers_wraps_every_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codewalk.staleness import install_github_staleness_wrappers

    class _FakeTool:
        def __init__(self, name: str, fn: Callable[[], str]) -> None:
            self.name = name
            self.fn = fn

    def tool_a() -> str:
        return "a"

    def tool_b() -> str:
        return "b"

    tools = [_FakeTool("a", tool_a), _FakeTool("b", tool_b)]

    class _FakeToolManager:
        def list_tools(self) -> list[_FakeTool]:
            return tools

    monkeypatch.setattr("codewalk.staleness.github_staleness_banner", lambda: "")
    install_github_staleness_wrappers(_FakeToolManager())

    for tool in tools:
        assert getattr(tool.fn, "_codewalk_github_staleness_wrapped", False) is True
