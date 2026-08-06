"""Tests for codewalk.ingestion.file_filter."""

from __future__ import annotations

from pathlib import Path

from codewalk.codewalk_config import CodewalkConfig
from codewalk.ingestion.file_filter import (
    GitignoreMatcher,
    is_dir_excluded,
    is_file_excluded,
    should_skip_dir,
    should_skip_file,
)


class TestShouldSkipDir:
    def test_core_skip_dirs_are_skipped(self) -> None:
        assert should_skip_dir("node_modules") is True
        assert should_skip_dir("__pycache__") is True
        assert should_skip_dir(".git") is True

    def test_hidden_dirs_are_skipped_by_default(self) -> None:
        assert should_skip_dir(".idea") is True

    def test_whitelisted_dot_dirs_are_kept(self) -> None:
        assert should_skip_dir(".github") is False

    def test_regular_source_dirs_are_kept(self) -> None:
        assert should_skip_dir("src") is False
        assert should_skip_dir("app") is False


class TestShouldSkipFile:
    def test_binary_extensions_are_skipped(self) -> None:
        assert should_skip_file("bin/tool.exe") is True
        assert should_skip_file("assets/logo.png") is True

    def test_lock_files_are_skipped(self) -> None:
        assert should_skip_file("package-lock.json") is True
        assert should_skip_file("poetry.lock") is True

    def test_generated_suffixes_are_skipped(self) -> None:
        assert should_skip_file("lib/model.g.dart") is True
        assert should_skip_file("proto/service.pb.go") is True

    def test_hidden_files_are_skipped(self) -> None:
        assert should_skip_file(".env") is True
        assert should_skip_file("src/.hidden.py") is True

    def test_files_under_hidden_dirs_are_skipped(self) -> None:
        assert should_skip_file(".vscode/settings.json") is True

    def test_files_under_whitelisted_dot_dirs_are_kept(self) -> None:
        assert should_skip_file(".github/workflows/ci.yml") is False

    def test_ordinary_source_file_is_kept(self) -> None:
        assert should_skip_file("src/main.py") is False

    def test_file_with_no_extension_is_kept(self) -> None:
        assert should_skip_file("Dockerfile") is False


class TestGitignoreMatcher:
    def test_no_gitignore_matches_nothing(self, tmp_path: Path) -> None:
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("anything.py") is False

    def test_plain_name_pattern_matches_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.json\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("secrets.json") is True
        assert matcher.matches("nested/dir/secrets.json") is True
        assert matcher.matches("other.json") is False

    def test_glob_pattern(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("debug.log") is True
        assert matcher.matches("nested/debug.log") is True
        assert matcher.matches("debug.txt") is False

    def test_directory_pattern(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("build/output.js") is True

    def test_anchored_pattern_only_matches_from_root(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("/only_root.txt\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("only_root.txt") is True
        assert matcher.matches("nested/only_root.txt") is False

    def test_comments_and_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("# comment\n\n*.tmp\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("scratch.tmp") is True

    def test_negation_is_not_supported_and_documented_as_ignored(self, tmp_path: Path) -> None:
        """Known limitation: `!pattern` is treated as a no-op, not "de-ignore"."""
        (tmp_path / ".gitignore").write_text("*.log\n!important.log\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        # important.log still matches *.log; negation does not re-include it.
        assert matcher.matches("important.log") is True

    def test_unreadable_gitignore_is_treated_as_absent(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.mkdir()  # a directory named .gitignore -> read_text() will fail
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("anything.py") is False

    def test_degenerate_slash_only_pattern_does_not_match_everything(self, tmp_path: Path) -> None:
        """A line that is just "/" strips down to an empty pattern and is skipped."""
        (tmp_path / ".gitignore").write_text("/\n*.log\n", encoding="utf-8")
        matcher = GitignoreMatcher(tmp_path)
        assert matcher.matches("main.py") is False
        assert matcher.matches("debug.log") is True


class TestIsDirExcluded:
    def test_core_safety_net_applies(self, tmp_path: Path) -> None:
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("node_modules", ".", config, gitignore) is True

    def test_config_exclude_plain_name(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["scripts"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("scripts", ".", config, gitignore) is True

    def test_config_exclude_glob_suffix(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["tests/**"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("tests", ".", config, gitignore) is True

    def test_include_overrides_everything(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["src"], include=["src/**"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("src", ".", config, gitignore) is False

    def test_gitignore_directory_pattern_applies(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("custom_build/\n", encoding="utf-8")
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        # "custom_build" is not in the core safety net, so this only passes
        # if the .gitignore directory-pattern branch is actually applied.
        assert is_dir_excluded("custom_build", ".", config, gitignore) is True

    def test_unrelated_dir_is_kept(self, tmp_path: Path) -> None:
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("src", ".", config, gitignore) is False

    def test_include_pattern_of_slash_star_star_keeps_everything(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["anything"], include=["/**"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("anything", ".", config, gitignore) is False

    def test_include_glob_pattern_matches_directory_directly(self, tmp_path: Path) -> None:
        config = CodewalkConfig(include=["feat*"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("feature", ".", config, gitignore) is False

    def test_include_glob_pattern_keeps_parent_dir_on_the_way_to_subtree(
        self, tmp_path: Path
    ) -> None:
        config = CodewalkConfig(include=["feat*/sub/**"])
        gitignore = GitignoreMatcher(tmp_path)
        # "feat" itself must be kept so the walk can reach feat*/sub/**.
        assert is_dir_excluded("feat", ".", config, gitignore) is False

    def test_include_glob_pattern_does_not_rescue_unrelated_excluded_dir(
        self, tmp_path: Path
    ) -> None:
        """`include` only overrides exclusion for dirs it actually matches."""
        config = CodewalkConfig(exclude=["other"], include=["feat*/sub/**"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("other", ".", config, gitignore) is True


class TestIsFileExcluded:
    def test_core_safety_net_applies(self, tmp_path: Path) -> None:
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("logo.png", "assets/logo.png", config, gitignore) is True

    def test_config_exclude_glob(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["*.generated.py"])
        gitignore = GitignoreMatcher(tmp_path)
        excluded = is_file_excluded(
            "models.generated.py", "src/models.generated.py", config, gitignore
        )
        assert excluded is True

    def test_config_exclude_path_prefix(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["src/mydir"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("foo.py", "src/mydir/foo.py", config, gitignore) is True

    def test_include_overrides_everything(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["*.png"], include=["assets/logo.png"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("logo.png", "assets/logo.png", config, gitignore) is False

    def test_include_glob_pattern_matches_file(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["*.png"], include=["*.png"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("logo.png", "assets/logo.png", config, gitignore) is False

    def test_gitignore_applies(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("api.secret", "config/api.secret", config, gitignore) is True

    def test_ordinary_file_is_kept(self, tmp_path: Path) -> None:
        config = CodewalkConfig()
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("main.py", "src/main.py", config, gitignore) is False

    def test_exclude_glob_pattern_matches_directory_by_name(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["feat*"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("feature", ".", config, gitignore) is True

    def test_exclude_path_prefix_matches_nested_directory(self, tmp_path: Path) -> None:
        config = CodewalkConfig(exclude=["src/customdir"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_dir_excluded("customdir", "src", config, gitignore) is True

    def test_exclude_plain_ancestor_name_matches_without_path_prefix(self, tmp_path: Path) -> None:
        """A plain exclude name (no slash) matches any ancestor directory segment."""
        config = CodewalkConfig(exclude=["customgen"])
        gitignore = GitignoreMatcher(tmp_path)
        assert is_file_excluded("foo.py", "src/customgen/foo.py", config, gitignore) is True
