"""Repo-root directory walk and file enumeration for graph analysis.

Applies the core safety net (`ingestion.file_filter`), `.gitignore`, and
`codewalk.yaml` include/exclude rules while walking. Never raises for
recoverable per-file or per-repo problems -- always returns a (possibly
empty) `ScanResult` with a `warnings` list instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from codewalk.codewalk_config import CodewalkConfig
from codewalk.ingestion.file_filter import GitignoreMatcher, is_dir_excluded, is_file_excluded
from codewalk.log import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_FILE_SIZE_BYTES = 5_000_000
DEFAULT_MAX_FILE_COUNT = 50_000

EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".dart": "dart",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".txt": "text",
    ".kt": "kotlin",
    ".swift": "swift",
    ".m": "objc",
    ".mm": "objc",
    ".sql": "sql",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
}


def detect_language(file_path: Path, overrides: dict[str, str] | None = None) -> str:
    """Map a file's extension to a language name.

    `overrides` (from `CodewalkConfig.language_overrides`) is checked first,
    so a repo can teach codewalk about an extension the built-in map doesn't
    know (e.g. `.proto` -> a custom name).
    """
    suffix = file_path.suffix.lower()
    if overrides and suffix in overrides:
        return overrides[suffix]
    return EXTENSION_MAP.get(suffix, "unknown")


@dataclass(frozen=True)
class ScannedFile:
    """A single scanned file's metadata (no content -- that's read later, on demand)."""

    file_path: str  # repo-root-relative, "/"-separated
    absolute_path: Path
    language: str
    size_bytes: int


@dataclass
class ScanResult:
    """Result of a repo scan: the files found, plus any recoverable warnings."""

    files: list[ScannedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


def scan_repo(
    repo_root: Path | str,
    config: CodewalkConfig | None = None,
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
) -> ScanResult:
    """Walk `repo_root` and enumerate files, applying all filtering rules.

    Never raises: a missing/non-directory `repo_root`, unreadable files,
    oversized files, and hitting `max_file_count` all degrade to a warning
    appended to the result rather than an exception.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return ScanResult(
            warnings=[f"repo root does not exist or is not a directory: {root}"],
        )

    cfg = config or CodewalkConfig()
    gitignore = GitignoreMatcher(root)
    result = ScanResult()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if result.truncated:
            break

        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames if not is_dir_excluded(d, rel_dir, cfg, gitignore))

        for fname in sorted(filenames):
            relative = fname if rel_dir == "." else f"{rel_dir}/{fname}"

            if is_file_excluded(fname, relative, cfg, gitignore):
                continue

            full_path = Path(dirpath) / fname
            try:
                size = full_path.stat().st_size
            except OSError as exc:
                result.warnings.append(f"skipped {relative}: could not stat file ({exc})")
                continue

            if size > max_file_size_bytes:
                result.warnings.append(
                    f"skipped {relative}: size {size} bytes exceeds max_file_size_bytes "
                    f"({max_file_size_bytes})"
                )
                continue

            if len(result.files) >= max_file_count:
                result.truncated = True
                result.warnings.append(
                    f"reached max_file_count cap ({max_file_count}); "
                    "remaining files were not scanned"
                )
                break

            result.files.append(
                ScannedFile(
                    file_path=relative,
                    absolute_path=full_path,
                    language=detect_language(full_path, cfg.language_overrides),
                    size_bytes=size,
                )
            )

    logger.info(
        "scanned %s -> %d files (%d warnings)", root, len(result.files), len(result.warnings)
    )
    return result
