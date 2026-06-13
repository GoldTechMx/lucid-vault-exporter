"""Filesystem helpers shared across the exporter. Windows-safe filenames are mandatory:
the vault must be writable on NTFS and SMB shares (this project lives on one)."""

from __future__ import annotations

import re
from pathlib import Path

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME = 120


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(name: str) -> str:
    cleaned = _FORBIDDEN.sub("_", name).strip().strip(".").strip()
    cleaned = cleaned[:_MAX_NAME].strip().strip(".")
    stripped = cleaned.strip("_")
    return cleaned if stripped else "untitled"


def unique_path(path: Path) -> Path:
    """Return `path` if free, else `name 2.ext`, `name 3.ext`, ..."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1
