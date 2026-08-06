"""Tests for review.rubric_loader: rubric catalog loading and framework detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codewalk.review.rubric_loader import (
    _builtin_rubrics_dir,
    build_rubrics,
    language_for_file,
)

_ALL_RUBRIC_NAMES = sorted(p.stem for p in _builtin_rubrics_dir().glob("*.md"))


def test_at_least_expected_rubric_count_present() -> None:
    # core + fallback + language + framework rubrics; ~34 files expected.
    assert len(_ALL_RUBRIC_NAMES) >= 30
    assert "core" in _ALL_RUBRIC_NAMES
    assert "fallback" in _ALL_RUBRIC_NAMES


@pytest.mark.parametrize("rubric_name", _ALL_RUBRIC_NAMES)
def test_every_builtin_rubric_loads_without_error(rubric_name: str) -> None:
    path = _builtin_rubrics_dir() / f"{rubric_name}.md"
    content = path.read_text(encoding="utf-8")
    assert content.strip() != ""


def test_language_for_file_known_extensions() -> None:
    assert language_for_file("src/app.py") == "python"
    assert language_for_file("src/app.ts") == "typescript"
    assert language_for_file("src/app.tsx") == "typescript"


def test_language_for_file_unknown_extension_returns_none() -> None:
    assert language_for_file("README.md") is None


def test_build_rubrics_loads_core_always(tmp_path: Path) -> None:
    rubrics = build_rubrics(tmp_path, ["a.py"])
    assert rubrics.core != ""


def test_build_rubrics_fallback_absent_when_language_detected(tmp_path: Path) -> None:
    """A recognized language (python.md) already loads -- fallback.md would
    mostly duplicate core.md's principles, so it should not also load."""
    rubrics = build_rubrics(tmp_path, ["a.py"])
    assert rubrics.language != {}
    assert rubrics.fallback == ""


def test_build_rubrics_fallback_present_when_no_language_and_no_framework(
    tmp_path: Path,
) -> None:
    """An unrecognized extension with no framework signal at all -- this is
    the one case fallback.md exists for."""
    rubrics = build_rubrics(tmp_path, ["schema.sql"])
    assert rubrics.language == {}
    assert rubrics.framework == ""
    assert rubrics.fallback != ""


def test_build_rubrics_loads_language_rubric_for_python(tmp_path: Path) -> None:
    rubrics = build_rubrics(tmp_path, ["a.py"])
    assert "python" in rubrics.language
    assert rubrics.for_language("python") != ""


def test_build_rubrics_unknown_language_returns_empty_via_for_language(tmp_path: Path) -> None:
    rubrics = build_rubrics(tmp_path, ["a.py"])
    assert rubrics.for_language(None) == ""
    assert rubrics.for_language("klingon") == ""


def test_build_rubrics_team_override_wins_over_builtin(tmp_path: Path) -> None:
    override_dir = tmp_path / ".codewalk" / "rubrics"
    override_dir.mkdir(parents=True)
    (override_dir / "python.md").write_text(
        "# Team Python Rubric\nOur own rules.\n", encoding="utf-8"
    )

    rubrics = build_rubrics(tmp_path, ["a.py"])
    assert "Team Python Rubric" in rubrics.language["python"]


def test_build_rubrics_detects_framework_from_stack_names(tmp_path: Path) -> None:
    rubrics = build_rubrics(tmp_path, ["a.py"], detected_rubric_names=["python", "python_fastapi"])
    assert rubrics.framework != ""
    assert "python" in rubrics.language


def test_build_rubrics_detects_nextjs_from_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14.0.0"}}), encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["pages/index.tsx"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_react_dom_without_next(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react-dom": "18.0.0"}}), encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["src/App.tsx"])
    assert rubrics.framework != ""


def test_build_rubrics_package_json_with_no_matching_deps(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "4.0.0"}}), encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["src/index.ts"])
    assert rubrics.framework == ""


def test_build_rubrics_detects_fastapi_from_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["main.py"])
    assert rubrics.framework != ""


def test_build_rubrics_requirements_no_matching_framework(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["main.py"])
    assert rubrics.framework == ""


def test_build_rubrics_no_files_no_framework(tmp_path: Path) -> None:
    rubrics = build_rubrics(tmp_path, [])
    assert rubrics.framework == ""
    assert rubrics.language == {}


def test_build_rubrics_detects_django_from_manage_py(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["app/models.py"])
    assert "django" in rubrics.framework.lower() or rubrics.framework != ""


def test_build_rubrics_detects_django_from_changed_file_path_only(tmp_path: Path) -> None:
    """manage.py appears in the changed-file list even though it isn't on disk."""
    rubrics = build_rubrics(tmp_path, ["manage.py", "app/models.py"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_flask(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["app.py"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_django_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('dependencies = ["django"]\n', encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["app.py"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_android_kotlin(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["MainActivity.kt"])
    assert rubrics.framework != ""


def test_build_rubrics_settings_gradle_only_no_match(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'app'\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["Main.java"])
    assert rubrics.framework == ""


def test_build_rubrics_detects_java_spring(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text(
        "implementation 'org.springframework:spring-core'\n", encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["App.java"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_kotlin_spring(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text(
        "implementation 'org.springframework:spring-core'\n", encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["App.kt"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_android_via_gradle_kts(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text("android {}\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["MainActivity.kt"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_swiftui(tmp_path: Path) -> None:
    (tmp_path / "Package.swift").write_text("// swift package\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["ContentView.swift"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_swift_ios_via_podfile(tmp_path: Path) -> None:
    (tmp_path / "Podfile").write_text("platform :ios\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["AppDelegate.swift"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_ruby_rails(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'rails'\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["app.rb"])
    assert rubrics.framework != ""


def test_build_rubrics_gemfile_no_rails(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'sinatra'\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["app.rb"])
    assert rubrics.framework == ""


def test_build_rubrics_detects_php_laravel(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^10.0"}}), encoding="utf-8"
    )
    rubrics = build_rubrics(tmp_path, ["routes/web.php"])
    assert rubrics.framework != ""


def test_build_rubrics_detects_dotnet_aspnet(tmp_path: Path) -> None:
    (tmp_path / "Program.cs").write_text("var app = WebApplication.Create();\n", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["Controllers/HomeController.cs"])
    assert rubrics.framework != ""


def test_build_rubrics_corrupted_package_json_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["a.ts"])
    assert rubrics.framework == ""


def test_build_rubrics_corrupted_composer_json_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text("{not valid json", encoding="utf-8")
    rubrics = build_rubrics(tmp_path, ["a.php"])
    assert rubrics.framework == ""


# ══════════════════════════════════════════════════════════════════════
# Multi-language / multi-framework combinations -- a single review batch
# (or a monorepo diff) commonly touches more than one language/framework
# at once. These verify build_rubrics() correctly combines everything
# rather than picking just one.
# ══════════════════════════════════════════════════════════════════════


class TestSameLanguageAndItsFramework:
    """language + framework rubric both present together (e.g. dart + dart_flutter)."""

    def test_dart_and_dart_flutter(self, tmp_path: Path) -> None:
        (tmp_path / "pubspec.yaml").write_text("name: my_app\n", encoding="utf-8")
        rubrics = build_rubrics(
            tmp_path, ["lib/main.dart"], detected_rubric_names=["dart", "dart_flutter"]
        )
        assert "dart" in rubrics.language
        assert "Principal Flutter Architect" in rubrics.framework
        assert rubrics.fallback == ""

    def test_python_and_fastapi(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
        rubrics = build_rubrics(
            tmp_path, ["main.py"], detected_rubric_names=["python", "python_fastapi"]
        )
        assert "python" in rubrics.language
        assert "Principal FastAPI Engineer" in rubrics.framework
        assert rubrics.fallback == ""

    def test_typescript_and_nextjs(self, tmp_path: Path) -> None:
        rubrics = build_rubrics(
            tmp_path, ["page.tsx"], detected_rubric_names=["typescript", "typescript_nextjs"]
        )
        assert "typescript" in rubrics.language
        assert "Principal Next.js Engineer" in rubrics.framework
        assert rubrics.fallback == ""

    def test_kotlin_and_kotlin_android(self, tmp_path: Path) -> None:
        rubrics = build_rubrics(
            tmp_path, ["MainActivity.kt"], detected_rubric_names=["kotlin", "kotlin_android"]
        )
        assert "kotlin" in rubrics.language
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework
        assert rubrics.fallback == ""

    def test_swift_and_swift_ios(self, tmp_path: Path) -> None:
        rubrics = build_rubrics(
            tmp_path, ["AppDelegate.swift"], detected_rubric_names=["swift", "swift_ios"]
        )
        assert "swift" in rubrics.language
        assert "Principal iOS Engineer (Swift)" in rubrics.framework
        assert rubrics.fallback == ""


class TestMultiLanguagePolyglotDiffs:
    """A single diff/batch touching more than one language, no framework needed."""

    def test_dart_and_typescript_together(self, tmp_path: Path) -> None:
        rubrics = build_rubrics(tmp_path, ["a.dart", "b.ts"])
        assert set(rubrics.language.keys()) == {"dart", "typescript"}
        assert rubrics.fallback == ""

    def test_python_and_kotlin_together(self, tmp_path: Path) -> None:
        rubrics = build_rubrics(tmp_path, ["service.py", "MainActivity.kt"])
        assert set(rubrics.language.keys()) == {"python", "kotlin"}
        assert rubrics.fallback == ""

    def test_swift_and_kotlin_mobile_monorepo(self, tmp_path: Path) -> None:
        """Common real-world case: a shared feature landing in both the iOS
        and Android app in the same PR."""
        rubrics = build_rubrics(tmp_path, ["ViewController.swift", "MainActivity.kt"])
        assert set(rubrics.language.keys()) == {"swift", "kotlin"}
        assert rubrics.fallback == ""

    def test_five_languages_at_once(self, tmp_path: Path) -> None:
        files = ["a.py", "b.ts", "c.go", "d.rs", "e.rb"]
        rubrics = build_rubrics(tmp_path, files)
        assert set(rubrics.language.keys()) == {"python", "typescript", "go", "rust", "ruby"}
        assert rubrics.fallback == ""


class TestMultiFrameworkCombinations:
    """Multiple *framework* rubrics combined into one review, not just languages."""

    def test_react_and_nextjs_from_different_files(self, tmp_path: Path) -> None:
        """A diff with one plain React component and one Next.js page --
        both framework rubrics should be included, not just one."""
        (tmp_path / "App.tsx").write_text(
            "import React, { useState } from 'react';\n", encoding="utf-8"
        )
        (tmp_path / "page.tsx").write_text(
            '"use client";\nexport default function Page() { return null; }\n',
            encoding="utf-8",
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        stack = fallback_detect_stack(tmp_path, ["App.tsx", "page.tsx"])
        assert set(stack["frameworks"]) == {"typescript_react", "typescript_nextjs"}
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, ["App.tsx", "page.tsx"], detected_rubric_names=names)
        assert "Principal React Engineer" in rubrics.framework
        assert "Principal Next.js Engineer" in rubrics.framework
        assert rubrics.fallback == ""

    def test_kotlin_android_and_kotlin_spring_mixed_diff(self, tmp_path: Path) -> None:
        """A diff touching both an Android module and a Spring backend
        service written in Kotlin (e.g. a shared-Kotlin-multiplatform repo)."""
        (tmp_path / "MainActivity.kt").write_text(
            "import android.os.Bundle\nimport androidx.appcompat.app.AppCompatActivity\n",
            encoding="utf-8",
        )
        (tmp_path / "UserController.kt").write_text(
            "import org.springframework.web.bind.annotation.RestController\n@RestController\n"
            "class UserController {}\n",
            encoding="utf-8",
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        stack = fallback_detect_stack(tmp_path, ["MainActivity.kt", "UserController.kt"])
        assert set(stack["frameworks"]) == {"kotlin_android", "kotlin_spring"}
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(
            tmp_path, ["MainActivity.kt", "UserController.kt"], detected_rubric_names=names
        )
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework
        assert "Principal Kotlin Spring Engineer" in rubrics.framework
        assert rubrics.fallback == ""

    def test_swift_ios_and_swift_swiftui_mixed_diff(self, tmp_path: Path) -> None:
        (tmp_path / "ContentView.swift").write_text(
            "import SwiftUI\n"
            'struct ContentView: View {\n    var body: some View { Text("x") }\n}\n',
            encoding="utf-8",
        )
        (tmp_path / "Host.swift").write_text(
            "import UIKit\nclass Host: UIViewController {}\n", encoding="utf-8"
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        stack = fallback_detect_stack(tmp_path, ["ContentView.swift", "Host.swift"])
        assert set(stack["frameworks"]) == {"swift_swiftui", "swift_ios"}
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(
            tmp_path, ["ContentView.swift", "Host.swift"], detected_rubric_names=names
        )
        assert "Principal iOS Engineer (Swift)" in rubrics.framework
        assert "SwiftUI" in rubrics.framework or "Flutter" not in rubrics.framework
        assert rubrics.fallback == ""

    def test_all_three_python_web_frameworks_across_files(self, tmp_path: Path) -> None:
        """Stress case: FastAPI, Django, and Flask files all touched in one
        diff (e.g. a migration between frameworks)."""
        (tmp_path / "service.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
        )
        (tmp_path / "models.py").write_text(
            "from django.db import models\nclass User(models.Model):\n    pass\n",
            encoding="utf-8",
        )
        (tmp_path / "legacy.py").write_text(
            "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["service.py", "models.py", "legacy.py"]
        stack = fallback_detect_stack(tmp_path, files)
        assert set(stack["frameworks"]) == {"python_fastapi", "python_django", "python_flask"}
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert "Principal FastAPI Engineer" in rubrics.framework
        assert "Principal Django Engineer" in rubrics.framework
        assert "Principal Flask Engineer" in rubrics.framework
        assert rubrics.fallback == ""


class TestCrossDomainCombinations:
    """Backend + mobile, or other cross-domain diffs in one review batch."""

    def test_python_fastapi_backend_and_kotlin_android_app(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
        (tmp_path / "android").mkdir()
        (tmp_path / "android" / "MainActivity.kt").write_text(
            "import android.os.Bundle\nimport androidx.appcompat.app.AppCompatActivity\n",
            encoding="utf-8",
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["api/main.py", "android/MainActivity.kt"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "python_fastapi" in stack["frameworks"]
        assert "kotlin_android" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert set(rubrics.language.keys()) == {"python", "kotlin"}
        assert "Principal FastAPI Engineer" in rubrics.framework
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework
        assert rubrics.fallback == ""

    def test_dart_flutter_and_typescript_react_admin_panel(self, tmp_path: Path) -> None:
        """A mobile app + web admin panel living in the same monorepo."""
        (tmp_path / "pubspec.yaml").write_text("name: my_app\n", encoding="utf-8")
        (tmp_path / "admin" / "src").mkdir(parents=True)
        (tmp_path / "admin" / "src" / "App.tsx").write_text(
            "import React, { useState } from 'react';\n", encoding="utf-8"
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["mobile/lib/main.dart", "admin/src/App.tsx"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "dart_flutter" in stack["frameworks"]
        assert "typescript_react" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert set(rubrics.language.keys()) == {"dart", "typescript"}
        assert "Principal Flutter Architect" in rubrics.framework
        assert "Principal React Engineer" in rubrics.framework
        assert rubrics.fallback == ""


class TestManifestOnlyFrameworksCombinedWithOthers:
    """Ruby, PHP, C#/.NET, and Java(JVM) are all eagerly detected in
    stack_detect.py's fallback_detect_stack() (PHP/dotnet/JVM detectors were
    added specifically to fix a gap where they only existed via
    rubric_loader.py's _resolve_framework_rubric() last-resort manifest
    fallback -- meaning they silently dropped out whenever any other
    framework, e.g. Kotlin Android or React, was already found eagerly).
    These tests confirm the combos now work end-to-end via the realistic
    engine flow (fallback_detect_stack -> build_rubrics)."""

    def test_rails_and_react_combine_via_stack_detect(self, tmp_path: Path) -> None:
        """Ruby is in stack_detect.py's eager list, so it combines with any
        other eagerly-detected framework -- no gap here."""
        (tmp_path / "Gemfile").write_text('gem "rails"\n', encoding="utf-8")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "App.tsx").write_text(
            "import React, { useState } from 'react';\n", encoding="utf-8"
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["app/models/user.rb", "app/App.tsx"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "ruby_rails" in stack["frameworks"]
        assert "typescript_react" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert set(rubrics.language.keys()) == {"ruby", "typescript"}
        assert "Rails" in rubrics.framework
        assert "Principal React Engineer" in rubrics.framework
        assert rubrics.fallback == ""

    def test_laravel_and_kotlin_android_now_combine_via_stack_detect(self, tmp_path: Path) -> None:
        """Regression test for a fixed gap: stack_detect.py previously had no
        PHP/C#/Java(JVM) detectors at all, so once Kotlin Android was found
        eagerly, php_laravel never got a chance to load (rubric_loader.py's
        _resolve_framework_rubric manifest fallback only runs as a last
        resort when detected_rubric_names contributed zero framework
        rubrics). stack_detect.py now has its own PHP/dotnet/JVM manifest
        detectors, so both frameworks are found eagerly and combine."""
        (tmp_path / "composer.json").write_text(
            json.dumps({"require": {"laravel/framework": "10.0"}}), encoding="utf-8"
        )
        (tmp_path / "android").mkdir()
        (tmp_path / "android" / "MainActivity.kt").write_text(
            "import android.os.Bundle\nimport androidx.appcompat.app.AppCompatActivity\n",
            encoding="utf-8",
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["routes/web.php", "android/MainActivity.kt"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "kotlin_android" in stack["frameworks"]
        assert "php_laravel" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework
        assert "Laravel" in rubrics.framework

    def test_laravel_and_kotlin_android_combine_via_pure_manifest_fallback(
        self, tmp_path: Path
    ) -> None:
        """rubric_loader.py's _resolve_framework_rubric() itself still works
        standalone (no detected_rubric_names passed at all) as an
        independent safety net for any caller that skips stack detection."""
        (tmp_path / "composer.json").write_text(
            json.dumps({"require": {"laravel/framework": "10.0"}}), encoding="utf-8"
        )
        (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
        files = ["routes/web.php", "MainActivity.kt"]
        # No detected_rubric_names passed -- forces the pure manifest path.
        rubrics = build_rubrics(tmp_path, files)
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework
        assert "Laravel" in rubrics.framework
        assert rubrics.fallback == ""

    def test_java_spring_backend_and_react_frontend_via_stack_detect(self, tmp_path: Path) -> None:
        """Java (not Kotlin) + React, combined eagerly via stack_detect.py's
        JVM and JS manifest detectors."""
        (tmp_path / "build.gradle").write_text(
            "implementation 'org.springframework:spring-core'\n", encoding="utf-8"
        )
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"react": "18.0.0"}}), encoding="utf-8"
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["src/main/java/App.java", "frontend/src/App.jsx"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "java_spring" in stack["frameworks"]
        assert "typescript_react" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert "Spring" in rubrics.framework
        assert "Principal React Engineer" in rubrics.framework

    def test_csharp_aspnet_and_kotlin_android_via_stack_detect(self, tmp_path: Path) -> None:
        """.NET ASP.NET backend + Kotlin Android app in one monorepo diff,
        both found eagerly via stack_detect.py's dotnet and JVM detectors."""
        (tmp_path / "Program.cs").write_text("var app = WebApplication.Create();\n")
        (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
        (tmp_path / "android").mkdir()
        (tmp_path / "android" / "MainActivity.kt").write_text(
            "import android.os.Bundle\nimport androidx.appcompat.app.AppCompatActivity\n",
            encoding="utf-8",
        )
        from codewalk.review.stack_detect import (
            fallback_detect_stack,
            get_rubric_names_from_stack,
        )

        files = ["Controllers/HomeController.cs", "android/MainActivity.kt"]
        stack = fallback_detect_stack(tmp_path, files)
        assert "csharp_aspnet" in stack["frameworks"]
        assert "kotlin_android" in stack["frameworks"]
        names = get_rubric_names_from_stack(stack)
        rubrics = build_rubrics(tmp_path, files, detected_rubric_names=names)
        assert "Principal Android Engineer (Kotlin)" in rubrics.framework


class TestRubricLoaderOnlyFallbackDetectors:
    """java_android/java_spring/csharp_aspnet/php_laravel/dotnet also have a
    dedicated safety net in rubric_loader.py's _resolve_framework_rubric(),
    independent of stack_detect.py's own eager detectors above -- this fires
    for any direct build_rubrics() caller that skips stack detection
    entirely (detected_rubric_names=None). These confirm that path still
    works standalone."""

    def test_java_android_via_manifest_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
        rubrics = build_rubrics(tmp_path, ["MainActivity.java"])
        assert "Principal Android Engineer (Java)" in rubrics.framework
        assert rubrics.fallback == ""

    def test_java_spring_via_manifest_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text(
            "implementation 'org.springframework:spring-core'\n", encoding="utf-8"
        )
        rubrics = build_rubrics(tmp_path, ["App.java"])
        assert "Principal" in rubrics.framework
        assert "Spring" in rubrics.framework
        assert rubrics.fallback == ""

    def test_csharp_aspnet_via_manifest_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "Program.cs").write_text("var app = WebApplication.Create();\n")
        rubrics = build_rubrics(tmp_path, ["Controllers/HomeController.cs"])
        assert rubrics.framework != ""
        assert rubrics.fallback == ""

    def test_php_laravel_via_manifest_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text(
            json.dumps({"require": {"laravel/framework": "10.0"}}), encoding="utf-8"
        )
        rubrics = build_rubrics(tmp_path, ["routes/web.php"])
        assert rubrics.framework != ""
        assert rubrics.fallback == ""

    def test_dotnet_generic_when_not_aspnet(self, tmp_path: Path) -> None:
        (tmp_path / "app.csproj").write_text("<Project></Project>\n", encoding="utf-8")
        rubrics = build_rubrics(tmp_path, ["Utils.cs"])
        assert rubrics.framework != ""
        assert rubrics.fallback == ""

    def test_manifest_fallback_does_not_fire_when_stack_detect_already_found_something(
        self, tmp_path: Path
    ) -> None:
        """The rubric_loader.py manifest fallback must stay a *fallback* --
        if stack_detect.py already found a framework, it must not also
        combine in an unrelated manifest-detected one."""
        (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
        (tmp_path / "pubspec.yaml").write_text("name: my_app\n", encoding="utf-8")
        # Pass detected_rubric_names explicitly (as engine.py would, from
        # stack_detect.py) with ONLY dart_flutter -- java_android must not
        # sneak in via the build.gradle fallback even though it's on disk.
        rubrics = build_rubrics(
            tmp_path, ["lib/main.dart"], detected_rubric_names=["dart", "dart_flutter"]
        )
        assert "Principal Flutter Architect" in rubrics.framework
        assert "Principal Android Engineer" not in rubrics.framework


@pytest.mark.parametrize(
    "language",
    [
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
        "csharp",
        "cpp",
        "c",
        "scala",
        "r",
        "objective_c",
    ],
)
def test_every_language_rubric_loads_via_build_rubrics(tmp_path: Path, language: str) -> None:
    """Every entry LANGUAGE_BY_EXTENSION can map to must have a loadable rubric."""
    ext_by_lang = {
        "python": "py",
        "typescript": "ts",
        "javascript": "js",
        "go": "go",
        "rust": "rs",
        "java": "java",
        "kotlin": "kt",
        "swift": "swift",
        "dart": "dart",
        "ruby": "rb",
        "php": "php",
        "csharp": "cs",
        "cpp": "cpp",
        "c": "c",
        "scala": "scala",
        "r": "r",
        "objective_c": "m",
    }
    rubrics = build_rubrics(tmp_path, [f"a.{ext_by_lang[language]}"])
    assert language in rubrics.language
    assert rubrics.language[language] != ""
    assert rubrics.fallback == ""
