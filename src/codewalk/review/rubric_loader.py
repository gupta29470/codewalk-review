"""Language/framework rubric loading for the review engine.

Rubrics are Markdown documents injected into the review context so the host
LLM has language- and framework-specific guidance. A team can override any
built-in rubric by placing a same-named file under ``.codewalk/rubrics/`` in
the repo being reviewed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codewalk.log import get_logger
from codewalk.paths import rubrics_override_dir

logger = get_logger(__name__)

# Map file extensions to base language rubrics.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".dart": "dart",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".scala": "scala",
    ".r": "r",
    ".m": "objective_c",
    ".mm": "objective_c",
}


def _builtin_rubrics_dir() -> Path:
    return Path(__file__).parent / "rubrics"


def _load_rubric(name: str, repo_root: Path | None = None) -> str | None:
    """Load a rubric file by name. Team override (`.codewalk/rubrics/`) wins."""
    if repo_root is not None:
        team_path = rubrics_override_dir(repo_root) / f"{name}.md"
        if team_path.exists():
            return team_path.read_text(encoding="utf-8")

    builtin_path = _builtin_rubrics_dir() / f"{name}.md"
    if builtin_path.exists():
        return builtin_path.read_text(encoding="utf-8")
    return None


def language_for_file(file_path: str) -> str | None:
    """Return the base language rubric name for a file path, if recognized."""
    return LANGUAGE_BY_EXTENSION.get(Path(file_path).suffix.lower())


@dataclass
class Rubrics:
    """Resolved rubric set for one review."""

    core: str = ""
    fallback: str = ""
    language: dict[str, str] = field(default_factory=dict)
    framework: str = ""

    def for_language(self, language: str | None) -> str:
        if not language:
            return ""
        return self.language.get(language, "")


def _detect_languages(file_paths: list[str], detected_rubric_names: list[str] | None) -> set[str]:
    languages: set[str] = set()
    for file_path in file_paths:
        lang = language_for_file(file_path)
        if lang:
            languages.add(lang)
    if detected_rubric_names:
        languages.update(name for name in detected_rubric_names if "_" not in name)
    return languages


def _load_language_rubrics(languages: set[str], repo_root: Path) -> dict[str, str]:
    language_rubrics: dict[str, str] = {}
    for lang in sorted(languages):
        rubric = _load_rubric(lang, repo_root)
        if rubric:
            language_rubrics[lang] = rubric
    return language_rubrics


def _resolve_framework_text(
    repo_root: Path, file_paths: list[str], detected_rubric_names: list[str] | None
) -> str:
    framework_parts: list[str] = []
    if detected_rubric_names:
        for name in detected_rubric_names:
            if "_" not in name:
                continue
            rubric = _load_rubric(name, repo_root)
            if rubric:
                framework_parts.append(rubric)

    if not framework_parts:
        detected_framework = _resolve_framework_rubric(repo_root, file_paths)
        if detected_framework:
            framework_parts.append(detected_framework)

    return "\n\n".join(framework_parts)


def build_rubrics(
    repo_root: Path,
    file_paths: Iterable[str],
    detected_rubric_names: list[str] | None = None,
) -> Rubrics:
    """Resolve core, fallback, per-language, and framework rubrics for a review.

    ``fallback`` (a generic, framework-agnostic rubric) only loads when
    neither a language-specific rubric nor a framework rubric was resolved --
    otherwise it substantially duplicates ``core``'s principles (naming,
    layer placement, error handling, DRY) while adding UI/lifecycle-specific
    guidance that doesn't apply to framework-less code.

    Args:
        repo_root: Repository root (for team override lookup).
        file_paths: Changed file paths, used for extension-based language
            detection.
        detected_rubric_names: Rubric names from stack detection (see
            ``stack_detect.py``). When given, these augment extension-based
            language detection and supply framework rubrics.
    """
    paths = list(file_paths)
    languages = _detect_languages(paths, detected_rubric_names)
    language_rubrics = _load_language_rubrics(languages, repo_root)
    framework_text = _resolve_framework_text(repo_root, paths, detected_rubric_names)

    fallback_text = ""
    if not language_rubrics and not framework_text:
        fallback_text = _load_rubric("fallback", repo_root) or ""

    return Rubrics(
        core=_load_rubric("core", repo_root) or "",
        fallback=fallback_text,
        language=language_rubrics,
        framework=framework_text,
    )


def _read_manifest_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("could not parse manifest %s", path)
        return None
    return data if isinstance(data, dict) else None


def _read_manifest_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").lower()
    except OSError:
        logger.warning("could not read manifest %s", path)
        return None


def _detect_js_framework(repo_root: Path) -> str | None:
    package_json = repo_root / "package.json"
    if not package_json.exists():
        return None
    pkg = _read_manifest_json(package_json)
    if not pkg:
        return None
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "next" in deps:
        return "typescript_nextjs"
    if "react" in deps or "react-dom" in deps:
        return "typescript_react"
    return None


def _detect_python_framework(repo_root: Path, paths_lower: list[str]) -> str | None:
    if any("manage.py" in p for p in paths_lower) or (repo_root / "manage.py").exists():
        return "python_django"

    for manifest_name in ("requirements.txt", "pyproject.toml"):
        manifest = repo_root / manifest_name
        if not manifest.exists():
            continue
        content = _read_manifest_text(manifest) or ""
        if "fastapi" in content:
            return "python_fastapi"
        if "flask" in content:
            return "python_flask"
        if "django" in content:
            return "python_django"
        break
    return None


def _detect_jvm_framework(repo_root: Path, file_paths: list[str]) -> str | None:
    build_gradle = repo_root / "build.gradle"
    build_gradle_kts = repo_root / "build.gradle.kts"
    settings_gradle = repo_root / "settings.gradle"
    if not (build_gradle.exists() or build_gradle_kts.exists() or settings_gradle.exists()):
        return None

    gradle_content = ""
    for gradle_file in (build_gradle, build_gradle_kts):
        if gradle_file.exists():
            gradle_content = _read_manifest_text(gradle_file) or ""
            break

    is_kotlin = any(fp.endswith((".kt", ".kts")) for fp in file_paths)
    if "com.android" in gradle_content or "android {" in gradle_content:
        return "kotlin_android" if is_kotlin else "java_android"
    if "spring" in gradle_content or "org.springframework" in gradle_content:
        return "kotlin_spring" if is_kotlin else "java_spring"
    return None


def _detect_swift_framework(repo_root: Path, file_paths: list[str]) -> str | None:
    has_xcode_project = (
        (repo_root / "Package.swift").exists()
        or any(repo_root.glob("*.xcodeproj"))
        or any(repo_root.glob("*.xcworkspace"))
    )
    if has_xcode_project:
        is_swiftui = any("swiftui" in fp.lower() or "ContentView" in fp for fp in file_paths)
        return "swift_swiftui" if is_swiftui else "swift_ios"
    if (repo_root / "Podfile").exists():
        return "swift_ios"
    return None


def _detect_ruby_framework(repo_root: Path) -> str | None:
    gemfile = repo_root / "Gemfile"
    if not gemfile.exists():
        return None
    content = _read_manifest_text(gemfile)
    return "ruby_rails" if content and "rails" in content else None


def _detect_php_framework(repo_root: Path) -> str | None:
    composer_json = repo_root / "composer.json"
    if not composer_json.exists():
        return None
    composer = _read_manifest_json(composer_json)
    if not composer:
        return None
    all_deps = {**composer.get("require", {}), **composer.get("require-dev", {})}
    return "php_laravel" if "laravel/framework" in all_deps else None


def _detect_dotnet_framework(repo_root: Path, file_paths: list[str]) -> str | None:
    has_dotnet_project = (
        any(repo_root.glob("*.csproj"))
        or any(repo_root.glob("*.sln"))
        or (repo_root / "Program.cs").exists()
    )
    if not has_dotnet_project:
        return None
    is_aspnet = any("asp" in fp.lower() or "controller" in fp.lower() for fp in file_paths)
    return "csharp_aspnet" if is_aspnet else "dotnet"


def _resolve_framework_rubric(repo_root: Path, file_paths: list[str]) -> str | None:
    """Detect frameworks from manifest/config files and combine their rubrics."""
    paths_lower = [fp.lower() for fp in file_paths]

    detected: list[str] = [
        name
        for name in (
            _detect_js_framework(repo_root),
            _detect_python_framework(repo_root, paths_lower),
            "dart_flutter" if (repo_root / "pubspec.yaml").exists() else None,
            _detect_jvm_framework(repo_root, file_paths),
            _detect_swift_framework(repo_root, file_paths),
            _detect_ruby_framework(repo_root),
            _detect_php_framework(repo_root),
            _detect_dotnet_framework(repo_root, file_paths),
        )
        if name is not None
    ]

    if not detected:
        return None

    rubric_parts = [rubric for fw in detected if (rubric := _load_rubric(fw, repo_root))]
    return "\n\n".join(rubric_parts) if rubric_parts else None
