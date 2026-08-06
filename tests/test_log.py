"""Tests for codewalk.log -- must never write to stdout (MCP stdio transport)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from codewalk import log as codewalk_log


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Each test gets a clean "codewalk" logger so configure_logging() re-runs."""
    logger = logging.getLogger("codewalk")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    codewalk_log._configured = False
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    codewalk_log._configured = False


def test_configure_logging_writes_only_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    codewalk_log.configure_logging(level=logging.DEBUG)
    logger = logging.getLogger("codewalk")

    logger.debug("debug message")
    logger.info("info message")
    logger.warning("warning message")
    logger.error("error message")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "info message" in captured.err
    assert "warning message" in captured.err
    assert "error message" in captured.err


def test_get_logger_configures_on_first_use(capsys: pytest.CaptureFixture[str]) -> None:
    logger = codewalk_log.get_logger("codewalk.some_module")
    logger.warning("hello")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err


def test_get_logger_skips_reconfiguring_when_already_configured() -> None:
    codewalk_log.configure_logging(level=logging.WARNING)
    logger = codewalk_log.get_logger("already_configured_child")
    assert logger.name == "codewalk.already_configured_child"


def test_get_logger_returns_child_of_codewalk_namespace() -> None:
    logger = codewalk_log.get_logger("my_module")
    assert logger.name == "codewalk.my_module"


def test_get_logger_default_returns_root_codewalk_logger() -> None:
    logger = codewalk_log.get_logger()
    assert logger.name == "codewalk"


def test_get_logger_empty_name_returns_root_codewalk_logger() -> None:
    logger = codewalk_log.get_logger("")
    assert logger.name == "codewalk"


def test_codewalk_logger_does_not_propagate_to_root(capsys: pytest.CaptureFixture[str]) -> None:
    """Guards against a dependency wiring the root logger to stdout."""
    root_handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        codewalk_log.configure_logging()
        logger = logging.getLogger("codewalk")
        assert logger.propagate is False
    finally:
        root.removeHandler(root_handler)


def test_configure_logging_is_idempotent() -> None:
    codewalk_log.configure_logging(level=logging.INFO)
    codewalk_log.configure_logging(level=logging.DEBUG)
    logger = logging.getLogger("codewalk")

    # A second call must not add a second copy of our own stderr handler.
    # (Other handlers, e.g. pytest's own log-capture handler, may also be
    # present on this logger and are not our concern here.)
    own_handlers = [h for h in logger.handlers if h.name == codewalk_log._HANDLER_NAME]
    assert len(own_handlers) == 1
    assert logger.level == logging.DEBUG
