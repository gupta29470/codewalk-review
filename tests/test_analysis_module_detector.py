"""Tests for codewalk.analysis.module_detector.

Scenarios mirror /memories/repo/module-detector.md exactly.
"""

from __future__ import annotations

from pathlib import Path

from codewalk.analysis.module_detector import detect_modules
from tests.conftest import write_repo_files


class TestFlatRepo:
    def test_all_files_at_root_become_root_module(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path, {"main.py": "x = 1\n", "utils.py": "y = 2\n", "config.py": "z = 3\n"}
        )
        result = detect_modules(files)
        assert result.source_root == ""
        assert set(result.modules) == {"root"}
        assert result.modules["root"].file_count == 3


class TestMonorepoWithSrcWrapper:
    def test_src_wrapper_stripped_and_top_level_dirs_become_modules(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/auth/logout.py": "x = 1\n",
                "src/billing/invoice.py": "x = 1\n",
                "src/billing/receipt.py": "x = 1\n",
                "src/reports/export.py": "x = 1\n",
            },
        )
        result = detect_modules(files)
        assert result.source_root == "src"
        assert set(result.modules) == {"auth", "billing", "reports"}
        assert result.modules["auth"].file_count == 2


class TestFlutterStyleNestedFeatures:
    def test_repeating_child_dirs_across_features_pushes_depth_to_two(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "lib/features/auth/bloc/auth_bloc.dart": "x",
                "lib/features/auth/ui/login_page.dart": "x",
                "lib/features/auth/models/user.dart": "x",
                "lib/features/home/bloc/home_bloc.dart": "x",
                "lib/features/home/ui/home_page.dart": "x",
                "lib/features/home/models/feed.dart": "x",
                "lib/features/settings/bloc/settings_bloc.dart": "x",
                "lib/features/settings/ui/settings_page.dart": "x",
                "lib/features/settings/models/prefs.dart": "x",
            },
        )
        result = detect_modules(files)
        # "lib" is a wrapper dir, stripped; "features" is the only remaining
        # top-level dir (single-child collapse), also stripped.
        assert result.source_root == "lib/features"
        assert set(result.modules) == {"auth", "home", "settings"}
        assert result.modules["auth"].file_count == 3


class TestTooManyModulesFallback:
    def test_falls_back_to_depth_one_when_over_twenty_modules(self, tmp_path: Path) -> None:
        files_dict = {}
        # 25 distinct top-level-ish groups at a depth that would otherwise be
        # chosen as depth 2, forcing the >20-modules safety net to kick in.
        for i in range(25):
            files_dict[f"src/group_{i}/sub/a.py"] = "x"
            files_dict[f"src/group_{i}/sub/b.py"] = "x"
        files = write_repo_files(tmp_path, files_dict)

        result = detect_modules(files)

        assert result.stats.total_modules <= 25
        # Fallback to depth 1 means each "group_N" is its own module (not
        # "group_N/sub").
        assert all("/" not in name for name in result.modules)


class TestModuleGraph:
    def test_module_graph_derived_from_file_level_dependencies(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/billing/invoice.py": "x = 1\n",
            },
        )
        file_graph = {"src/auth/login.py": ["src/billing/invoice.py"], "src/billing/invoice.py": []}

        result = detect_modules(files, dep_graph=file_graph)

        assert result.module_graph["auth"] == ["billing"]
        assert result.module_graph["billing"] == []

    def test_no_dep_graph_yields_empty_module_edges(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path, {"src/auth/login.py": "x = 1\n", "src/billing/invoice.py": "x = 1\n"}
        )
        result = detect_modules(files, dep_graph=None)
        assert result.module_graph["auth"] == []

    def test_self_module_dependency_is_excluded(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/auth/session.py": "x = 1\n",
                "src/billing/invoice.py": "x = 1\n",
            },
        )
        file_graph = {
            "src/auth/login.py": ["src/auth/session.py"],
            "src/auth/session.py": [],
            "src/billing/invoice.py": [],
        }
        result = detect_modules(files, dep_graph=file_graph)
        assert result.module_graph["auth"] == []  # same-module edges don't count


class TestEdgeCases:
    def test_empty_file_list(self) -> None:
        result = detect_modules([])
        assert result.modules == {}
        assert result.source_root == ""
        assert result.stats.total_files == 0

    def test_single_file_repo(self, tmp_path: Path) -> None:
        files = write_repo_files(tmp_path, {"main.py": "x = 1\n"})
        result = detect_modules(files)
        assert set(result.modules) == {"root"}

    def test_languages_are_counted_per_module(self, tmp_path: Path) -> None:
        files = write_repo_files(
            tmp_path,
            {
                "src/auth/login.py": "x = 1\n",
                "src/auth/session.py": "x = 1\n",
                "src/auth/README.md": "# auth\n",
                "src/billing/invoice.py": "x = 1\n",
            },
        )
        result = detect_modules(files)
        assert result.modules["auth"].languages == {"python": 2, "markdown": 1}
