"""Subprocess-based static analysis and test running for the review verification step.

Called after the host applies an accepted review fix, to confirm nothing
broke. Language detection is extension-based; commands are configurable per
language via `codewalk.yaml` (`tools.static_analysis.<language>` /
`tools.test_command.<language>`), with sane built-in defaults otherwise.
Tools that aren't installed are skipped gracefully, never crash the caller.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from codewalk.codewalk_config import CodewalkConfig

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}

_DEFAULT_STATIC_ANALYSIS_COMMANDS: dict[str, list[str]] = {
    "python": ["ruff", "check", "--output-format=concise"],
    "go": ["go", "vet"],
    "rust": ["cargo", "check"],
}
_DEFAULT_TEST_COMMANDS: dict[str, list[str]] = {
    "python": ["pytest", "-q"],
    "go": ["go", "test", "./..."],
    "rust": ["cargo", "test"],
}
_SUBPROCESS_TIMEOUT_SECONDS = 120
_OUTPUT_TAIL_CHARS = 4000


@dataclass
class CommandResult:
    """Outcome of running one subprocess-based command."""

    ok: bool
    command: str
    stdout: str
    stderr: str
    skipped_reason: str | None = None


def language_for_path(file_path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(Path(file_path).suffix.lower())


def languages_in(file_paths: list[str]) -> list[str]:
    """Distinct languages present in `file_paths`, in first-seen order."""
    languages: list[str] = []
    for file_path in file_paths:
        lang = language_for_path(file_path)
        if lang and lang not in languages:
            languages.append(lang)
    return languages


def _run_command(repo_root: Path, command: list[str], extra_args: list[str]) -> CommandResult:
    full_command = [*command, *extra_args]
    joined = " ".join(full_command)
    try:
        result = subprocess.run(  # noqa: S603
            full_command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return CommandResult(
            ok=True,
            command=joined,
            stdout="",
            stderr="",
            skipped_reason=f"'{command[0]}' is not installed",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            ok=False,
            command=joined,
            stdout="",
            stderr="",
            skipped_reason=f"timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
        )
    return CommandResult(
        ok=result.returncode == 0,
        command=joined,
        stdout=result.stdout[-_OUTPUT_TAIL_CHARS:],
        stderr=result.stderr[-_OUTPUT_TAIL_CHARS:],
    )


def run_static_analysis(
    repo_root: Path, file_paths: list[str], config: CodewalkConfig | None = None
) -> list[CommandResult]:
    """Run each detected language's static-analysis command over `file_paths`.

    Falls back to Python if no language could be detected from `file_paths`
    (e.g. an empty list). Languages with neither a configured nor a built-in
    command are silently skipped (not an error -- just nothing to run).
    """
    configured = (config.tools.get("static_analysis") if config else None) or {}
    languages = languages_in(file_paths) or ["python"]

    results: list[CommandResult] = []
    for lang in languages:
        command = configured.get(lang) or _DEFAULT_STATIC_ANALYSIS_COMMANDS.get(lang)
        if not command:
            continue
        relevant = [fp for fp in file_paths if language_for_path(fp) == lang] or file_paths
        results.append(_run_command(repo_root, command, relevant))
    return results


def run_tests(
    repo_root: Path, file_paths: list[str], config: CodewalkConfig | None = None
) -> CommandResult | None:
    """Run the configured test command for the dominant language among `file_paths`.

    Returns None (not an error) if no command is configured or built-in for
    the detected/default language.
    """
    configured = (config.tools.get("test_command") if config else None) or {}
    languages = languages_in(file_paths)
    lang = languages[0] if languages else "python"

    command = configured.get(lang) or _DEFAULT_TEST_COMMANDS.get(lang)
    if not command:
        return None
    return _run_command(repo_root, command, [])
