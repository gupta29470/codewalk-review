"""Tests for codewalk.codewalk_config -- codewalk.yaml loading, never crashes."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from codewalk.codewalk_config import (
    CONFIG_FILE_NAME,
    CodewalkConfig,
    generate_default_config,
    load_codewalk_yaml,
)


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_codewalk_yaml(tmp_path)
    assert config == CodewalkConfig()
    assert config.exclude == []
    assert config.tools == {}


def test_well_formed_yaml_is_loaded(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        """
indexing:
  exclude: ["node_modules", "dist/**"]
  include: ["src/**"]
docs_path: docs
code_guidelines: docs/guidelines.md
language_overrides:
  ".proto": protobuf
tools:
  static_analysis:
    python: ["ruff", "check"]
""",
        encoding="utf-8",
    )

    config = load_codewalk_yaml(tmp_path)

    assert config.exclude == ["node_modules", "dist/**"]
    assert config.include == ["src/**"]
    assert config.docs_path == "docs"
    assert config.code_guidelines == "docs/guidelines.md"
    assert config.language_overrides == {".proto": "protobuf"}
    assert config.tools == {"static_analysis": {"python": ["ruff", "check"]}}


def test_empty_file_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("", encoding="utf-8")
    assert load_codewalk_yaml(tmp_path) == CodewalkConfig()


def test_malformed_yaml_syntax_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("indexing: [unclosed\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config == CodewalkConfig()
    assert any("invalid yaml" in message.lower() for message in caplog.messages)


def test_non_mapping_top_level_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("- just\n- a\n- list\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config == CodewalkConfig()
    assert any("expected a mapping" in message.lower() for message in caplog.messages)


def test_unknown_top_level_keys_warn_but_do_not_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "docs_path: docs\nsome_unknown_key: true\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.docs_path == "docs"
    assert any("unknown top-level" in message.lower() for message in caplog.messages)


def test_unknown_indexing_keys_warn_but_do_not_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "indexing:\n  exclude: [a]\n  bogus_key: 1\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.exclude == ["a"]
    assert any("unknown 'indexing'" in message.lower() for message in caplog.messages)


def test_wrong_type_for_exclude_falls_back_to_empty_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        'indexing:\n  exclude: "not-a-list"\n', encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.exclude == []
    assert any("indexing.exclude" in message for message in caplog.messages)


def test_wrong_type_for_docs_path_falls_back_to_empty_string(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("docs_path: 42\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.docs_path == ""
    assert any("docs_path" in message for message in caplog.messages)


def test_malformed_tools_entry_is_skipped_individually(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        """
tools:
  static_analysis:
    python: ["ruff", "check"]
    java: "not-a-list"
  bad_tool: "not-a-mapping"
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.tools == {"static_analysis": {"python": ["ruff", "check"]}}
    assert any("bad_tool" in message for message in caplog.messages)
    assert any("java" in message for message in caplog.messages)


def test_tools_top_level_wrong_type_falls_back_to_empty_dict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text('tools: "oops-not-a-mapping"\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.tools == {}
    assert any("'tools' must be a mapping" in message for message in caplog.messages)


def test_language_overrides_wrong_type_falls_back_to_empty_dict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        'language_overrides: ["not", "a", "mapping"]\n', encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.language_overrides == {}
    assert any("language_overrides" in message for message in caplog.messages)


def test_unreadable_file_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read any file regardless of permissions")

    path = tmp_path / CONFIG_FILE_NAME
    path.write_text("docs_path: docs\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="codewalk"):
            config = load_codewalk_yaml(tmp_path)
    finally:
        path.chmod(0o644)

    assert config == CodewalkConfig()
    assert any("could not read" in message.lower() for message in caplog.messages)


def test_indexing_wrong_type_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text('indexing: "not-a-mapping"\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="codewalk"):
        config = load_codewalk_yaml(tmp_path)

    assert config.exclude == []
    assert config.include == []
    assert any("'indexing' must be a mapping" in message for message in caplog.messages)


def test_config_is_frozen(tmp_path: Path) -> None:
    config = load_codewalk_yaml(tmp_path)
    with pytest.raises(Exception):  # noqa: B017 -- pydantic raises its own ValidationError subtype
        config.docs_path = "changed"  # type: ignore[misc]


def test_generate_default_config_creates_file(tmp_path: Path) -> None:
    path = generate_default_config(tmp_path)
    assert path == tmp_path / CONFIG_FILE_NAME
    assert path.exists()
    assert "indexing:" in path.read_text(encoding="utf-8")


def test_generate_default_config_does_not_overwrite_by_default(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_FILE_NAME
    path.write_text("custom: true\n", encoding="utf-8")

    result = generate_default_config(tmp_path)

    assert result == path
    assert path.read_text(encoding="utf-8") == "custom: true\n"


def test_generate_default_config_overwrites_with_force(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_FILE_NAME
    path.write_text("custom: true\n", encoding="utf-8")

    generate_default_config(tmp_path, force=True)

    assert "indexing:" in path.read_text(encoding="utf-8")


def test_generate_default_config_creates_missing_directory(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "repo"
    path = generate_default_config(nested)
    assert path.exists()
