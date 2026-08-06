"""Cheap, deterministic technology-stack detection from manifest files.

This is a heuristic signal only -- used as a fallback by `review.stack_detect`
when a richer (host-LLM-driven) detection hasn't run yet. It never raises:
an unreadable or missing repo simply yields an empty result.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_FILE_MAP: dict[str, str] = {
    "package.json": "javascript/node",
    "tsconfig.json": "typescript",
    "nx.json": "nx",
    "pubspec.yaml": "dart/flutter",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
    "Pipfile": "python",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java/kotlin",
    "build.gradle.kts": "kotlin",
    "Gemfile": "ruby",
    "composer.json": "php",
    "CMakeLists.txt": "c/cpp",
    "Makefile": "c/cpp",
}


def detect_tech_stack(repo_root: Path | str) -> list[str]:
    """Detect the technology stack of a repo from top-level manifest files.

    Returns a sorted, deduplicated list of detected technologies (e.g.
    `["python", "typescript"]`). If both `requirements.txt` and
    `pyproject.toml` exist, `"python"` still appears only once. An empty
    list is returned (not an error) if `repo_root` doesn't exist or no
    manifest is recognized.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return []

    detected: set[str] = set()
    for filename, tech in CONFIG_FILE_MAP.items():
        try:
            if (root / filename).exists():
                detected.add(tech)
        except OSError:  # pragma: no cover -- Path.exists() already swallows OSError
            continue

    return sorted(detected)
