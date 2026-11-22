"""Zip, unzip and recursive archive listing."""

from __future__ import annotations

import io
import re
import tarfile
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .common import human_bytes, is_excluded, iter_files

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tar.bz2", ".tgz", ".tbz2", ".gz", ".bz2")
MAX_NESTED_BYTES = 200 * 1024 * 1024


def _safe_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or "\x00" in normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    if not parts or ".." in parts:
        return False
    return not re.match(r"^[A-Za-z]:$", parts[0])


def _is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


@contextmanager
def _archive(source: Path | bytes) -> Iterator[tuple[list[tuple[str, bool, int]], Callable[[str], bytes]]]:
    """Open a zip or tar archive; yield safe members and a per-member reader."""
    if isinstance(source, Path):
        buffer: io.BytesIO | None = None
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as zf:
                infos = [info for info in zf.infolist() if _safe_name(info.filename)]
                members = [(info.filename, info.is_dir(), info.file_size) for info in infos]
                lookup = {info.filename: info for info in infos}
                yield members, lambda name: zf.read(lookup[name])
            return
        if tarfile.is_tarfile(source):
            with tarfile.open(source, "r:*") as tf:
                infos = [
                    member
                    for member in tf.getmembers()
                    if _safe_name(member.name)
                    and not member.issym()
                    and not member.islnk()
                    and not member.isdev()
                ]
                members = [(m.name, m.isdir(), m.size) for m in infos]
                lookup = {m.name: m for m in infos}

                def read(name: str) -> bytes:
                    handle = tf.extractfile(lookup[name])
                    return handle.read() if handle is not None else b""

                yield members, read
            return
        raise ValueError("not a zip or tar archive")

    buffer = io.BytesIO(source)
    try:
        with zipfile.ZipFile(buffer) as zf:
            infos = [info for info in zf.infolist() if _safe_name(info.filename)]
            members = [(info.filename, info.is_dir(), info.file_size) for info in infos]
            lookup = {info.filename: info for info in infos}
            yield members, lambda name: zf.read(lookup[name])
        return
    except zipfile.BadZipFile:
        pass
    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r:*") as tf:
        infos = [
            member
            for member in tf.getmembers()
            if _safe_name(member.name)
            and not member.issym()
            and not member.islnk()
            and not member.isdev()
        ]
        members = [(m.name, m.isdir(), m.size) for m in infos]
        lookup = {m.name: m for m in infos}

        def read(name: str) -> bytes:
            handle = tf.extractfile(lookup[name])
            return handle.read() if handle is not None else b""

        yield members, read


def cmd_zip(args) -> int:
    out = Path(args.output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    excludes: list[str] = getattr(args, "exclude", [])
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for raw in args.paths:
            path = Path(raw).expanduser().resolve()
            if path == out:
                continue
            if path.is_file():
                zf.write(path, path.name)
                count += 1
            elif path.is_dir():
                entry_root = path.name or "root"
                for child in iter_files(path):
                    rel = child.relative_to(path).as_posix()
                    if is_excluded(rel, excludes):
                        continue
                    zf.write(child, f"{entry_root}/{rel}")
                    count += 1
            else:
                print(f"zip: no such path: {path}", file=sys.stderr)
    print(f"Created {out} with {count} file(s) ({human_bytes(out.stat().st_size)}).")
    return 0


def _extract_destination(archive: Path, dest: str | None) -> Path:
    if dest:
        return Path(dest).expanduser().resolve()
    name = archive.name
    for suffix in (".gz", ".bz2", ".zip", ".tar"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return archive.parent / name


def cmd_unzip(args) -> int:

    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        print(f"unzip: no such archive: {archive}", file=sys.stderr)
        return 1
    dest = _extract_destination(archive, args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with _archive(archive) as (members, _read):
            names = [name for name, is_dir, _size in members if not is_dir]
    except ValueError as error:
        print(f"unzip: {error}: {archive}", file=sys.stderr)
        return 1
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            infos = [info for info in zf.infolist() if _safe_name(info.filename)]
            zf.extractall(dest, members=infos)
    else:
        with tarfile.open(archive, "r:*") as tf:
            safe = [m for m in tf.getmembers() if _safe_name(m.name) and not m.issym() and not m.islnk() and not m.isdev()]
            tf.extractall(dest, members=safe)
    print(f"Extracted {len(names)} file(s) into {dest}")
    return 0


def _print_members(
    members: list[tuple[str, bool, int]],
    read: Callable[[str], bytes],
    prefix: str,
    depth: int,
    max_depth: int,
) -> None:
    for name, is_dir, size in members:
        if is_dir:
            continue
        print(f"{human_bytes(size):>8}  {prefix}{name}")
        if depth < max_depth and _is_archive_name(name) and size <= MAX_NESTED_BYTES:
            try:
                data = read(name)
                with _archive(data) as (sub_members, sub_read):
                    print(f"  -> nested archive: {prefix}{name}")
                    _print_members(sub_members, sub_read, f"{prefix}{name}/", depth + 1, max_depth)
            except (ValueError, KeyError, OSError, tarfile.ReadError, zipfile.BadZipFile):
                pass


def cmd_arclist(args) -> int:
    import sys

    max_depth = getattr(args, "max_depth", 6)
    for raw in args.paths:
        archive = Path(raw).expanduser().resolve()
        try:
            with _archive(archive) as (members, read):
                print(f"{archive}:")
                _print_members(members, read, "", 0, max_depth)
        except (ValueError, OSError, tarfile.ReadError, zipfile.BadZipFile) as error:
            print(f"arclist: {error}: {archive}", file=sys.stderr)
            return 1
    return 0
