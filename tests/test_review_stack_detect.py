"""Tests for review.stack_detect: deterministic fallback detection + persistence."""

from __future__ import annotations

import json
from pathlib import Path

from codewalk.review.stack_detect import (
    AVAILABLE_RUBRICS,
    fallback_detect_stack,
    format_stack_context_header,
    get_rubric_names_from_stack,
    load_cached_stack_context,
    save_stack_context,
)


def test_load_cached_stack_context_missing_returns_none(tmp_path: Path) -> None:
    assert load_cached_stack_context(tmp_path) is None


def test_load_cached_stack_context_corrupted_returns_none(tmp_path: Path) -> None:
    path = tmp_path / ".codewalk" / "stack_context.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_cached_stack_context(tmp_path) is None


def test_save_and_load_stack_context_round_trip(tmp_path: Path) -> None:
    data = {
        "languages": ["python"],
        "frameworks": ["python_fastapi"],
        "architecture": "clean architecture",
        "state_management": "",
        "data_layer": "sqlalchemy",
        "testing": "pytest",
        "api_style": "REST",
    }
    save_stack_context(tmp_path, data)

    loaded = load_cached_stack_context(tmp_path)
    assert loaded is not None
    assert loaded["languages"] == ["python"]
    assert loaded["architecture"] == "clean architecture"


def test_save_stack_context_filters_unknown_framework_names(tmp_path: Path) -> None:
    data = {
        "languages": ["python", "made_up_language"],
        "frameworks": ["python_fastapi", "made_up_framework"],
    }
    cleaned = save_stack_context(tmp_path, data)
    assert cleaned["languages"] == ["python"]
    assert cleaned["frameworks"] == ["python_fastapi"]


def test_save_stack_context_drops_unknown_keys(tmp_path: Path) -> None:
    data = {"languages": ["python"], "some_internal_key": "secret"}
    cleaned = save_stack_context(tmp_path, data)
    assert "some_internal_key" not in cleaned


def test_save_stack_context_writes_atomically(tmp_path: Path) -> None:
    save_stack_context(tmp_path, {"languages": ["python"]})
    path = tmp_path / ".codewalk" / "stack_context.json"
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_fallback_detect_stack_languages_from_extensions(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["a.py", "b.py", "c.ts"])
    assert "python" in result["languages"]
    assert result["architecture"] == ""


def test_fallback_detect_stack_no_changed_files(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, [])
    assert result["languages"] == []
    assert result["frameworks"] == []


def test_fallback_detect_stack_detects_nextjs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14.0.0", "react": "18.0.0"}}), encoding="utf-8"
    )
    result = fallback_detect_stack(tmp_path, ["pages/index.tsx"])
    assert "typescript_nextjs" in result["frameworks"]


def test_fallback_detect_stack_detects_react_without_next(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "18.0.0"}}), encoding="utf-8"
    )
    result = fallback_detect_stack(tmp_path, ["src/App.tsx"])
    assert "typescript_react" in result["frameworks"]


def test_fallback_detect_stack_corrupted_package_json_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, [])
    assert "typescript_nextjs" not in result["frameworks"]


def test_fallback_detect_stack_detects_fastapi(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["main.py"])
    assert "python_fastapi" in result["frameworks"]


def test_fallback_detect_stack_detects_flutter(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: my_app\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["lib/main.dart"])
    assert "dart_flutter" in result["frameworks"]


def test_fallback_detect_stack_detects_rails(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'rails'\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["app.rb"])
    assert "ruby_rails" in result["frameworks"]


def test_fallback_detect_stack_detects_django(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==5.0\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["views.py"])
    assert "python_django" in result["frameworks"]


def test_fallback_detect_stack_detects_flask(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["app.py"])
    assert "python_flask" in result["frameworks"]


def test_fallback_detect_stack_no_frameworks_when_nothing_matches(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["a.py"])
    assert result["frameworks"] == []


def test_fallback_detect_stack_gemfile_no_rails(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'sinatra'\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["app.rb"])
    assert "ruby_rails" not in result["frameworks"]


def test_fallback_detect_stack_detects_swiftui(tmp_path: Path) -> None:
    swift_file = tmp_path / "ContentView.swift"
    swift_file.write_text(
        'import SwiftUI\n\nstruct ContentView: View {\n    var body: some View { Text("hi") }\n}\n',
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["ContentView.swift"])
    assert "swift_swiftui" in result["frameworks"]
    assert "swift_ios" not in result["frameworks"]


def test_fallback_detect_stack_detects_uikit(tmp_path: Path) -> None:
    swift_file = tmp_path / "ViewController.swift"
    swift_file.write_text(
        "import UIKit\n\nclass ViewController: UIViewController {\n}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["ViewController.swift"])
    assert "swift_ios" in result["frameworks"]
    assert "swift_swiftui" not in result["frameworks"]


def test_fallback_detect_stack_detects_both_swift_frameworks_when_mixed(tmp_path: Path) -> None:
    swift_file = tmp_path / "Bridge.swift"
    swift_file.write_text(
        "import SwiftUI\nimport UIKit\n\nstruct Bridge: View {\n"
        '    var body: some View { Text("x") }\n}\n'
        "class Host: UIViewController {}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["Bridge.swift"])
    assert "swift_ios" in result["frameworks"]
    assert "swift_swiftui" in result["frameworks"]


def test_fallback_detect_stack_swift_file_missing_on_disk_is_skipped(tmp_path: Path) -> None:
    """Content-based detection reads real files; a path that doesn't exist on
    disk (e.g. a deleted file still listed in a diff) must not crash."""
    result = fallback_detect_stack(tmp_path, ["Deleted.swift"])
    assert result["frameworks"] == []


def test_fallback_detect_stack_plain_swift_file_no_ui_framework(tmp_path: Path) -> None:
    swift_file = tmp_path / "Utils.swift"
    swift_file.write_text("struct Point { let x: Int; let y: Int }\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["Utils.swift"])
    assert result["frameworks"] == []


# ─── Kotlin: content-based Android vs. Spring detection ──────────────────


def test_fallback_detect_stack_detects_kotlin_android_import(tmp_path: Path) -> None:
    kt_file = tmp_path / "MainActivity.kt"
    kt_file.write_text(
        "import android.os.Bundle\nimport androidx.appcompat.app.AppCompatActivity\n\n"
        "class MainActivity : AppCompatActivity() {}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["MainActivity.kt"])
    assert "kotlin_android" in result["frameworks"]
    assert "kotlin_spring" not in result["frameworks"]


def test_fallback_detect_stack_detects_kotlin_android_compose(tmp_path: Path) -> None:
    kt_file = tmp_path / "Greeting.kt"
    kt_file.write_text(
        '@Composable\nfun Greeting(name: String) {\n    Text(text = "Hello $name")\n}\n',
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["Greeting.kt"])
    assert "kotlin_android" in result["frameworks"]


def test_fallback_detect_stack_detects_kotlin_spring(tmp_path: Path) -> None:
    kt_file = tmp_path / "UserController.kt"
    kt_file.write_text(
        "import org.springframework.web.bind.annotation.RestController\n\n"
        "@RestController\nclass UserController {}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["UserController.kt"])
    assert "kotlin_spring" in result["frameworks"]
    assert "kotlin_android" not in result["frameworks"]


def test_fallback_detect_stack_detects_both_kotlin_frameworks_when_mixed(tmp_path: Path) -> None:
    kt_file = tmp_path / "Mixed.kt"
    kt_file.write_text(
        "import android.os.Bundle\n"
        "import org.springframework.stereotype.Service\n\n"
        "@Service\nclass Mixed {}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["Mixed.kt"])
    assert "kotlin_android" in result["frameworks"]
    assert "kotlin_spring" in result["frameworks"]


def test_fallback_detect_stack_plain_kotlin_file_no_framework(tmp_path: Path) -> None:
    kt_file = tmp_path / "Utils.kt"
    kt_file.write_text("data class Point(val x: Int, val y: Int)\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["Utils.kt"])
    assert result["frameworks"] == []


def test_fallback_detect_stack_kotlin_file_missing_on_disk_is_skipped(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["Deleted.kt"])
    assert result["frameworks"] == []


# ─── TypeScript/JavaScript: content-based React vs. Next.js detection ────


def test_fallback_detect_stack_detects_react_from_content(tmp_path: Path) -> None:
    tsx_file = tmp_path / "App.tsx"
    tsx_file.write_text(
        "import React, { useState } from 'react';\n\n"
        "export function App() {\n  const [count, setCount] = useState(0);\n  return null;\n}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["App.tsx"])
    assert "typescript_react" in result["frameworks"]
    assert "typescript_nextjs" not in result["frameworks"]


def test_fallback_detect_stack_detects_nextjs_from_content(tmp_path: Path) -> None:
    page_file = tmp_path / "page.tsx"
    page_file.write_text(
        "\"use client\";\n\nimport { useRouter } from 'next/navigation';\n\n"
        "export default function Page() {\n  return null;\n}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["page.tsx"])
    assert "typescript_nextjs" in result["frameworks"]
    # Next.js implies React but only the more specific rubric is reported.
    assert "typescript_react" not in result["frameworks"]


def test_fallback_detect_stack_detects_nextjs_get_server_side_props(tmp_path: Path) -> None:
    page_file = tmp_path / "index.tsx"
    page_file.write_text(
        "export async function getServerSideProps() {\n  return { props: {} };\n}\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["index.tsx"])
    assert "typescript_nextjs" in result["frameworks"]


def test_fallback_detect_stack_react_content_detection_works_on_plain_js(tmp_path: Path) -> None:
    js_file = tmp_path / "App.jsx"
    js_file.write_text("import React from 'react';\nexport function App() { return null; }\n")
    result = fallback_detect_stack(tmp_path, ["App.jsx"])
    assert "typescript_react" in result["frameworks"]


def test_fallback_detect_stack_plain_ts_file_no_framework(tmp_path: Path) -> None:
    ts_file = tmp_path / "utils.ts"
    ts_file.write_text("export function add(a: number, b: number): number {\n  return a + b;\n}\n")
    result = fallback_detect_stack(tmp_path, ["utils.ts"])
    assert result["frameworks"] == []


def test_fallback_detect_stack_ts_file_missing_on_disk_is_skipped(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["Deleted.tsx"])
    assert result["frameworks"] == []


# ─── Python: content-based FastAPI/Django/Flask detection (no manifest) ──


def test_fallback_detect_stack_detects_fastapi_from_content_no_manifest(tmp_path: Path) -> None:
    py_file = tmp_path / "main.py"
    py_file.write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n"
        '@app.get("/")\ndef read_root():\n    return {"hello": "world"}\n',
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["main.py"])
    assert "python_fastapi" in result["frameworks"]


def test_fallback_detect_stack_detects_django_from_content_no_manifest(tmp_path: Path) -> None:
    py_file = tmp_path / "models.py"
    py_file.write_text(
        "from django.db import models\n\n"
        "class User(models.Model):\n    name = models.CharField()\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["models.py"])
    assert "python_django" in result["frameworks"]


def test_fallback_detect_stack_detects_flask_from_content_no_manifest(tmp_path: Path) -> None:
    py_file = tmp_path / "app.py"
    py_file.write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\n"
        '@app.route("/")\ndef index():\n    return "hi"\n',
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["app.py"])
    assert "python_flask" in result["frameworks"]


def test_fallback_detect_stack_manifest_and_content_detection_dedupe(tmp_path: Path) -> None:
    """When both the manifest check and the content check agree, the
    framework must appear only once in the final list."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    py_file = tmp_path / "main.py"
    py_file.write_text("from fastapi import FastAPI\n\napp = FastAPI()\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["main.py"])
    assert result["frameworks"].count("python_fastapi") == 1


def test_fallback_detect_stack_detects_multiple_python_frameworks_across_files(
    tmp_path: Path,
) -> None:
    """A monorepo diff can legitimately touch both a FastAPI service and a
    Django app in the same review; both should be reported."""
    (tmp_path / "service.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n", encoding="utf-8"
    )
    (tmp_path / "models.py").write_text(
        "from django.db import models\n\nclass User(models.Model):\n    pass\n",
        encoding="utf-8",
    )
    result = fallback_detect_stack(tmp_path, ["service.py", "models.py"])
    assert "python_fastapi" in result["frameworks"]
    assert "python_django" in result["frameworks"]


def test_fallback_detect_stack_plain_python_file_no_framework(tmp_path: Path) -> None:
    py_file = tmp_path / "utils.py"
    py_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["utils.py"])
    assert result["frameworks"] == []


def test_fallback_detect_stack_python_file_missing_on_disk_is_skipped(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["deleted.py"])
    assert result["frameworks"] == []


def test_get_rubric_names_from_stack_dedupes_and_orders() -> None:
    stack = {
        "languages": ["python", "typescript"],
        "frameworks": ["python_fastapi", "bogus_framework"],
    }
    names = get_rubric_names_from_stack(stack)
    assert names == ["python", "typescript", "python_fastapi"]


def test_fallback_detect_stack_detects_laravel(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "10.0"}}), encoding="utf-8"
    )
    result = fallback_detect_stack(tmp_path, ["routes/web.php"])
    assert "php_laravel" in result["frameworks"]


def test_fallback_detect_stack_composer_json_no_laravel(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"symfony/console": "6.0"}}), encoding="utf-8"
    )
    result = fallback_detect_stack(tmp_path, ["src/App.php"])
    assert "php_laravel" not in result["frameworks"]


def test_fallback_detect_stack_composer_json_corrupted_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text("{not valid json", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["src/App.php"])
    assert "php_laravel" not in result["frameworks"]


def test_fallback_detect_stack_detects_aspnet(tmp_path: Path) -> None:
    (tmp_path / "Program.cs").write_text("var app = WebApplication.Create();\n")
    result = fallback_detect_stack(tmp_path, ["Controllers/HomeController.cs"])
    assert "csharp_aspnet" in result["frameworks"]


def test_fallback_detect_stack_detects_dotnet_generic_when_not_aspnet(tmp_path: Path) -> None:
    (tmp_path / "app.csproj").write_text("<Project></Project>\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["Utils.cs"])
    assert "dotnet" in result["frameworks"]
    assert "csharp_aspnet" not in result["frameworks"]


def test_fallback_detect_stack_no_dotnet_project_no_framework(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["Utils.cs"])
    assert "dotnet" not in result["frameworks"]
    assert "csharp_aspnet" not in result["frameworks"]


def test_fallback_detect_stack_detects_java_android(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["MainActivity.java"])
    assert "java_android" in result["frameworks"]


def test_fallback_detect_stack_detects_java_spring(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text(
        "implementation 'org.springframework:spring-core'\n", encoding="utf-8"
    )
    result = fallback_detect_stack(tmp_path, ["App.java"])
    assert "java_spring" in result["frameworks"]


def test_fallback_detect_stack_gradle_kotlin_android_no_kt_content_needed(
    tmp_path: Path,
) -> None:
    """The gradle-based JVM detector reports kotlin_android from build.gradle
    content alone, given a .kt file is present, even if that file's own
    content doesn't match the content-based Kotlin detector's patterns."""
    (tmp_path / "build.gradle").write_text("android {}\n", encoding="utf-8")
    (tmp_path / "Constants.kt").write_text("const val MAX = 10\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["Constants.kt"])
    assert "kotlin_android" in result["frameworks"]


def test_fallback_detect_stack_no_gradle_file_no_jvm_framework(tmp_path: Path) -> None:
    result = fallback_detect_stack(tmp_path, ["App.java"])
    assert "java_android" not in result["frameworks"]
    assert "java_spring" not in result["frameworks"]


def test_fallback_detect_stack_gradle_file_no_recognized_pattern(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["App.java"])
    assert "java_android" not in result["frameworks"]
    assert "java_spring" not in result["frameworks"]


def test_fallback_detect_stack_settings_gradle_alone_triggers_check(tmp_path: Path) -> None:
    """settings.gradle existing (with no build.gradle content to read) is
    still a valid JVM-project signal, but yields no framework without a
    matching pattern in an actual build.gradle(.kts) file."""
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'app'\n", encoding="utf-8")
    result = fallback_detect_stack(tmp_path, ["App.java"])
    assert "java_android" not in result["frameworks"]
    assert "java_spring" not in result["frameworks"]


def test_get_rubric_names_from_stack_empty() -> None:
    assert get_rubric_names_from_stack({}) == []


def test_format_stack_context_header_includes_present_fields() -> None:
    stack = {"languages": ["python"], "frameworks": [], "architecture": "layered"}
    header = format_stack_context_header(stack)
    assert "python" in header
    assert "layered" in header


def test_format_stack_context_header_all_fields() -> None:
    stack = {
        "languages": ["python"],
        "frameworks": ["python_fastapi"],
        "architecture": "layered",
        "state_management": "redux",
        "data_layer": "sqlalchemy",
        "testing": "pytest",
        "api_style": "REST",
    }
    header = format_stack_context_header(stack)
    assert "**State management:** redux" in header
    assert "**Data layer:** sqlalchemy" in header
    assert "**Testing:** pytest" in header
    assert "**API style:** REST" in header
    assert "**Frameworks:** python_fastapi" in header


def test_format_stack_context_header_empty_stack_returns_empty_string() -> None:
    assert format_stack_context_header({}) == ""


def test_available_rubrics_contains_core_languages() -> None:
    assert "python" in AVAILABLE_RUBRICS
    assert "typescript" in AVAILABLE_RUBRICS
