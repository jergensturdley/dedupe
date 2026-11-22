"""Shared helpers for the dedupe command-line tools."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Iterator, Sequence

HASH_CHUNK = 1024 * 1024


def human_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024
        if value < 1024 or unit == "PB":
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def iter_files(root: Path) -> Iterator[Path]:
    """Yield regular files below root without following any symlinks."""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                yield path


def is_excluded(relative: str, patterns: Sequence[str]) -> bool:
    rel = relative.replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    for raw in patterns:
        pattern = raw.strip().replace("\\", "/").strip("/")
        if not pattern:
            continue
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
        if fnmatch.fnmatch(rel, pattern):
            return True
        if "/" in pattern and rel.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def hash_file(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\x00" in data
