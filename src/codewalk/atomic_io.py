"""Atomic JSON file writes shared by review session and stack-context persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    """Write `data` as JSON to `path` atomically.

    Writes to a sibling temp file first, then renames it over `path`. On
    POSIX, `os.replace` is atomic: readers never observe a partially-written
    file, and if the process crashes mid-write, the original file (if any)
    is left untouched and only an orphaned `.tmp` file remains.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=2)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
