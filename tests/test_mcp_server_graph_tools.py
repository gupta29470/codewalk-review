"""Tests for mcp.server: graph/query MCP tools (thin-wrapper behavior)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codewalk.mcp import server


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    )


def _init_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_analyze_codebase_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "def helper():\n    return 1\n"})
    result = server.codewalk_analyze_codebase(repo_path=str(repo))
    assert "Built graph" in result
    assert "Files: 1" in result


def test_analyze_codebase_refresh(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    server.codewalk_analyze_codebase(repo_path=str(repo))
    result = server.codewalk_analyze_codebase(repo_path=str(repo), refresh=True)
    assert "Refreshed graph" in result


def test_analyze_codebase_bad_repo_path_returns_error_not_crash(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = server.codewalk_analyze_codebase(repo_path=str(missing))
    assert result.startswith("\u274c")


def test_refresh_analysis_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    server.codewalk_analyze_codebase(repo_path=str(repo))
    result = server.codewalk_refresh_analysis(repo_path=str(repo))
    assert "Refreshed graph" in result


def test_generate_config_writes_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_generate_config(repo_path=str(repo))
    assert "codewalk.yaml ready" in result
    assert (repo / "codewalk.yaml").exists()


def test_generate_config_bad_repo_path_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = server.codewalk_generate_config(repo_path=str(missing))
    assert result.startswith("\u274c")


def test_get_overview_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(
        tmp_path,
        {
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "utils.py": "def helper():\n    return 1\n",
        },
    )
    result = server.codewalk_get_overview(repo_path=str(repo))
    assert "\u274c" not in result


def test_explain_function_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"utils.py": "def helper():\n    return 1\n"})
    result = server.codewalk_explain_function("helper", repo_path=str(repo))
    assert "def helper" in result


def test_explain_function_unknown_returns_error_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"utils.py": "def helper():\n    return 1\n"})
    result = server.codewalk_explain_function("does_not_exist", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_explain_class_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"models.py": "class Widget:\n    pass\n"})
    result = server.codewalk_explain_class("Widget", repo_path=str(repo))
    assert "Widget" in result


def test_explain_class_unknown_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"models.py": "class Widget:\n    pass\n"})
    result = server.codewalk_explain_class("Ghost", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_lookup_symbol_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"utils.py": "def helper():\n    return 1\n"})
    result = server.codewalk_lookup_symbol("helper", repo_path=str(repo))
    assert "helper" in result


def test_get_module_info_unknown_module_returns_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_module_info("nonexistent_module", repo_path=str(repo))
    assert result.startswith("\u274c")


def test_get_blast_radius_map_default(tmp_path: Path) -> None:
    repo = _init_repo(
        tmp_path,
        {
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "utils.py": "def helper():\n    return 1\n",
        },
    )
    result = server.codewalk_get_blast_radius_map(repo_path=str(repo))
    assert "\u274c" not in result


def test_get_blast_radius_map_with_target(tmp_path: Path) -> None:
    repo = _init_repo(
        tmp_path,
        {
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "utils.py": "def helper():\n    return 1\n",
        },
    )
    result = server.codewalk_get_blast_radius_map(target="utils.py", repo_path=str(repo))
    assert "main.py" in result


def test_find_circular_dependencies_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_find_circular_dependencies(repo_path=str(repo))
    assert "\u274c" not in result


def test_get_reading_order_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(
        tmp_path,
        {
            "main.py": "from utils import helper\n\n\ndef run():\n    return helper()\n",
            "utils.py": "def helper():\n    return 1\n",
        },
    )
    result = server.codewalk_get_reading_order(repo_path=str(repo))
    assert "\u274c" not in result


def test_get_execution_flow_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_execution_flow(repo_path=str(repo))
    assert "\u274c" not in result


def test_get_architecture_health_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_architecture_health(repo_path=str(repo))
    assert "\u274c" not in result


def test_call_chain_no_path_between_unrelated_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 1\n"})
    result = server.codewalk_call_chain("a.py", "b.py", repo_path=str(repo))
    assert "\u274c" not in result


def test_get_stack_info_returns_prompt_with_file_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_get_stack_info(repo_path=str(repo))
    assert "a.py" in result
    assert "codewalk_save_stack_context" in result


def test_save_and_reuse_stack_context(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, {"a.py": "x = 1\n"})
    result = server.codewalk_save_stack_context(
        {"languages": ["python"], "frameworks": []}, repo_path=str(repo)
    )
    assert "python" in result
    assert (repo / ".codewalk" / "stack_context.json").exists()


def test_format_build_warnings_empty() -> None:
    from codewalk.workspace import BuildWarnings, Workspace

    ws = Workspace.__new__(Workspace)
    ws.last_build_warnings = BuildWarnings()
    assert server._format_build_warnings(ws) == ""


def test_format_build_warnings_truncates_after_20() -> None:
    from codewalk.workspace import BuildWarnings, Workspace

    ws = Workspace.__new__(Workspace)
    ws.last_build_warnings = BuildWarnings(scan=[f"warning {i}" for i in range(25)])
    text = server._format_build_warnings(ws)
    assert "25 warning(s)" in text
    assert "and 5 more" in text
