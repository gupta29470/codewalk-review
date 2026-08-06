"""Typed exceptions for codewalk.

Core modules raise these specific exceptions; only the MCP tool wrapper layer
(`codewalk.mcp.server`) catches them and formats a friendly string response.
Keeping errors typed here (instead of ad-hoc strings) makes core logic
testable with `pytest.raises(...)`.
"""

from __future__ import annotations


class CodewalkError(Exception):
    """Base class for all codewalk-specific errors."""


class RepoNotConfiguredError(CodewalkError):
    """Raised when no valid repo root can be resolved for a requested operation."""


class GraphNotBuiltError(CodewalkError):
    """Raised when a graph-dependent operation runs before the graph has been built."""


class GraphCorruptedError(CodewalkError):
    """Raised when the persisted graph database exists but cannot be safely read."""


class GraphLockError(CodewalkError):
    """Raised when the graph database lock cannot be acquired within the retry budget."""


class InvalidDiffError(CodewalkError):
    """Raised when a git diff cannot be produced or parsed for review."""


class SessionNotFoundError(CodewalkError):
    """Raised when a review session id does not correspond to a persisted session."""


class InvalidFindingError(CodewalkError):
    """Raised when a host-submitted review finding fails schema or sanity validation."""


class PathTraversalError(CodewalkError):
    """Raised when a supplied path resolves outside the repo root."""


class ParseError(CodewalkError):
    """Raised when a source file cannot be parsed by any available grammar."""


class ConfigError(CodewalkError):
    """Raised for unrecoverable configuration problems (e.g. an unwritable .codewalk/ dir)."""
