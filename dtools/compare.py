"""Backup, folder comparison and directory size reporting."""

from __future__ import annotations

import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from .common import hash_file, human_bytes, is_excluded, iter_files


def _manifest(root: Path, excludes: list[str]) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel, excludes):
            continue
        result[rel] = (path.stat().st_size, hash_file(path))
    return result


def cmd_backup(args) -> int:
    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()
    excludes: list[str] = getattr(args, "exclude", [])
    if not src.exists():
        print(f"backup: no such source: {src}", file=sys.stderr)
        return 1
    if dst == src or dst.is_relative_to(src):
        print("backup: destination must be outside the source", file=sys.stderr)
        return 1
    if src.is_file():
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"Copied 1 file into {dst}")
        return 0
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = Path(dirpath).relative_to(src).as_posix()
        dirnames[:] = [
            name
            for name in dirnames
            if not is_excluded(f"{rel_dir}/{name}".strip("/"), excludes)
        ]
        for name in filenames:
            rel = f"{rel_dir}/{name}".strip("/")
            if is_excluded(rel, excludes):
                continue
            target = dst / rel
            if not args.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(dirpath) / name, target)
            copied += 1
    verb = "Would copy" if args.dry_run else "Copied"
    print(f"{verb} {copied} file(s) into {dst}")
    return 0


def cmd_compare(args) -> int:
    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()
    excludes: list[str] = getattr(args, "exclude", [])
    if not src.is_dir() or not dst.is_dir():
        print("compare: both arguments must be directories", file=sys.stderr)
        return 1
    left = _manifest(src, excludes)
    right = _manifest(dst, excludes)
    added = sorted(right.keys() - left.keys())
    removed = sorted(left.keys() - right.keys())
    changed = sorted(
        name
        for name in left.keys() & right.keys()
        if left[name][1] != right[name][1]
    )
    print(f"Compared {len(left)} vs {len(right)} file(s)")
    print(f"Added ({len(added)}):")
    for name in added[:50]:
        print(f"  + {name}")
    print(f"Removed ({len(removed)}):")
    for name in removed[:50]:
        print(f"  - {name}")
    print(f"Changed ({len(changed)}):")
    for name in changed[:50]:
        print(f"  * {name} ({human_bytes(left[name][0])} -> {human_bytes(right[name][0])})")
    return 0


def cmd_treesize(args) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"treesize: not a directory: {root}", file=sys.stderr)
        return 1
    total = 0
    per_folder: dict[str, int] = defaultdict(int)
    for path in iter_files(root):
        size = path.stat().st_size
        total += size
        rel = path.relative_to(root)
        for parent in rel.parents:
            if parent != Path("."):
                per_folder[parent.as_posix()] += size
    print(f"{human_bytes(total)}  {root}")
    for folder, size in sorted(per_folder.items(), key=lambda item: (-item[1], item[0]))[:40]:
        print(f"{human_bytes(size):>8}  {folder}")
    return 0
