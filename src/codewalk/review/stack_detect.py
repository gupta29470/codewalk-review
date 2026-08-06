"""Deterministic project-stack detection for rubric selection.

The MCP flow (wired up in a later phase) is:

1. Check ``.codewalk/stack_context.json`` -- persistent, survives across
   commits (architecture rarely changes).
2. If missing, this module's ``fallback_detect_stack`` gives a deterministic
   best-effort guess (language/framework signal only, from file extensions
   and manifest files) that the MCP tool wrapper can return to the host LLM
   for confirmation/enrichment, or save outright if it's confident enough.
3. The host LLM (or a human) can supply a richer JSON blob (architecture,
   state management, data layer, testing, api style) which is validated and
   persisted by ``save_stack_context``.

This module never calls an LLM itself -- codewalk has no internal LLM calls
anywhere.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from codewalk.atomic_io import write_json_atomic
from codewalk.log import get_logger
from codewalk.paths import stack_context_path
from codewalk.review.rubric_loader import LANGUAGE_BY_EXTENSION

logger = get_logger(__name__)

# Rubric names that exist on disk (language + framework). Used to validate
# both the deterministic fallback and any host-supplied stack context.
AVAILABLE_RUBRICS: frozenset[str] = frozenset(
    {
        # Languages
        "python",
        "typescript",
        "javascript",
        "go",
        "rust",
        "java",
        "kotlin",
        "swift",
        "dart",
        "ruby",
        "php",
        "c",
        "cpp",
        "csharp",
        "scala",
        "r",
        "objective_c",
        # Frameworks
        "python_fastapi",
        "python_django",
        "python_flask",
        "typescript_nextjs",
        "typescript_react",
        "dart_flutter",
        "java_android",
        "java_spring",
        "kotlin_android",
        "kotlin_spring",
        "swift_ios",
        "swift_swiftui",
        "ruby_rails",
        "php_laravel",
        "csharp_aspnet",
        "dotnet",
    }
)

_STACK_CONTEXT_KEYS = (
    "languages",
    "frameworks",
    "architecture",
    "state_management",
    "data_layer",
    "testing",
    "api_style",
)

STACK_DETECT_PROMPT = """You are a senior software architect. Analyze the repository file \
tree and changed files below.

Respond ONLY with a valid JSON object -- no explanation, no markdown fences, no preamble.

{{
  "languages": ["python", "typescript"],
  "frameworks": ["python_fastapi", "typescript_nextjs"],
  "architecture": "clean architecture with service-repository pattern",
  "state_management": "zustand for frontend, dependency injection for backend",
  "data_layer": "sqlalchemy with alembic migrations",
  "testing": "pytest with factory fixtures, jest + RTL for frontend",
  "api_style": "REST with pydantic request/response schemas"
}}

Field definitions:
- `languages`: primary languages used in this project (lowercase)
- `frameworks`: MUST match EXACTLY from this list: {available_rubrics}
- `architecture`: architecture pattern -- MVC, clean architecture, hexagonal, feature-based,
  layered, microservice, monolith with modules, etc.
- `state_management`: what manages application state, or "none / server-side only"
- `data_layer`: ORM / database access pattern + migrations
- `testing`: testing framework + approach
- `api_style`: REST, GraphQL, tRPC, gRPC, websockets, including schema approach

Rules:
- If a field cannot be determined, use empty string ""
- Only use framework names from the available list -- do not invent names
- Detect ALL languages and frameworks present, not just the dominant one

## Repository file tree
{file_tree}

## Changed files in this review
{changed_files}"""


def load_cached_stack_context(repo_root: Path) -> dict[str, Any] | None:
    """Load ``.codewalk/stack_context.json`` if present and well-formed."""
    path = stack_context_path(repo_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("stack_context.json at %s is corrupted", path)
        return None
    return data if isinstance(data, dict) else None


def save_stack_context(repo_root: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a stack context blob to ``.codewalk/stack_context.json``.

    Unknown keys are dropped. ``languages``/``frameworks`` entries not in
    ``AVAILABLE_RUBRICS`` are silently filtered out rather than rejecting the
    whole payload, since the caller may be a host LLM guessing at names.

    Returns the cleaned data that was actually written.
    """
    cleaned: dict[str, Any] = {}
    for key in _STACK_CONTEXT_KEYS:
        value = data.get(key)
        if key in ("languages", "frameworks"):
            items = value if isinstance(value, list) else []
            cleaned[key] = [item for item in items if item in AVAILABLE_RUBRICS]
        else:
            cleaned[key] = value if isinstance(value, str) else ""

    write_json_atomic(stack_context_path(repo_root), cleaned)
    return cleaned


def _detect_languages_by_extension(changed_files: list[str]) -> list[str]:
    lang_counts: Counter[str] = Counter()
    for file_path in changed_files:
        lang = LANGUAGE_BY_EXTENSION.get(Path(file_path).suffix.lower())
        if lang:
            lang_counts[lang] += 1
    return [lang for lang, _ in lang_counts.most_common(3)]


def _detect_js_framework(repo_root: Path) -> str | None:
    package_json = repo_root / "package.json"
    if not package_json.exists():
        return None
    try:
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "next" in deps:
        return "typescript_nextjs"
    if "react" in deps:
        return "typescript_react"
    return None


def _detect_python_framework(repo_root: Path) -> str | None:
    for req_file in ("requirements.txt", "pyproject.toml"):
        manifest = repo_root / req_file
        if not manifest.exists():
            continue
        try:
            content = manifest.read_text(encoding="utf-8").lower()
        except OSError:
            content = ""
        if "fastapi" in content:
            return "python_fastapi"
        if "django" in content:
            return "python_django"
        if "flask" in content:
            return "python_flask"
        break
    return None


def _detect_ruby_framework(repo_root: Path) -> str | None:
    gemfile = repo_root / "Gemfile"
    if not gemfile.exists():
        return None
    try:
        content = gemfile.read_text(encoding="utf-8").lower()
    except OSError:
        return None
    return "ruby_rails" if "rails" in content else None


def _detect_php_framework(repo_root: Path) -> str | None:
    composer_json = repo_root / "composer.json"
    if not composer_json.exists():
        return None
    try:
        composer = json.loads(composer_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(composer, dict):
        return None
    all_deps = {**composer.get("require", {}), **composer.get("require-dev", {})}
    return "php_laravel" if "laravel/framework" in all_deps else None


def _detect_dotnet_framework(repo_root: Path, changed_files: list[str]) -> str | None:
    has_dotnet_project = (
        any(repo_root.glob("*.csproj"))
        or any(repo_root.glob("*.sln"))
        or (repo_root / "Program.cs").exists()
    )
    if not has_dotnet_project:
        return None
    is_aspnet = any("asp" in fp.lower() or "controller" in fp.lower() for fp in changed_files)
    return "csharp_aspnet" if is_aspnet else "dotnet"


def _detect_jvm_framework(repo_root: Path, changed_files: list[str]) -> str | None:
    """Java/Kotlin Android vs. Spring, from build.gradle(.kts) content.

    Distinct from ``_detect_kotlin_frameworks`` below, which scans .kt file
    *content* directly and needs no gradle file at all. This one is the only
    signal available for plain Java (no .kt files), and also catches Kotlin
    projects whose changed files don't happen to touch a recognizable
    android/spring pattern themselves.
    """
    build_gradle = repo_root / "build.gradle"
    build_gradle_kts = repo_root / "build.gradle.kts"
    settings_gradle = repo_root / "settings.gradle"
    if not (build_gradle.exists() or build_gradle_kts.exists() or settings_gradle.exists()):
        return None

    gradle_content = ""
    for gradle_file in (build_gradle, build_gradle_kts):
        if gradle_file.exists():
            try:
                gradle_content = gradle_file.read_text(encoding="utf-8").lower()
            except OSError:
                gradle_content = ""
            break

    is_kotlin = any(fp.endswith((".kt", ".kts")) for fp in changed_files)
    if "com.android" in gradle_content or "android {" in gradle_content:
        return "kotlin_android" if is_kotlin else "java_android"
    if "spring" in gradle_content or "org.springframework" in gradle_content:
        return "kotlin_spring" if is_kotlin else "java_spring"
    return None


# iOS has no single manifest file that reliably distinguishes SwiftUI from
# UIKit -- a Podfile/Package.swift/*.xcodeproj can exist for either. Instead,
# scan the *content* of changed .swift files for framework-specific
# imports/patterns. Both may be detected for apps mixing UIKit and SwiftUI.
_SWIFTUI_PATTERNS: tuple[str, ...] = (
    "import SwiftUI",
    "@State ",
    "@Binding ",
    "@ObservedObject ",
    "@EnvironmentObject ",
    "@StateObject ",
    ": View {",
    ": View,",
)
_UIKIT_PATTERNS: tuple[str, ...] = (
    "import UIKit",
    "UIViewController",
    "UIView",
)


def _detect_swift_frameworks(repo_root: Path, changed_files: list[str]) -> list[str]:
    """Content-based detection for Swift UI frameworks (no manifest to check).

    Never raises: an unreadable file is skipped, not an abort.
    """
    found: set[str] = set()
    for file_path in changed_files:
        if not file_path.endswith(".swift"):
            continue
        try:
            content = (repo_root / file_path).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(pattern in content for pattern in _SWIFTUI_PATTERNS):
            found.add("swift_swiftui")
        if any(pattern in content for pattern in _UIKIT_PATTERNS):
            found.add("swift_ios")
    return sorted(found)


# Kotlin has no single manifest signal either: a build.gradle(.kts) can
# declare Android and/or Spring Boot dependencies in ways too varied to parse
# reliably. Scan .kt file content instead.
_KOTLIN_ANDROID_PATTERNS: tuple[str, ...] = (
    "import android.",
    "androidx.",
    "@Composable",
    "AppCompatActivity",
    ": Activity",
    ": Fragment",
    "ViewModel(",
)
_KOTLIN_SPRING_PATTERNS: tuple[str, ...] = (
    "org.springframework",
    "@RestController",
    "@SpringBootApplication",
    "@Service",
    "@Autowired",
    "@Repository",
)


def _detect_kotlin_frameworks(repo_root: Path, changed_files: list[str]) -> list[str]:
    """Content-based detection for Kotlin frameworks (Android vs. Spring)."""
    found: set[str] = set()
    for file_path in changed_files:
        if not file_path.endswith(".kt"):
            continue
        try:
            content = (repo_root / file_path).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(pattern in content for pattern in _KOTLIN_ANDROID_PATTERNS):
            found.add("kotlin_android")
        if any(pattern in content for pattern in _KOTLIN_SPRING_PATTERNS):
            found.add("kotlin_spring")
    return sorted(found)


# TypeScript/JavaScript: reinforce the package.json-based check with content
# patterns, so a monorepo diff whose own package.json doesn't list react/next
# as a direct dependency (or has no package.json in scope at all) still gets
# the right rubric. Next.js implies React but only the more specific rubric
# is reported, matching _detect_js_framework's existing behavior.
_NEXTJS_PATTERNS: tuple[str, ...] = (
    "from 'next/",
    'from "next/',
    "getServerSideProps",
    "getStaticProps",
    '"use client"',
    "'use client'",
    '"use server"',
    "'use server'",
)
_REACT_PATTERNS: tuple[str, ...] = (
    "from 'react'",
    'from "react"',
    "import React",
    "useState(",
    "useEffect(",
    "React.FC",
)
_TS_JS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx"})


def _detect_typescript_frameworks(repo_root: Path, changed_files: list[str]) -> list[str]:
    """Content-based detection for React vs. Next.js."""
    found: set[str] = set()
    for file_path in changed_files:
        if Path(file_path).suffix.lower() not in _TS_JS_EXTENSIONS:
            continue
        try:
            content = (repo_root / file_path).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(pattern in content for pattern in _NEXTJS_PATTERNS):
            found.add("typescript_nextjs")
        elif any(pattern in content for pattern in _REACT_PATTERNS):
            found.add("typescript_react")
    return sorted(found)


# Python: reinforce the requirements.txt/pyproject.toml check with content
# patterns, for the same monorepo/missing-manifest reasons as TypeScript.
_FASTAPI_PATTERNS: tuple[str, ...] = ("from fastapi", "import fastapi", "FastAPI(")
_DJANGO_PATTERNS: tuple[str, ...] = (
    "from django",
    "import django",
    "models.Model",
    "django.db",
    "django.urls",
)
_FLASK_PATTERNS: tuple[str, ...] = ("from flask", "import flask", "Flask(__name__)")


def _detect_python_frameworks_by_content(repo_root: Path, changed_files: list[str]) -> list[str]:
    """Content-based detection for Python web frameworks."""
    found: set[str] = set()
    for file_path in changed_files:
        if not file_path.endswith(".py"):
            continue
        try:
            content = (repo_root / file_path).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(pattern in content for pattern in _FASTAPI_PATTERNS):
            found.add("python_fastapi")
        if any(pattern in content for pattern in _DJANGO_PATTERNS):
            found.add("python_django")
        if any(pattern in content for pattern in _FLASK_PATTERNS):
            found.add("python_flask")
    return sorted(found)


def fallback_detect_stack(repo_root: Path, changed_files: list[str]) -> dict[str, Any]:
    """Deterministic best-effort stack detection (no LLM involved).

    Uses file-extension counts for languages and a handful of manifest-file
    checks for frameworks. Leaves the free-text fields (architecture,
    state_management, data_layer, testing, api_style) empty -- only a host
    LLM (or a human) can fill those in via ``save_stack_context``.
    """
    frameworks = [
        name
        for name in (
            _detect_js_framework(repo_root),
            "dart_flutter" if (repo_root / "pubspec.yaml").exists() else None,
            _detect_python_framework(repo_root),
            _detect_ruby_framework(repo_root),
            _detect_php_framework(repo_root),
            _detect_dotnet_framework(repo_root, changed_files),
            _detect_jvm_framework(repo_root, changed_files),
        )
        if name is not None
    ]
    frameworks.extend(_detect_swift_frameworks(repo_root, changed_files))
    frameworks.extend(_detect_kotlin_frameworks(repo_root, changed_files))
    frameworks.extend(_detect_typescript_frameworks(repo_root, changed_files))
    frameworks.extend(_detect_python_frameworks_by_content(repo_root, changed_files))
    frameworks = list(dict.fromkeys(frameworks))  # dedupe, preserve order

    return {
        "languages": _detect_languages_by_extension(changed_files),
        "frameworks": frameworks,
        "architecture": "",
        "state_management": "",
        "data_layer": "",
        "testing": "",
        "api_style": "",
    }


def get_rubric_names_from_stack(stack: dict[str, Any]) -> list[str]:
    """Extract deduplicated rubric names (languages + frameworks) from a stack dict."""
    names: list[str] = []
    for lang in stack.get("languages", []):
        if lang in AVAILABLE_RUBRICS:
            names.append(lang)
    for fw in stack.get("frameworks", []):
        if fw in AVAILABLE_RUBRICS:
            names.append(fw)
    return list(dict.fromkeys(names))


def format_stack_context_header(stack: dict[str, Any]) -> str:
    """Format a stack dict as a Markdown header injected into review prompts."""
    lines = ["## Repository Architecture Context"]
    if stack.get("languages"):
        lines.append(f"- **Languages:** {', '.join(stack['languages'])}")
    if stack.get("frameworks"):
        lines.append(f"- **Frameworks:** {', '.join(stack['frameworks'])}")
    if stack.get("architecture"):
        lines.append(f"- **Architecture:** {stack['architecture']}")
    if stack.get("state_management"):
        lines.append(f"- **State management:** {stack['state_management']}")
    if stack.get("data_layer"):
        lines.append(f"- **Data layer:** {stack['data_layer']}")
    if stack.get("testing"):
        lines.append(f"- **Testing:** {stack['testing']}")
    if stack.get("api_style"):
        lines.append(f"- **API style:** {stack['api_style']}")

    if len(lines) <= 1:
        return ""
    lines.append("")
    return "\n".join(lines)
