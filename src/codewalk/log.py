"""Structured logging configuration.

stdout is reserved for the MCP stdio transport protocol -- codewalk must never
write anything to stdout outside of the MCP SDK's own message framing. All
logging goes to stderr, and the "codewalk" logger never propagates to the
root logger (which some dependency could wire to stdout).
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "codewalk"
_HANDLER_NAME = "codewalk-stderr-handler"
_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotently configure the "codewalk" logger to write to stderr only."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        logger.setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return a logger under the "codewalk" namespace, configuring output on first use."""
    if not _configured:
        configure_logging()
    if name == _LOGGER_NAME or not name:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(_LOGGER_NAME).getChild(name)
