"""Tests for codewalk.ingestion.scanner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codewalk.codewalk_config import CodewalkConfig
from codewalk.ingestion.scanner import ScanResult, detect_language, scan_repo


def _relative_paths(result: ScanResult) -> set[str]:
    return {f.file_path for f in result.files}


class TestDetectLanguage:
    def test_known_extension(self) -> None:
        assert detect_language(Path("main.py")) == "python"
        assert detect_language(Path("app.tsx")) == "typescript"

    def test_unknown_extension(self) -> None:
        assert detect_language(Path("weird.xyz123")) == "unknown"

    def test_case_insensitive(self) -> None:
        assert detect_language(Path("MAIN.PY")) == "python"

    def test_override_wins_over_builtin_map(self) -> None:
        assert detect_language(Path("service.proto"), {".proto": "custom_idl"}) == "custom_idl"

    def test_override_adds_unknown_extension(self) -> None:
        assert detect_language(Path("schema.foo"), {".foo": "fooscript"}) == "fooscript"


class TestScanRepo:
    def test_empty_repo_returns_no_files_no_warnings(self, tmp_path: Path) -> None:
        result = scan_repo(tmp_path)
        assert result.files == []
        assert result.warnings == []
        assert result.truncated is False

    def test_nonexistent_repo_root_returns_warning_not_exception(self, tmp_path: Path) -> None:
        result = scan_repo(tmp_path / "missing")
        assert result.files == []
        assert len(result.warnings) == 1
        assert "does not exist" in result.warnings[0]

    def test_repo_with_only_binaries_returns_no_files(self, tmp_path: Path) -> None:
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "app.exe").write_bytes(b"MZ")

        result = scan_repo(tmp_path)

        assert result.files == []

    def test_ordinary_source_file_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

        result = scan_repo(tmp_path)

        assert _relative_paths(result) == {"main.py"}
        assert result.files[0].language == "python"

    def test_core_skip_dirs_are_pruned(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

        result = scan_repo(tmp_path)

        assert _relative_paths(result) == {"src/main.py"}

    def test_huge_single_file_is_excluded_with_warning(self, tmp_path: Path) -> None:
        big_file = tmp_path / "huge.py"
        big_file.write_bytes(b"x" * 1000)

        result = scan_repo(tmp_path, max_file_size_bytes=100)

        assert _relative_paths(result) == set()
        assert any("exceeds max_file_size_bytes" in w for w in result.warnings)

    def test_max_file_count_truncates(self, tmp_path: Path) -> None:
        for i in range(10):
            (tmp_path / f"file_{i}.py").write_text("x = 1\n", encoding="utf-8")
        # A second, later-walked directory ensures the outer loop gets another
        # iteration after truncation, exercising the early-exit check on it.
        (tmp_path / "zz_later_dir").mkdir()
        (tmp_path / "zz_later_dir" / "more.py").write_text("x = 1\n", encoding="utf-8")

        result = scan_repo(tmp_path, max_file_count=3)

        assert len(result.files) == 3
        assert result.truncated is True
        assert any("max_file_count cap" in w for w in result.warnings)

    def test_symlink_directory_cycle_does_not_hang(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
        # Create a symlink loop: src/loop -> tmp_path (the repo root itself).
        (tmp_path / "src" / "loop").symlink_to(tmp_path, target_is_directory=True)

        result = scan_repo(tmp_path)

        # followlinks=False means os.walk never descends into the symlinked
        # directory, so the walk terminates and finds exactly the real file.
        assert _relative_paths(result) == {"src/main.py"}

    def test_broken_file_symlink_is_skipped_with_warning(self, tmp_path: Path) -> None:
        target = tmp_path / "does_not_exist.py"
        link = tmp_path / "broken_link.py"
        link.symlink_to(target)

        result = scan_repo(tmp_path)

        assert _relative_paths(result) == set()
        assert any("could not stat" in w for w in result.warnings)

    def test_deeply_nested_paths_are_found(self, tmp_path: Path) -> None:
        current = tmp_path
        for i in range(40):
            current = current / f"level_{i}"
        current.mkdir(parents=True)
        (current / "deep.py").write_text("x = 1\n", encoding="utf-8")

        result = scan_repo(tmp_path)

        assert any(f.file_path.endswith("deep.py") for f in result.files)

    def test_gitignore_excludes_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.generated.py\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "model.generated.py").write_text("x = 2\n", encoding="utf-8")

        result = scan_repo(tmp_path)

        assert _relative_paths(result) == {"main.py"}

    def test_non_utf8_filename_does_not_crash_scan(self, tmp_path: Path) -> None:
        # A filename with an invalid UTF-8 byte sequence. POSIX allows any
        # byte except NUL and '/' in a filename, but not every filesystem
        # accepts one (e.g. macOS/APFS requires valid UTF-8) -- skip there.
        bad_name = os.fsdecode(b"weird_\xff_name.py")
        try:
            (tmp_path / bad_name).write_bytes(b"x = 1\n")
        except OSError:
            pytest.skip("filesystem rejects non-UTF-8 filenames on this platform")

        result = scan_repo(tmp_path)

        # Must not raise, and the file is still enumerated.
        assert len(result.files) == 1

    def test_config_exclude_is_applied(self, tmp_path: Path) -> None:
        (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "skip_me.py").write_text("x = 2\n", encoding="utf-8")
        config = CodewalkConfig(exclude=["skip_me.py"])

        result = scan_repo(tmp_path, config=config)

        assert _relative_paths(result) == {"keep.py"}

    def test_config_include_overrides_core_skip(self, tmp_path: Path) -> None:
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "important.py").write_text("x = 1\n", encoding="utf-8")
        config = CodewalkConfig(include=["vendor/**"])

        result = scan_repo(tmp_path, config=config)

        assert "vendor/important.py" in _relative_paths(result)

    def test_language_overrides_from_config_apply(self, tmp_path: Path) -> None:
        (tmp_path / "schema.proto").write_text("message Foo {}\n", encoding="utf-8")
        config = CodewalkConfig(language_overrides={".proto": "custom_idl"})

        result = scan_repo(tmp_path, config=config)

        assert result.files[0].language == "custom_idl"

    def test_hidden_directories_are_pruned_by_default(self, tmp_path: Path) -> None:
        (tmp_path / ".idea").mkdir()
        (tmp_path / ".idea" / "workspace.xml").write_text("<xml/>", encoding="utf-8")
        (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")

        result = scan_repo(tmp_path)

        assert _relative_paths(result) == {"src.py"}
