"""Core safety-net filtering: directories, extensions, filenames, and suffixes
that are never useful to scan (version control metadata, build output,
binaries, lock files, generated code).

Repo- or framework-specific exclusions belong in `codewalk.yaml`
(`indexing.exclude` / `indexing.include`), applied on top of this safety net
by `ingestion.scanner`. This module never raises -- filtering decisions
always degrade to "don't skip" on ambiguous input rather than erroring.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from codewalk.codewalk_config import CodewalkConfig

# Dot-directories to KEEP despite the leading dot (useful code/config).
KEEP_DOT_DIRS: frozenset[str] = frozenset({".github"})

# Core safety-net directories that are always pruned: version control
# metadata, dependency folders, build/cache output, generated artifacts.
CORE_SKIP_DIRS: frozenset[str] = frozenset(
    {
        # Version control / codewalk internal
        ".git",
        ".codewalk",
        # JS/TS dependencies / build
        "node_modules",
        "bower_components",
        # Python environments / caches
        "__pycache__",
        "venv",
        ".venv",
        "env",
        ".env",
        "egg-info",
        # Generic build / output
        "dist",
        "build",
        "target",
        "coverage",
        # iOS / macOS
        "Pods",
        "DerivedData",
        "Carthage",
        # Flutter / Dart
        "ephemeral",
        ".dart_tool",
        # Go / Ruby / PHP dependencies
        "vendor",
        "deps",
        # Swift
        "Packages",
        # Elixir
        "_build",
        # Gradle
        ".gradle",
        # Framework build caches
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".nx",
        ".terraform",
        # Python tooling caches
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "htmlcov",
        ".eggs",
        "__pypackages__",
        ".hypothesis",
        # JS build / deploy caches
        ".cache",
        ".parcel-cache",
        "storybook-static",
        ".vercel",
        ".netlify",
        # Output / generated directories
        "out",
        "obj",
        "gen",
        "generated",
        "__generated__",
        "intermediates",
        # Xcode / Android NDK
        "xcuserdata",
        ".cxx",
        # Test / CI artifacts
        ".nyc_output",
        "test-results",
        "test-reports",
        # Docs build output
        "site",
        # Generic temp / logs
        "tmp",
        "temp",
        "logs",
        "reports",
    }
)

# Core safety-net extensions that are never useful source code: binaries,
# media, archives, fonts, lock files, secrets, generated artifacts.
CORE_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Compiled / bytecode
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".wasm",
        ".dex",
        ".beam",
        ".hi",
        ".elc",
        ".rbc",
        ".fasl",
        # App bundles / archives
        ".apk",
        ".ipa",
        ".aab",
        ".jar",
        ".aar",
        ".ear",
        ".war",
        # Images
        ".ico",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".bmp",
        ".tiff",
        ".webp",
        ".heic",
        ".heif",
        ".psd",
        ".ai",
        ".sketch",
        ".fig",
        # 3D / game assets
        ".fbx",
        ".glb",
        ".gltf",
        ".blend",
        ".unity",
        ".prefab",
        # Fonts
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        # Audio / video
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".aac",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".webm",
        # Archives
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".rar",
        ".7z",
        ".tgz",
        # Lock files (auto-generated, not hand-written)
        ".lock",
        # Database / data files
        ".db",
        ".sqlite",
        ".sqlite3",
        ".h5",
        ".hdf5",
        ".pkl",
        ".pickle",
        # ML model files
        ".pt",
        ".pth",
        ".onnx",
        ".safetensors",
        ".bin",
        ".model",
        ".weights",
        # Documents (not code)
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".rst",
        ".txt",
        ".adoc",
        # Translation / localization data
        ".arb",
        ".xliff",
        ".xlf",
        ".po",
        ".pot",
        ".mo",
        ".strings",
        ".stringsdict",
        # Certificates / keys / secrets
        ".pem",
        ".crt",
        ".key",
        ".p12",
        ".pfx",
        ".secret",
        ".secrets",
        ".age",
        ".jks",
        ".keystore",
        ".cer",
        ".der",
        ".p8",
        ".mobileprovision",
        # Terraform (state + vars may contain secrets)
        ".tfstate",
        ".tfvars",
        # Maps / generated
        ".map",
        ".ipynb",
        # Xcode / iOS generated
        ".pbxproj",
        ".xcscheme",
        ".storyboard",
        ".xib",
        # GPU shaders
        ".glsl",
        ".hlsl",
        ".vert",
        ".frag",
        ".spv",
        ".metal",
        # Debug symbols
        ".pdb",
        ".res",
        # Patch / diff
        ".patch",
        ".diff",
        # Temp / scratch / logs
        ".tmp",
        ".temp",
        ".bak",
        ".orig",
        ".log",
        ".cache",
        # Editor swap files
        ".swp",
        ".swo",
        ".swn",
        ".iml",
        # Profiling / dumps / coverage
        ".prof",
        ".cpuprofile",
        ".dmp",
        ".hprof",
        ".profdata",
        ".profraw",
        ".lcov",
        ".gcda",
        ".gcno",
        # Compiler-generated artifacts
        ".d",
        ".pch",
        ".gch",
        ".rlib",
        ".jmod",
        # Unity-specific
        ".meta",
        ".anim",
        ".controller",
        ".lighting",
        ".shadergraph",
        # Visual Studio binary
        ".suo",
        ".sdf",
        ".ncb",
        ".user",
        # R / MATLAB data
        ".rda",
        ".rds",
        ".rdata",
        ".mat",
        # Ops / infra
        ".tfplan",
        ".retry",
        ".webmanifest",
        # Misc binary
        ".dat",
        ".data",
        ".npy",
        ".npz",
        ".parquet",
        ".feather",
        ".arrow",
        ".tfrecord",
        ".coverage",
    }
)

# Specific filenames to skip (generated / lock files / OS junk).
CORE_SKIP_FILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pubspec.lock",
        "Podfile.lock",
        "Gemfile.lock",
        "composer.lock",
        "Cargo.lock",
        "go.sum",
        "flake.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "mix.lock",
        "stack.yaml.lock",
        "deno.lock",
        "npm-shrinkwrap.json",
        ".terraform.lock.hcl",
        "Thumbs.db",
        "desktop.ini",
        "MANIFEST",
    }
)

# Filename suffixes for generated/auto-generated code.
CORE_SKIP_SUFFIXES: tuple[str, ...] = (
    ".g.dart",
    ".freezed.dart",
    ".gen.dart",
    ".generated.dart",
    ".g.cs",
    ".designer.cs",
    ".pb.go",
    "_pb2.py",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".chunk.js",
    ".pb.swift",
    ".pb.dart",
    "_pb2_grpc.py",
    ".grpc.swift",
    ".graphql.ts",
    ".gql.ts",
    "_generated.go",
    "_mock.go",
    ".mock.go",
    ".generated.ts",
    ".generated.js",
    ".chunk.css",
)


def should_skip_dir(dir_name: str) -> bool:
    """True if this directory should be pruned during a repo walk.

    This is the core safety net only -- framework- and repo-specific
    directory pruning is layered on top via `codewalk.yaml`.
    """
    if dir_name.startswith(".") and dir_name not in KEEP_DOT_DIRS:
        return True
    return dir_name in CORE_SKIP_DIRS


def should_skip_file(relative_path: str) -> bool:
    """True if this file should be skipped by the core safety net.

    `relative_path` uses `/` as the separator (repo-root-relative).
    """
    parts = relative_path.split("/")
    name = parts[-1]

    # Skip files under a hidden directory, except whitelisted ones.
    for part in parts[:-1]:
        if part.startswith(".") and part not in KEEP_DOT_DIRS:
            return True

    # Skip hidden files.
    if name.startswith("."):
        return True

    # Skip junk directories anywhere in the path.
    if any(part in CORE_SKIP_DIRS for part in parts):
        return True

    # Skip binary/media/lock extensions.
    dot = name.rfind(".")
    if dot != -1 and name[dot:] in CORE_SKIP_EXTENSIONS:
        return True

    # Skip specific filenames.
    if name in CORE_SKIP_FILES:
        return True

    # Skip generated file patterns (e.g. foo.g.dart, bar.pb.go).
    return any(name.endswith(suffix) for suffix in CORE_SKIP_SUFFIXES)


class GitignoreMatcher:
    """Matches repo-root-relative paths against the repo's top-level `.gitignore`
    and `.codewalkignore` (same syntax, merged into one pattern list).

    Known, deliberate limitations (documented, not silently "fixed" later):
      - Only the repo-root `.gitignore`/`.codewalkignore` are read;
        per-directory `.gitignore` files, `.git/info/exclude`, and global git
        excludes are not consulted.
      - Negation patterns (`!pattern`) are not supported; a matching negated
        pattern is ignored (treated as a comment), never as "de-ignore".
      - `**` is treated the same as `*` (single-segment glob), not a
        multi-level recursive match.
    """

    def __init__(self, repo_root: Path) -> None:
        self._patterns = self._load(repo_root)

    @staticmethod
    def _load(repo_root: Path) -> list[str]:
        patterns: list[str] = []
        for filename in (".gitignore", ".codewalkignore"):
            path = repo_root / filename
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            patterns.extend(
                line.strip()
                for line in text.splitlines()
                if line.strip()
                and not line.strip().startswith("#")
                and not line.strip().startswith("!")
            )
        return patterns

    def matches(self, relative_path: str) -> bool:
        """True if `relative_path` (repo-root-relative, `/`-separated) is ignored."""
        if not self._patterns:
            return False

        rel = relative_path.replace("\\", "/")
        segments = rel.split("/")
        name = segments[-1]

        for raw_pattern in self._patterns:
            anchored = raw_pattern.startswith("/")
            pattern = raw_pattern[1:] if anchored else raw_pattern
            dir_only = pattern.endswith("/")
            pattern = pattern[:-1] if dir_only else pattern
            if not pattern:
                continue

            candidates = [rel] if anchored else [rel, name]
            if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
                return True
            if not anchored and "/" not in pattern and pattern in segments:
                return True

        return False


def is_dir_excluded(
    dir_name: str,
    rel_dir: str,
    config: CodewalkConfig,
    gitignore: GitignoreMatcher,
) -> bool:
    """Decide whether to prune a directory during a config-aware repo walk.

    Order: explicit `include` patterns override everything (keep the dir);
    otherwise the core safety net, `.gitignore`, and `codewalk.yaml` excludes
    all apply.
    """
    full_dir = f"{rel_dir}/{dir_name}" if rel_dir != "." else dir_name

    if config.include and any(_include_keeps_dir(p, full_dir) for p in config.include):
        return False

    if should_skip_dir(dir_name):
        return True

    if gitignore.matches(full_dir + "/"):
        return True

    return any(_exclude_matches_dir(pattern, dir_name, full_dir) for pattern in config.exclude)


def is_file_excluded(
    filename: str,
    relative_path: str,
    config: CodewalkConfig,
    gitignore: GitignoreMatcher,
) -> bool:
    """Decide whether to skip a file during a config-aware repo walk.

    Order: explicit `include` patterns override everything (keep the file);
    otherwise the core safety net, `.gitignore`, and `codewalk.yaml` excludes
    all apply.
    """
    if config.include and any(
        _include_keeps_file(p, relative_path, filename) for p in config.include
    ):
        return False

    if should_skip_file(relative_path):
        return True

    if gitignore.matches(relative_path):
        return True

    return any(
        _exclude_matches_file(pattern, filename, relative_path) for pattern in config.exclude
    )


def _include_keeps_dir(pattern: str, dir_path: str) -> bool:
    """True if `dir_path` (or anything under/above it) is covered by an include pattern."""
    base = pattern.rstrip("/")
    if base.endswith("/**"):
        base = base[:-3]
    if not base:
        return True

    if "*" in base or "?" in base:
        if fnmatch.fnmatch(dir_path, base):
            return True
        concrete_prefix = base.split("*", 1)[0].rstrip("/")
        return bool(concrete_prefix) and (
            dir_path == concrete_prefix or dir_path.startswith(concrete_prefix + "/")
        )

    return dir_path == base or dir_path.startswith(base + "/") or base.startswith(dir_path + "/")


def _include_keeps_file(pattern: str, relative_path: str, filename: str) -> bool:
    """True if a file matches an include pattern."""
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(relative_path, pattern) or fnmatch.fnmatch(filename, pattern)
    return (
        relative_path == pattern or relative_path.startswith(pattern + "/") or filename == pattern
    )


def _exclude_matches_dir(pattern: str, dir_name: str, full_dir: str) -> bool:
    """True if a `codewalk.yaml` exclude pattern matches this directory."""
    if "*" not in pattern and "?" not in pattern and "/" not in pattern:
        return pattern == dir_name
    if pattern.endswith("/**"):
        dir_pattern = pattern[:-3]
        return full_dir == dir_pattern or fnmatch.fnmatch(full_dir, dir_pattern)
    if "/" in pattern and "*" not in pattern and "?" not in pattern:
        return full_dir == pattern or full_dir.startswith(pattern + "/")
    return fnmatch.fnmatch(full_dir, pattern.rstrip("/"))


def _exclude_matches_file(pattern: str, filename: str, relative_path: str) -> bool:
    """True if a `codewalk.yaml` exclude pattern matches this file."""
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(relative_path, pattern)
    if "/" in pattern:
        return relative_path == pattern or relative_path.startswith(pattern + "/")
    if pattern == filename:
        return True
    return pattern in relative_path.split("/")
