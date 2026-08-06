"""`codewalk.yaml` schema and loading.

`codewalk.yaml` is entirely optional. A missing file simply means "use
defaults" -- this module never requires its presence. Malformed content (bad
YAML syntax, wrong types, unknown keys) degrades to sane defaults with a
logged warning rather than raising, so a broken config file can never block
`codewalk_analyze_codebase`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from codewalk.ingestion.tech_detect import detect_tech_stack
from codewalk.log import get_logger

logger = get_logger(__name__)

CONFIG_FILE_NAME = "codewalk.yaml"

_KNOWN_TOP_LEVEL_KEYS = {"indexing", "docs_path", "code_guidelines", "language_overrides", "tools"}
_KNOWN_INDEXING_KEYS = {"exclude", "include"}

_BASE_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".codewalk/**",
    ".git/**",
    "node_modules/**",
    "__pycache__/**",
    ".venv/**",
    "venv/**",
)

# Extra excludes layered on top of `_BASE_EXCLUDE_PATTERNS` when
# `ingestion.tech_detect.detect_tech_stack` recognizes a stack's manifest
# file. Best-effort only: an unrecognized or absent stack just falls back to
# the base excludes above, never blocking config generation.
TECH_STACK_EXCLUDES: dict[str, list[str]] = {
    "javascript/node": ["**/*.stories.tsx", "**/*.stories.ts", "coverage/**"],
    "typescript": ["**/*.d.ts"],
    "nx": ["dist/**", "tmp/**"],
    "dart/flutter": ["**/*.mocks.dart", "**/*.g.dart", "**/*.freezed.dart"],
    "python": ["**/migrations/**", "**/*.egg-info/**"],
    "rust": ["target/**"],
    "go": ["vendor/**"],
    "java/kotlin": ["**/build/**"],
    "kotlin": ["**/build/**"],
    "ruby": ["vendor/bundle/**"],
    "php": ["vendor/**"],
    "c/cpp": ["build/**", "cmake-build-*/**"],
}

_CONFIG_FOOTER = """
# include:
#   - src/**

# docs_path: docs
# code_guidelines: docs/code_guidelines.md

# language_overrides:
#   ".proto": protobuf

# tools:
#   static_analysis:
#     python: ["ruff", "check", "--output-format=json"]
#   test_command:
#     python: ["pytest"]
"""


def _render_config_text(extra_excludes: list[str]) -> str:
    """Build the `codewalk.yaml` text, merging the base excludes with any
    tech-stack-specific ones detected for this repo."""
    excludes = list(dict.fromkeys([*_BASE_EXCLUDE_PATTERNS, *extra_excludes]))
    exclude_block = "\n".join(f"  - {pattern}" for pattern in excludes)
    return (
        "# codewalk.yaml -- optional per-repo configuration.\n"
        "# Every key below is optional; omit anything you don't need to override.\n\n"
        "indexing:\n"
        "  exclude:\n"
        f"{exclude_block}\n"
        f"{_CONFIG_FOOTER}"
    )


DEFAULT_CONFIG_TEMPLATE = _render_config_text([])


class CodewalkConfig(BaseModel):
    """Validated, defaulted view of `codewalk.yaml`."""

    model_config = ConfigDict(frozen=True)

    exclude: list[str] = []
    include: list[str] = []
    docs_path: str = ""
    code_guidelines: str = ""
    language_overrides: dict[str, str] = {}
    tools: dict[str, dict[str, list[str]]] = {}


def _coerce_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    logger.warning("codewalk.yaml: '%s' must be a list of strings, ignoring invalid value", key)
    return []


def _coerce_str(value: Any, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    logger.warning("codewalk.yaml: '%s' must be a string, ignoring invalid value", key)
    return ""


def _coerce_str_dict(value: Any, key: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        return value
    logger.warning(
        "codewalk.yaml: '%s' must be a mapping of string to string, ignoring invalid value", key
    )
    return {}


def _parse_indexing_section(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the `indexing:` section, warning on any issue."""
    indexing = data.get("indexing")
    if indexing is None:
        return {}
    if not isinstance(indexing, dict):
        logger.warning("codewalk.yaml: 'indexing' must be a mapping, ignoring")
        return {}

    unknown_indexing = set(indexing) - _KNOWN_INDEXING_KEYS
    if unknown_indexing:
        keys = ", ".join(sorted(unknown_indexing))
        logger.warning("codewalk.yaml: ignoring unknown 'indexing' key(s): %s", keys)
    return indexing


def _coerce_tools(value: Any) -> dict[str, dict[str, list[str]]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.warning("codewalk.yaml: 'tools' must be a mapping, ignoring invalid value")
        return {}

    result: dict[str, dict[str, list[str]]] = {}
    for tool_name, per_language in value.items():
        if not isinstance(tool_name, str) or not isinstance(per_language, dict):
            logger.warning("codewalk.yaml: 'tools.%s' is malformed, skipping", tool_name)
            continue
        languages: dict[str, list[str]] = {}
        for lang, command in per_language.items():
            valid = (
                isinstance(lang, str)
                and isinstance(command, list)
                and all(isinstance(c, str) for c in command)
            )
            if valid:
                languages[lang] = command
            else:
                logger.warning(
                    "codewalk.yaml: 'tools.%s.%s' must be a list of strings, skipping",
                    tool_name,
                    lang,
                )
        result[tool_name] = languages
    return result


def load_codewalk_yaml(repo_root: Path | str) -> CodewalkConfig:
    """Load and validate `codewalk.yaml` from `repo_root`.

    Never raises for a missing, malformed, or partially-invalid file --
    always returns a usable (possibly all-default) `CodewalkConfig`, logging
    a warning for anything it had to ignore or fall back on.
    """
    path = Path(repo_root) / CONFIG_FILE_NAME
    if not path.exists():
        return CodewalkConfig()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("codewalk.yaml: could not read %s (%s), using defaults", path, exc)
        return CodewalkConfig()

    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning("codewalk.yaml: invalid YAML in %s (%s), using defaults", path, exc)
        return CodewalkConfig()

    if data is None:
        return CodewalkConfig()

    if not isinstance(data, dict):
        logger.warning(
            "codewalk.yaml: expected a mapping at the top level, got %s, using defaults",
            type(data).__name__,
        )
        return CodewalkConfig()

    unknown_top = set(data) - _KNOWN_TOP_LEVEL_KEYS
    if unknown_top:
        keys = ", ".join(sorted(unknown_top))
        logger.warning("codewalk.yaml: ignoring unknown top-level key(s): %s", keys)

    indexing = _parse_indexing_section(data)

    try:
        return CodewalkConfig(
            exclude=_coerce_str_list(indexing.get("exclude"), "indexing.exclude"),
            include=_coerce_str_list(indexing.get("include"), "indexing.include"),
            docs_path=_coerce_str(data.get("docs_path"), "docs_path"),
            code_guidelines=_coerce_str(data.get("code_guidelines"), "code_guidelines"),
            language_overrides=_coerce_str_dict(
                data.get("language_overrides"), "language_overrides"
            ),
            tools=_coerce_tools(data.get("tools")),
        )
    # Defensive net: the coercion helpers above already guarantee valid types,
    # so this should be unreachable in practice.
    except ValidationError as exc:  # pragma: no cover
        logger.warning("codewalk.yaml: %s, using defaults", exc)
        return CodewalkConfig()


def generate_default_config(repo_root: Path | str, *, force: bool = False) -> Path:
    """Write a `codewalk.yaml` at `repo_root` unless one already exists.

    Detects the repo's tech stack via `ingestion.tech_detect.detect_tech_stack`
    and layers stack-specific excludes (e.g. Dart generated files, Python
    migrations) on top of the core safety-net excludes. An unrecognized or
    undetectable stack just falls back to the base excludes.

    Args:
        repo_root: Directory to write `codewalk.yaml` into.
        force: Overwrite an existing `codewalk.yaml` if True.

    Returns:
        Path to the written (or pre-existing, if `force=False`) `codewalk.yaml`.
    """
    root = Path(repo_root)
    path = root / CONFIG_FILE_NAME
    if path.exists() and not force:
        return path

    extra_excludes: list[str] = []
    for tech in detect_tech_stack(root):
        extra_excludes.extend(TECH_STACK_EXCLUDES.get(tech, []))

    root.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_config_text(extra_excludes), encoding="utf-8")
    return path
