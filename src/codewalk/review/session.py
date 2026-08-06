"""Internal review session model."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    """Lifecycle status of a review session."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class ReviewSession:
    """Persistent metadata for one review run.

    Findings themselves live in the sibling ``llm_findings.json`` /
    ``static_findings.json`` files (see ``session_store.py``) rather than
    being embedded here, so there is exactly one place each finding is
    stored.
    """

    session_id: str
    repo_path: str
    target_branch: str | None
    commit: str | None
    staged: bool
    status: SessionStatus = SessionStatus.ACTIVE
    error: str | None = None
    folder_name: str = ""
    current_branch: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @staticmethod
    def generate_id() -> str:
        return secrets.token_urlsafe(12)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "repo_path": self.repo_path,
            "target_branch": self.target_branch,
            "commit": self.commit,
            "staged": self.staged,
            "status": self.status.value,
            "error": self.error,
            "folder_name": self.folder_name,
            "current_branch": self.current_branch,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ReviewSession:
        return ReviewSession(
            session_id=data["session_id"],
            repo_path=data["repo_path"],
            target_branch=data.get("target_branch"),
            commit=data.get("commit"),
            staged=data.get("staged", False),
            status=SessionStatus(data.get("status", "active")),
            error=data.get("error"),
            folder_name=data.get("folder_name", ""),
            current_branch=data.get("current_branch"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
