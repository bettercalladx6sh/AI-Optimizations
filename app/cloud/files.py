"""Shared file output: JSON documents that are replaced in place, atomically."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.auth.common import ENV_PATH

PROJECT_ROOT = ENV_PATH.parent


def write_json(document: Any, path: Path) -> bool:
    """Create or replace a JSON file atomically; return True when an existing file was replaced.

    The JSON is written to a temporary file next to the target and renamed over it, so the
    file always holds either the previous or the new complete document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return existed


def display_path(path: Path) -> str:
    """Path relative to the project root when possible, for terminal output."""
    return str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)