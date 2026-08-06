"""Tests for codewalk.errors."""

from __future__ import annotations

import pytest

from codewalk.errors import (
    CodewalkError,
    ConfigError,
    GraphCorruptedError,
    GraphLockError,
    GraphNotBuiltError,
    InvalidDiffError,
    ParseError,
    PathTraversalError,
    RepoNotConfiguredError,
    SessionNotFoundError,
)

ALL_SUBCLASSES = [
    RepoNotConfiguredError,
    GraphNotBuiltError,
    GraphCorruptedError,
    GraphLockError,
    InvalidDiffError,
    SessionNotFoundError,
    PathTraversalError,
    ParseError,
    ConfigError,
]


@pytest.mark.parametrize("exc_cls", ALL_SUBCLASSES)
def test_all_errors_subclass_codewalk_error(exc_cls: type[CodewalkError]) -> None:
    assert issubclass(exc_cls, CodewalkError)
    assert issubclass(exc_cls, Exception)


@pytest.mark.parametrize("exc_cls", ALL_SUBCLASSES)
def test_errors_carry_a_message(exc_cls: type[CodewalkError]) -> None:
    with pytest.raises(exc_cls, match="boom"):
        raise exc_cls("boom")


def test_codewalk_error_is_catchable_as_base_class() -> None:
    """Callers that only know about CodewalkError still catch every subclass."""
    for exc_cls in ALL_SUBCLASSES:
        with pytest.raises(CodewalkError):
            raise exc_cls("some detail")


def test_subclasses_are_distinct_types() -> None:
    """A handler for one specific error must not accidentally swallow another."""
    assert not issubclass(SessionNotFoundError, GraphNotBuiltError)
    assert not issubclass(GraphNotBuiltError, SessionNotFoundError)

    with pytest.raises(SessionNotFoundError):
        raise SessionNotFoundError("wrong session")

    # A handler scoped to GraphNotBuiltError must not catch SessionNotFoundError.
    with pytest.raises(SessionNotFoundError):
        try:
            raise SessionNotFoundError("wrong session")
        except GraphNotBuiltError:
            pytest.fail("SessionNotFoundError must not be caught as GraphNotBuiltError")
