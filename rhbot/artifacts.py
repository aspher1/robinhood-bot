"""Write a new local report without overwriting files or touching bot state."""

from __future__ import annotations

import os
from pathlib import Path


def write_new_artifact(output: str | Path, content: str, state_dir: Path) -> Path:
    path = Path(output).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve()
    if path == state or path.is_relative_to(state):
        raise ValueError("output must be outside the bot state directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("output already exists; choose a new filename") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
