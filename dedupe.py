#!/usr/bin/env python3
"""Find and remove duplicate files with an interactive terminal UI.

Usage:
    dedupe [PATH]              launch the TUI (PATH defaults to the current directory)
    dedupe --scan [PATH]       headless report, no UI and no changes
    dedupe --restore           restore the most recent trash batch
    dedupe --help              full option list

Deleted files are moved to ~/.dedupe_trash, never removed outright.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import shlex
import socket
import subprocess
import os
import secrets
import shutil
import sys
import threading
import time
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar
from dtools.common import human_bytes, iter_file_stats

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    OptionList,
    ProgressBar,
    Static,
)

TRASH_ROOT = Path.home() / ".dedupe_trash"
HASH_CHUNK = 1024 * 1024
SAMPLE_BYTES = 64 * 1024
HASH_WORKERS = min(8, os.cpu_count() or 4)
SAMPLE_WORKERS = min(4, HASH_WORKERS)
PROGRESS_INTERVAL = 0.05
_THREAD_LOCAL = threading.local()
_T = TypeVar("_T")
_R = TypeVar("_R")


# --------------------------------------------------------------------------- #
# Core                                                                        #
# --------------------------------------------------------------------------- #




@dataclass
class Group:
    index: int
    files: list[Path]
    size: int
    keeper: Path
    marked: set[Path] = field(default_factory=set)
    mtimes: dict[Path, float] = field(default_factory=dict)

    @property
    def wasted(self) -> int:
        return self.size * (len(self.files) - 1)


@dataclass
class ScanResult:
    root: Path
    scanned: int
    groups: list[Group]

    @property
    def wasted(self) -> int:
        return sum(group.wasted for group in self.groups)


@dataclass(frozen=True)
class ScanProgress:
    phase: str
    files: int = 0
    total: int = 0
    bytes_read: int = 0
    groups: int = 0

    @property
    def label(self) -> str:
        return {
            "walking": "walking tree",
            "sampling": "sampling candidates",
            "hashing": "hashing candidates",
            "grouping": "grouping duplicates",
        }.get(self.phase, self.phase)


def _read_buffer() -> bytearray:
    buf = getattr(_THREAD_LOCAL, "buf", None)
    if buf is None:
        buf = bytearray(HASH_CHUNK)
        _THREAD_LOCAL.buf = buf
    return buf


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    chunk = os.pread(fd, length, offset)
    if len(chunk) == length or not chunk:
        return chunk
    parts = [chunk]
    remaining = length - len(chunk)
    pos = offset + len(chunk)
    while remaining > 0:
        piece = os.pread(fd, remaining, pos)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
        pos += len(piece)
    return b"".join(parts)


def digest_file(
    path: Path | str, cancel: threading.Event | None = None
) -> tuple[bytes, int] | None:
    digest = hashlib.blake2b(digest_size=16)
    read = 0
    buf = _read_buffer()
    mv = memoryview(buf)
    try:
        with open(path, "rb", buffering=0) as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                n = handle.readinto(mv)
                if not n:
                    break
                digest.update(mv[:n])
                read += n
    except OSError:
        return None
    return digest.digest(), read


def quick_key(path: Path | str, size: int) -> bytes | None:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(size.to_bytes(8, "big"))
    try:
        if hasattr(os, "pread"):
            fd = os.open(path, os.O_RDONLY)
            try:
                digest.update(_pread_exact(fd, SAMPLE_BYTES, 0))
                if size > SAMPLE_BYTES * 2:
                    digest.update(_pread_exact(fd, SAMPLE_BYTES, size - SAMPLE_BYTES))
            finally:
                os.close(fd)
        else:
            with open(path, "rb", buffering=0) as handle:
                digest.update(handle.read(SAMPLE_BYTES))
                if size > SAMPLE_BYTES * 2:
                    handle.seek(size - SAMPLE_BYTES)
                    digest.update(handle.read(SAMPLE_BYTES))
    except OSError:
        return None
    return digest.digest()


def _sample_or_hash(
    item: tuple[int, str], cancel: threading.Event | None = None
) -> tuple[bool, int, str, bytes, int] | None:
    if cancel is not None and cancel.is_set():
        return None
    size, path = item
    digest = hashlib.blake2b(digest_size=16)
    try:
        if size <= SAMPLE_BYTES * 2:
            with open(path, "rb", buffering=0) as handle:
                data = handle.read()
            digest.update(data)
            actual = len(data)
            return True, actual, path, digest.digest(), actual
        digest.update(size.to_bytes(8, "big"))
        if hasattr(os, "pread"):
            fd = os.open(path, os.O_RDONLY)
            try:
                head = _pread_exact(fd, SAMPLE_BYTES, 0)
                tail = _pread_exact(fd, SAMPLE_BYTES, size - SAMPLE_BYTES)
            finally:
                os.close(fd)
        else:
            with open(path, "rb", buffering=0) as handle:
                head = handle.read(SAMPLE_BYTES)
                handle.seek(size - SAMPLE_BYTES)
                tail = handle.read(SAMPLE_BYTES)
        digest.update(head)
        digest.update(tail)
        return False, size, path, digest.digest(), len(head) + len(tail)
    except OSError:
        return None


def _chunk_by_bytes(
    items: Iterable[tuple[int, str]],
    max_items: int = 64,
    max_bytes: int = 2 * 1024 * 1024,
    per_item_cap: int | None = None,
) -> Iterator[list[tuple[int, str]]]:
    batch: list[tuple[int, str]] = []
    batch_bytes = 0
    for item in items:
        size = item[0]
        cost = min(size, per_item_cap) if per_item_cap is not None else size
        if batch and (len(batch) >= max_items or batch_bytes + cost > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += cost
    if batch:
        yield batch


def _sample_batch(
    batch: list[tuple[int, str]], cancel: threading.Event | None = None
) -> tuple[int, list[tuple[bool, int, str, bytes, int]]]:
    outcomes: list[tuple[bool, int, str, bytes, int]] = []
    processed = 0
    for item in batch:
        if cancel is not None and cancel.is_set():
            break
        processed += 1
        outcome = _sample_or_hash(item, cancel)
        if outcome is not None:
            outcomes.append(outcome)
    return processed, outcomes


def _hash_batch(
    batch: list[tuple[int, str]], cancel: threading.Event | None = None
) -> list[tuple[int, str, bytes, int]]:
    outcomes: list[tuple[int, str, bytes, int]] = []
    for size, path_str in batch:
        if cancel is not None and cancel.is_set():
            break
        result = digest_file(path_str, cancel)
        if result is not None:
            digest, read = result
            outcomes.append((size, path_str, digest, read))
    return outcomes


def _run_bounded(
    pool: ThreadPoolExecutor,
    fn: Callable[[_T], _R],
    items: Iterable[_T],
    cancel: threading.Event | None = None,
    max_in_flight: int = HASH_WORKERS * 4,
) -> Iterator[_R]:
    iterator = iter(items)
    pending: set[Future[_R]] = set()

    def fill() -> None:
        while len(pending) < max_in_flight:
            if cancel is not None and cancel.is_set():
                return
            try:
                item = next(iterator)
            except StopIteration:
                return
            pending.add(pool.submit(fn, item))

    fill()
    while pending:
        if cancel is not None and cancel.is_set():
            for fut in pending:
                fut.cancel()
            return
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for fut in done:
            if cancel is not None and cancel.is_set():
                for rem in pending:
                    rem.cancel()
                return
            try:
                result = fut.result()
            except OSError:
                continue
            yield result
        fill()


def choose_keeper(
    files: list[Path],
    strategy: str,
    mtimes: dict[Path, float] | None = None,
) -> Path:
    def mtime_of(path: Path) -> float:
        if mtimes is not None and path in mtimes:
            return mtimes[path]
        return path.stat().st_mtime

    if strategy == "oldest":
        return min(files, key=lambda p: (mtime_of(p), len(p.parts), str(p)))
    if strategy == "newest":
        return max(files, key=lambda p: (mtime_of(p), -len(p.parts), str(p)))
    return min(files, key=lambda p: (len(p.parts), len(str(p)), str(p)))


def scan(
    root: Path,
    keep: str = "shortest",
    progress: Callable[[ScanProgress], None] | None = None,
    min_size: int = 0,
    cancel: threading.Event | None = None,
) -> ScanResult:
    root = root.expanduser().resolve()
    last_progress: ScanProgress | None = None
    last_report_at = 0.0

    def report(state: ScanProgress, force: bool = False) -> None:
        nonlocal last_progress, last_report_at
        if progress is None or state == last_progress:
            return
        now = time.monotonic()
        if force or now - last_report_at >= PROGRESS_INTERVAL:
            progress(state)
            last_progress = state
            last_report_at = now

    first_path: dict[int, str] = {}
    first_mtime: dict[int, float] = {}
    dups_by_size: dict[int, list[str]] = {}
    dup_mtimes: dict[str, float] = {}
    scanned = 0

    for path_str, info in iter_file_stats(root):
        size = info.st_size
        if size < min_size:
            continue
        scanned += 1
        if size > 0:
            prev = first_path.get(size)
            if prev is None:
                first_path[size] = path_str
                first_mtime[size] = info.st_mtime
            else:
                bucket = dups_by_size.get(size)
                if bucket is None:
                    dups_by_size[size] = [prev, path_str]
                    dup_mtimes[prev] = first_mtime[size]
                else:
                    bucket.append(path_str)
                dup_mtimes[path_str] = info.st_mtime
        if (scanned & 511) == 0:
            if cancel is not None and cancel.is_set():
                return ScanResult(root=root, scanned=scanned, groups=[])
            report(ScanProgress("walking", files=scanned))
    first_path.clear()
    first_mtime.clear()
    if cancel is not None and cancel.is_set():
        return ScanResult(root=root, scanned=scanned, groups=[])
    report(ScanProgress("walking", files=scanned), force=True)

    sampling_total = sum(len(paths) for paths in dups_by_size.values())
    sampling_done = 0
    sampled_bytes = 0
    quick: dict[tuple[int, bytes], list[str]] = {}
    full: dict[tuple[int, bytes], list[str]] = {}

    if sampling_total > 0:
        est_sample_bytes = sum(
            min(size, SAMPLE_BYTES * 2) * len(paths)
            for size, paths in dups_by_size.items()
        )
        sample_workers = (
            min(2, SAMPLE_WORKERS)
            if est_sample_bytes < sampling_total * 16 * 1024
            else SAMPLE_WORKERS
        )
        sample_batches = _chunk_by_bytes(
            (
                (size, path_str)
                for size, paths in dups_by_size.items()
                for path_str in paths
            ),
            per_item_cap=SAMPLE_BYTES * 2,
        )
        with ThreadPoolExecutor(max_workers=sample_workers) as sample_pool:
            for processed, outcomes in _run_bounded(
                sample_pool,
                lambda b: _sample_batch(b, cancel),
                sample_batches,
                cancel,
                max_in_flight=sample_workers * 4,
            ):
                sampling_done += processed
                for is_full, actual_size, path_str, key, nbytes in outcomes:
                    sampled_bytes += nbytes
                    if is_full:
                        if actual_size >= min_size and actual_size > 0:
                            full.setdefault((actual_size, key), []).append(path_str)
                    else:
                        quick.setdefault((actual_size, key), []).append(path_str)
                report(
                    ScanProgress(
                        "sampling", sampling_done, sampling_total, sampled_bytes
                    )
                )
        if cancel is not None and cancel.is_set():
            return ScanResult(root=root, scanned=scanned, groups=[])
        report(
            ScanProgress("sampling", sampling_done, sampling_total, sampled_bytes),
            force=True,
        )

        candidates = [
            (size, path_str)
            for (size, _key), bucket in quick.items()
            if len(bucket) > 1
            for path_str in bucket
        ]
        quick.clear()
        hash_total = len(candidates)
        completed = 0
        hashed_bytes = 0
        if hash_total > 0:
            hash_batches = _chunk_by_bytes(candidates)
            with ThreadPoolExecutor(max_workers=HASH_WORKERS) as hash_pool:
                for outcomes in _run_bounded(
                    hash_pool,
                    lambda b: _hash_batch(b, cancel),
                    hash_batches,
                    cancel,
                ):
                    for size, path_str, digest, read in outcomes:
                        full.setdefault((size, digest), []).append(path_str)
                        completed += 1
                        hashed_bytes += read
                    report(
                        ScanProgress("hashing", completed, hash_total, hashed_bytes)
                    )
            if cancel is not None and cancel.is_set():
                return ScanResult(root=root, scanned=scanned, groups=[])
        report(
            ScanProgress("hashing", completed, hash_total, hashed_bytes),
            force=True,
        )
    else:
        report(ScanProgress("sampling", 0, 0, 0), force=True)
        report(ScanProgress("hashing", 0, 0, 0), force=True)
        hashed_bytes = 0

    groups: list[Group] = []
    for (size, _digest), bucket in full.items():
        if len(bucket) < 2:
            continue
        files = [Path(p) for p in sorted(bucket)]
        group_mtimes = {
            path: dup_mtimes[path_str]
            for path, path_str in zip(files, sorted(bucket))
            if path_str in dup_mtimes
        }
        groups.append(
            Group(
                index=len(groups),
                files=files,
                size=size,
                keeper=choose_keeper(files, keep, mtimes=group_mtimes),
                mtimes=group_mtimes,
            )
        )
    report(
        ScanProgress("grouping", len(groups), len(groups), hashed_bytes),
        force=True,
    )

    groups.sort(key=lambda g: (-g.wasted, str(g.files[0])))
    for index, group in enumerate(groups):
        group.index = index
    return ScanResult(root=root, scanned=scanned, groups=groups)


# --------------------------------------------------------------------------- #
# Trash                                                                       #
# --------------------------------------------------------------------------- #


def trash_files(paths: list[Path]) -> None:
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = TRASH_ROOT / f"{stamp}-{secrets.token_hex(3)}"
    batch.mkdir()
    manifest = batch / "manifest.json"
    created = datetime.now().isoformat(timespec="seconds")
    entries: list[dict[str, str]] = []
    used: set[str] = set()

    def save() -> None:
        manifest.write_text(
            json.dumps({"created": created, "entries": entries}, indent=2)
        )

    try:
        for index, path in enumerate(sorted(paths, key=str), start=1):
            original = path.resolve()
            name = original.name
            destination = batch / name
            counter = 1
            while name in used or destination.exists():
                name = f"{original.stem}-{counter}{original.suffix}"
                destination = batch / name
                counter += 1
            used.add(name)
            shutil.move(str(original), str(destination))
            entries.append({"original": str(original), "stored": name})
            if index % 250 == 0:
                save()
    finally:
        if entries:
            save()


def restore_batch(manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text())
    restored = 0
    for entry in manifest["entries"]:
        original = Path(entry["original"])
        stored = manifest_path.parent / entry["stored"]
        if not stored.exists() or original.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored), str(original))
        restored += 1
    shutil.rmtree(manifest_path.parent)
    return restored


def restore_last_batch() -> int:
    if not TRASH_ROOT.exists():
        return 0
    manifests = sorted(
        TRASH_ROOT.glob("*/manifest.json"),
        key=lambda path: json.loads(path.read_text())["created"],
    )
    if not manifests:
        return 0
    return restore_batch(manifests[-1])


def trash_batches() -> list[tuple[Path, dict]]:
    """Return (manifest_path, manifest) pairs, oldest first."""
    if not TRASH_ROOT.exists():
        return []
    batches: list[tuple[Path, dict]] = []
    for manifest_path in TRASH_ROOT.glob("*/manifest.json"):
        try:
            batches.append((manifest_path, json.loads(manifest_path.read_text())))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    batches.sort(key=lambda item: item[1].get("created", ""))
    return batches


# --------------------------------------------------------------------------- #
# TUI widgets                                                                 #
# --------------------------------------------------------------------------- #


def short_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) if path != root else path.name
    except ValueError:
        return str(path)


def _group_row(group: Group, root: Path) -> Text:
    row = Text()
    row.append(f"{group.index + 1:02}  ", style="dim")
    row.append(f"{human_bytes(group.wasted):>9}", style="bold red")
    row.append(f"  {len(group.files)}x ", style="cyan")
    row.append(short_path(group.files[0], root), style="white")
    return row


def _file_row(group: Group, path: Path, root: Path) -> Text:
    marked = path in group.marked
    row = Text()
    row.append("[x] " if marked else "[ ] ", style="green" if marked else "dim")
    row.append("keep " if path == group.keeper else "drop ", style="dim")
    row.append(short_path(path, root), style="white")
    return row


def _scan_dir_entries(root: Path) -> list[tuple[Path, bool, int]]:
    items: list[tuple[str, str, bool, int]] = []
    with os.scandir(root) as it:
        for entry in it:
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                items.append((entry.name, entry.path, is_dir, size))
            except OSError:
                continue
    items.sort(key=lambda item: (item[2], item[0].lower()))
    return [(Path(path), is_dir, size) for _name, path, is_dir, size in items]


class GroupItem(ListItem):
    def __init__(self, group: Group, root: Path) -> None:
        super().__init__()
        self.group = group
        self.root = root

    def compose(self) -> ComposeResult:
        yield Static(_group_row(self.group, self.root), classes="group-row")


class FileItem(ListItem):
    def __init__(self, group: Group, path: Path, root: Path) -> None:
        super().__init__()
        self.group = group
        self.path = path
        self.root = root

    def compose(self) -> ComposeResult:
        yield Static(_file_row(self.group, self.path, self.root), classes="file-row")


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { background: #070b10; }
    #dialog {
        width: 64;
        height: auto;
        border: solid #2f6f52;
        background: #0f1922;
        padding: 1 2;
    }
    #headline { color: #7ee787; text-style: bold; }
    #body { color: #b8c4d2; margin: 1 0; }
    #buttons { height: 3; }
    #buttons > Button { width: 1fr; margin: 0 1; }
    """

    def __init__(self, count: int, wasted: int) -> None:
        super().__init__()
        self.count = count
        self.wasted = wasted

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static("Move duplicates to trash?", id="headline")
                yield Static(
                    f"{self.count} file(s) · {human_bytes(self.wasted)} will be reclaimed.\n"
                    "Undo later with: dedupe --restore",
                    id="body",
                )
                with Horizontal(id="buttons"):
                    yield Button("Cancel", id="cancel")
                    yield Button("Move to trash", id="confirm", variant="success")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(False)


def _qr_block_text(url: str) -> str:
    import qrcode

    qr = qrcode.QRCode(
        border=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines: list[str] = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < len(matrix) else [False] * len(top)
        row: list[str] = []
        for a, b in zip(top, bottom):
            if a and b:
                row.append("█")
            elif a:
                row.append("▀")
            elif b:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


def _serve_url(args: argparse.Namespace) -> str:
    from dtools.server import _lan_ip

    target = Path(args.path).expanduser().resolve()
    host = _lan_ip() if args.host in ("0.0.0.0", "") else args.host
    suffix = quote(target.name, safe="") if target.is_file() else ""
    return f"http://{host}:{args.port}/{suffix}"

class ServerScreen(Screen):
    TITLE = "Share"
    CSS = """
    ServerScreen { background: #0d1117; }
    ServerScreen #columns { height: 1fr; padding: 1 2; }
    ServerScreen #browser, ServerScreen #side {
        border: solid #253244;
        background: #101822;
        padding: 1;
    }
    ServerScreen #browser { width: 42%; }
    ServerScreen #side { width: 1fr; }
    ServerScreen .pane-title { color: #7dd3fc; text-style: bold; height: 1; }
    ServerScreen #cwd, ServerScreen #status {
        color: #68798b;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ServerScreen OptionList {
        height: 1fr;
        border: none;
        padding: 0;
        background: #101822;
        color: #c9d4e2;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ServerScreen OptionList:focus {
        border: none;
        background-tint: transparent;
    }
    ServerScreen OptionList > .option-list--option { padding: 0 1; }
    ServerScreen OptionList > .option-list--option-highlighted { background: #172a38; }
    ServerScreen OptionList:focus > .option-list--option-highlighted { background: #1e3a4a; }
    ServerScreen #selection { color: #c9d4e2; height: 2; text-wrap: wrap; }
    ServerScreen #url { color: #7dd3fc; height: 2; text-wrap: wrap; }
    ServerScreen #auth {
        color: #94a3b8;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ServerScreen #args, ServerScreen #secret {
        background: #0b1219;
        border: solid #253244;
    }
    ServerScreen #buttons { height: 2; }
    ServerScreen #buttons > Button { width: 1fr; margin: 0; }
    ServerScreen #qr-scroll { height: 1fr; }
    ServerScreen #qr { color: #94a3b8; text-wrap: nowrap; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("enter", "activate", "Open/Select"),
        Binding("s", "share", "Share", priority=True),
        Binding("g", "generate_password", "Gen password", priority=True),
        Binding("x", "stop", "Stop", priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("u", "parent", "Up"),
    ]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self.initial = initial
        self.root = Path.cwd().resolve()
        self.entries: list[Path] = []
        self._entry_is_dir: list[bool] = []
        self._load_gen = 0
        self.selected: Path | None = None
        self.server_url = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="columns"):
            with Vertical(id="browser"):
                yield Static("  FILES IN PWD", classes="pane-title")
                yield Static("", id="cwd")
                yield OptionList(id="entries")
            with Vertical(id="side"):
                yield Static("  SHARE", classes="pane-title")
                yield Static("", id="selection")
                yield Static("", id="url")
                yield Static("", id="auth")
                yield Input(id="args", placeholder="flags (optional): --port 0 --host 127.0.0.1")
                yield Input(
                    id="secret",
                    password=True,
                    placeholder="shared password (blank = open; g generates)",
                )
                with Horizontal(id="buttons"):
                    yield Button("Share", id="share", variant="success")
                    yield Button("Stop", id="stop")
                    yield Button("Back", id="back")
                with VerticalScroll(id="qr-scroll"):
                    yield Static("", id="qr")
                yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#args", Input).value = self.initial
        await self.reload_entries()
        self.query_one("#entries", OptionList).focus()
        proc = getattr(self.app, "server_process", None)
        if proc is not None and proc.poll() is None:
            url = getattr(self.app, "server_url", "")
            self._show_server(url)
            self._set_auth(getattr(self.app, "server_password", None), "running")
            self._set_status("Server is running; press x to stop.")
        else:
            self._set_selection(None, is_dir=True)
            self._set_auth(None)
            self._set_status("Click a file or use arrows + Enter.")

    async def reload_entries(self) -> None:
        self._load_gen += 1
        gen = self._load_gen
        target_root = self.root
        view = self.query_one("#entries", OptionList)
        self.query_one("#cwd", Static).update(str(target_root))
        try:
            scanned = await asyncio.to_thread(_scan_dir_entries, target_root)
        except OSError as error:
            if gen == self._load_gen:
                view.clear_options()
                self.entries = []
                self._entry_is_dir = []
                self._set_status(f"Cannot read {target_root}: {error}")
            return
        if gen != self._load_gen:
            return

        entries: list[Path] = []
        is_dirs: list[bool] = []
        rows: list[Text] = []
        if target_root.parent != target_root:
            entries.append(target_root.parent)
            is_dirs.append(True)
            rows.append(Text("..", style="bold cyan"))
        for path, is_dir, size in scanned:
            entries.append(path)
            is_dirs.append(is_dir)
            if is_dir:
                rows.append(Text("dir", style="bold cyan").append(f"  {path.name}"))
            else:
                rows.append(
                    Text("file", style="dim").append(
                        f" {human_bytes(size):>8}  {path.name}"
                    )
                )

        self.entries = entries
        self._entry_is_dir = is_dirs
        view.clear_options()
        view.add_options(rows)
        if self.entries:
            view.highlighted = 0

    @on(OptionList.OptionHighlighted, "#entries")
    def preview_entry(self, event: OptionList.OptionHighlighted) -> None:
        index = event.option_index
        if 0 <= index < len(self.entries):
            self._set_selection(self.entries[index], is_dir=self._entry_is_dir[index])

    @on(OptionList.OptionSelected, "#entries")
    async def activate_entry(self, event: OptionList.OptionSelected) -> None:
        await self._activate_index(event.option_index)

    async def action_activate(self) -> None:
        view = self.query_one("#entries", OptionList)
        if view.highlighted is not None:
            await self._activate_index(view.highlighted)

    async def _activate_index(self, index: int) -> None:
        if not 0 <= index < len(self.entries):
            return
        path = self.entries[index]
        is_dir = self._entry_is_dir[index]
        if path == self.root.parent:
            self.root = path
            await self.reload_entries()
            self._set_status(f"Opened {path}; press u to go up.")
        elif is_dir:
            self.root = path
            await self.reload_entries()
            self._set_status(f"Opened {path.name}; click a file to select it.")
        else:
            self.selected = path
            self._set_selection(path, is_dir=False)
            self._set_status(f"Selected {path.name}; press s or click Share. Flags are optional.")

    def _set_selection(self, path: Path | None, is_dir: bool | None = None) -> None:
        target = path or self.root
        if is_dir is None:
            is_dir = target.is_dir()
        kind = "folder" if is_dir else "file"
        self.query_one("#selection", Static).update(
            f"{kind}: {target}\nPress s or click Share to serve this {kind}."
        )
    def _set_auth(self, password: str | None, source: str = "") -> None:
        detail = f" ({source})" if source and source != "running" else ""
        note = (
            f"Password required{detail}; browser will prompt."
            if password
            else "Open access; no password gate."
        )
        self.query_one("#auth", Static).update(note)

    def _show_server(self, url: str) -> None:
        self.server_url = url
        self.query_one("#url", Static).update(url)
        self.query_one("#qr", Static).update(_qr_block_text(url))

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    @on(Button.Pressed, "#share")
    async def share_from_button(self) -> None:
        await self.action_share()

    @on(Button.Pressed, "#stop")
    def stop_from_button(self) -> None:
        self.action_stop()

    @on(Button.Pressed, "#back")
    def back_from_button(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.dismiss()

    def action_generate_password(self) -> None:
        password = secrets.token_urlsafe(12)
        self.query_one("#secret", Input).value = password
        self._set_auth(password, "generated")
        self._set_status(f"Generated password: {password}; share it separately.")

    async def action_share(self) -> None:
        proc = getattr(self.app, "server_process", None)
        if proc is not None and proc.poll() is None:
            self._set_status("Server already running; press x to stop first.")
            return
        from dtools import server as server_tools

        parts = shlex.split(self.query_one("#args", Input).value)
        target = self.selected or self.root
        if parts and not parts[0].startswith("-"):
            typed = Path(parts[0]).expanduser()
            if typed.exists():
                target = typed.resolve()
                parts = parts[1:]
        if not target.exists():
            self._set_status(f"No such path: {target}")
            return

        secret = self.query_one("#secret", Input).value
        has_password = any(
            token == "--password" or token.startswith("--password=") for token in parts
        )
        if secret and not has_password:
            parts += ["--password", secret]
            has_password = True
        generated = ""
        if "--auth" in parts:
            parts = [token for token in parts if token != "--auth"]
            if not has_password and not os.environ.get(server_tools.AUTH_ENV):
                generated = secrets.token_urlsafe(12)
                parts += ["--password", generated]
                has_password = True
        try:
            args = build_tool_parser().parse_args(["serve", str(target), *parts])
        except SystemExit:
            self._set_status("Invalid flags; try `dedupe serve --help`.")
            return

        argv = ["serve", str(target), *parts, "--host", args.host, "--port", str(args.port)]
        if args.port == 0:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            args.port = sock.getsockname()[1]
            sock.close()
            argv = [
                "serve",
                str(target),
                *parts,
                "--host",
                args.host,
                "--port",
                str(args.port),
            ]
        if args.png and "--png" not in parts:
            argv += ["--png", args.png]

        password, source = server_tools.resolve_password(args)
        url = _serve_url(args)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *argv],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.app.server_process = child
        self.app.server_url = url
        self.app.server_password = password
        self._show_server(url)
        self._set_auth(password, source)
        if generated:
            self._set_status(f"Generated password: {generated}; share it separately.")
        else:
            self._set_status(f"Sharing {target}; press x to stop.")

    def action_stop(self) -> None:
        proc = getattr(self.app, "server_process", None)
        if proc is None or proc.poll() is not None:
            self._set_status("No server is running.")
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        self.app.server_process = None
        self.app.server_password = None
        self.server_url = ""
        self.query_one("#url", Static).update("")
        self.query_one("#qr", Static).update("")
        self._set_auth(None)
        self._set_status("Server stopped.")

    async def action_parent(self) -> None:
        if self.root.parent != self.root:
            self.root = self.root.parent
            await self.reload_entries()
            self._set_status(f"Opened {self.root}; press u to go up.")

    async def action_refresh(self) -> None:
        await self.reload_entries()
        self._set_status("Refreshed.")

class ToolsScreen(Screen):
    TITLE = "Tools"
    CSS = """
    ToolsScreen { background: #0d1117; }
    ToolsScreen #columns { height: 1fr; padding: 1 2; }
    ToolsScreen #tool-pane, ToolsScreen #run-pane {
        border: solid #253244;
        background: #101822;
        padding: 1;
    }
    ToolsScreen #tool-pane { width: 32%; }
    ToolsScreen #run-pane { width: 1fr; }
    ToolsScreen .pane-title { color: #7dd3fc; text-style: bold; height: 1; }
    ToolsScreen ListItem { height: 1; padding: 0 1; }
    ToolsScreen .tool-name { height: 1; text-wrap: nowrap; }
    ToolsScreen #args { background: #0b1219; border: solid #253244; }
    ToolsScreen #tool-hint { color: #7dd3fc; height: 2; text-wrap: wrap; }
    ToolsScreen #tool-info {
        color: #94a3b8;
        height: 4;
        padding: 0 1;
        text-wrap: wrap;
        background: #0b1219;
        border: solid #1f2d3d;
    }
    ToolsScreen #buttons { height: 2; }
    ToolsScreen #buttons > Button { width: 1fr; margin: 0; }
    ToolsScreen #output-scroll {
        border: solid #1f2d3d;
        height: 1fr;
        min-height: 2;
    }
    ToolsScreen #output { color: #c9d4e2; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("enter", "run", "Run", priority=True),
        Binding("x", "stop_server", "Stop server"),
    ]

    COMMANDS = [
        (
            "zip",
            "zip -o out.zip PATH... [--exclude PATTERN]",
            "Packs files and folders into one ZIP; honors exclude globs.",
        ),
        (
            "unzip",
            "unzip ARCHIVE [-d DIR]",
            "Extracts ZIP/tar into a sibling folder; rejects unsafe paths and links.",
        ),
        (
            "arclist",
            "arclist ARCHIVE... [--max-depth 6]",
            "Lists members and sizes, including nested archives up to max-depth.",
        ),
        (
            "backup",
            "backup SRC DST [--exclude PATTERN] [--dry-run]",
            "Copies a tree with timestamps; skips excludes and never writes inside source.",
        ),
        (
            "compare",
            "compare SRC DST [--exclude PATTERN]",
            "Diffs folders via size-and-hash manifests; reports added, removed, changed.",
        ),
        (
            "treesize",
            "treesize [PATH]",
            "Shows total size and largest subfolders; never follows symlinks.",
        ),
        (
            "office",
            "office REGEX PATH...",
            "Searches text inside docx/xlsx/pptx/odt/ods by reading their internal XML.",
        ),
        (
            "detab",
            "detab PATH... [--tabs 4] [--in-place]",
            "Replaces tabs with spaces; skips binaries; optional atomic in-place write.",
        ),
        (
            "crlf",
            "crlf PATH... [--to lf|crlf] [--in-place]",
            "Normalizes line endings to LF or CRLF; skips binaries; atomic in-place write.",
        ),
        (
            "checksum",
            "checksum PATH... [--algo md5] [-o MANIFEST]",
            "Writes one hash per file; output includes an algo header for later verify.",
        ),
        (
            "verify",
            "verify MANIFEST",
            "Re-hashes manifest files; reports OK/MISMATCH/MISSING and fails on errors.",
        ),
        (
            "serve",
            "serve [PATH] [--host 0.0.0.0] [--port 8000] [--password PW | --auth]",
            "Opens a browser of the current folder; click/arrow to select, g generates a password, then Share.",
        ),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.current = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="columns"):
            with Vertical(id="tool-pane"):
                yield Static("  UTILITIES", classes="pane-title")
                yield ListView(id="tool-list")
            with Vertical(id="run-pane"):
                yield Static("  COMMAND", classes="pane-title")
                yield Static("", id="tool-hint")
                yield Static("", id="tool-info")
                yield Input(id="args", placeholder="paths and flags ...")
                with Horizontal(id="buttons"):
                    yield Button("Run", id="run", variant="success")
                    yield Button("Stop server", id="stop")
                    yield Button("Back", id="back")
                with VerticalScroll(id="output-scroll"):
                    yield Static("", id="output")
        yield Footer()

    async def on_mount(self) -> None:
        list_view = self.query_one("#tool-list", ListView)
        items = [
            ListItem(Static(name, classes="tool-name"))
            for name, _, _ in self.COMMANDS
        ]
        await list_view.extend(items)
        self._show_hint()
        proc = getattr(self.app, "server_process", None)
        if proc is not None and proc.poll() is None:
            self._set_output(self._server_output())
        else:
            self._set_output("Pick a utility; serve opens a file browser instead of typing paths.")
        list_view.index = 0
        list_view.focus()

    def on_screen_resume(self) -> None:
        proc = getattr(self.app, "server_process", None)
        if proc is not None and proc.poll() is None:
            self._set_output(self._server_output())
        else:
            self._set_output("Pick a utility; serve opens a file browser instead of typing paths.")

    @on(ListView.Selected, "#tool-list")
    def select_tool(self, event: ListView.Selected) -> None:
        self.current = event.index or 0
        self._show_hint()

    @on(ListView.Highlighted, "#tool-list")
    def highlight_tool(self, event: ListView.Highlighted) -> None:
        self.current = event.list_view.index or 0
        self._show_hint()

    def _show_hint(self) -> None:
        _name, hint, info = self.COMMANDS[self.current]
        self.query_one("#tool-hint", Static).update(f"dedupe {hint}")
        self.query_one("#tool-info", Static).update(info)


    def _set_output(self, value: str) -> None:
        self.query_one("#output", Static).update(value)

    def _server_output(self) -> str:
        url = getattr(self.app, "server_url", "")
        gate = (
            "Password required; browser will prompt."
            if getattr(self.app, "server_password", None)
            else "Open access; no password gate."
        )
        return f"Server still running at {url}\n{gate}\n\n" + _qr_block_text(url) + "\n\nPress x to stop it."

    @on(Button.Pressed, "#run")
    async def run_from_button(self) -> None:
        await self.action_run()

    @on(Button.Pressed, "#stop")
    def stop_from_button(self) -> None:
        self.action_stop_server()

    @on(Button.Pressed, "#back")
    def back_from_button(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.dismiss()

    async def action_run(self) -> None:
        command = self.COMMANDS[self.current][0]
        raw = self.query_one("#args", Input).value
        argv = [command, *shlex.split(raw)]
        if command == "serve":
            self.app.push_screen(ServerScreen(initial=raw))
            return
        output = self.query_one("#output", Static)
        output.update(f"Running: dedupe {' '.join(argv)} ...")
        script = str(Path(__file__).resolve())
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, script, *argv],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            output.update(f"Failed to run {command}: {exc}")
            return
        body = proc.stdout.strip()
        if proc.stderr:
            body = f"{body}\n{proc.stderr.strip()}" if body else proc.stderr.strip()
        output.update(f"$ dedupe {' '.join(argv)}\n[exit {proc.returncode}]\n{body}")

    def action_stop_server(self) -> None:
        proc = getattr(self.app, "server_process", None)
        if proc is None or proc.poll() is not None:
            self._set_output("No server is running.")
            return
        proc.terminate()
        self.app.server_process = None
        self._set_output("Server stopped.")

class BatchItem(ListItem):
    def __init__(self, manifest: Path, data: dict) -> None:
        super().__init__()
        self.manifest = manifest
        self.data = data

    def compose(self) -> ComposeResult:
        created = str(self.data.get("created", "?"))
        entries = self.data.get("entries", [])
        lines = Text()
        lines.append(f"{created}  ", style="cyan")
        lines.append(f"{len(entries)} file(s)  ", style="white")
        first = Path(entries[0]["original"]).parent if entries else ""
        lines.append(f"from {first} ...", style="dim")
        yield Static(lines, classes="batch-row")


class TrashScreen(Screen):
    TITLE = "Trash"
    CSS = """
    TrashScreen { background: #0d1117; }
    TrashScreen #columns { height: 1fr; padding: 1 2; }
    TrashScreen #list-pane, TrashScreen #detail-pane {
        border: solid #253244;
        background: #101822;
        padding: 1;
    }
    TrashScreen #list-pane { width: 42%; }
    TrashScreen #detail-pane { width: 1fr; }
    TrashScreen .pane-title { color: #7dd3fc; text-style: bold; height: 1; }
    TrashScreen ListItem { height: 1; padding: 0 1; }
    TrashScreen .batch-row, .entry-row {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    TrashScreen #batch-detail { color: #94a3b8; height: 1; }
    TrashScreen #buttons {
        height: 3;
        width: 100%;
        layout: grid;
        grid-size: 4 1;
    }
    TrashScreen #buttons > Button { width: 1fr; margin: 0; }
    TrashScreen #entries-scroll {
        border: solid #1f2d3d;
        height: 1fr;
        min-height: 3;
    }
    TrashScreen #entries { color: #c9d4e2; padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "restore_selected", "Restore selected"),
        Binding("l", "restore_latest", "Restore latest"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        self.restored = False
        with Horizontal(id="columns"):
            with Vertical(id="list-pane"):
                yield Static("  TRASH BATCHES", classes="pane-title")
                yield ListView(id="batches")
            with Vertical(id="detail-pane"):
                yield Static("  FILES IN BATCH", classes="pane-title")
                yield Static("", id="batch-detail")
                with VerticalScroll(id="entries-scroll"):
                    yield Static("", id="entries")
                with Grid(id="buttons"):
                    yield Button("Restore", id="restore-selected", variant="success")
                    yield Button("Latest", id="restore-latest")
                    yield Button("Refresh", id="refresh")
                    yield Button("Back", id="back")
        yield Footer()

    async def on_mount(self) -> None:
        await self.reload_batches()

    async def reload_batches(self) -> None:
        self.batches = trash_batches()
        view = self.query_one("#batches", ListView)
        await view.clear()
        await view.extend(
            BatchItem(manifest, data) for manifest, data in self.batches
        )
        if self.batches:
            view.index = len(self.batches) - 1
        else:
            self.query_one("#batch-detail", Static).update(
                "Trash is empty. Deleted files land in ~/.dedupe_trash."
            )
            self.query_one("#entries", Static).update("")

    def _current_batch(self) -> tuple[Path, dict] | None:
        index = self.query_one("#batches", ListView).index
        if index is None or not 0 <= index < len(getattr(self, "batches", [])):
            return None
        return self.batches[index]

    @on(ListView.Highlighted, "#batches")
    @on(ListView.Selected, "#batches")
    def show_batch(self, event: ListView.Highlighted) -> None:
        item = event.item if isinstance(event.item, BatchItem) else None
        if item is None:
            return
        entries = item.data.get("entries", [])
        self.query_one("#batch-detail", Static).update(
            f"{len(entries)} file(s) · deleted {item.data.get('created', '?')}"
        )
        body = Text()
        for entry in entries:
            body.append(f"{entry['original']}\n", style="white")
        self.query_one("#entries", Static).update(body)

    def _do_restore(self, manifest: Path | None) -> None:
        if manifest is None:
            self.notify("No batch selected", severity="warning")
            return
        self.restored = True

        def done(count: int) -> None:
            self.notify(f"Restored {count} file(s)")
            asyncio.create_task(self.reload_batches())

        asyncio.create_task(self._restore_async(manifest, done))

    async def _restore_async(self, manifest: Path, done) -> None:
        count = await asyncio.to_thread(restore_batch, manifest)
        done(count)

    @on(Button.Pressed, "#restore-selected")
    def restore_selected(self) -> None:
        batch = self._current_batch()
        self._do_restore(batch[0] if batch else None)

    @on(Button.Pressed, "#restore-latest")
    def restore_latest(self) -> None:
        if not getattr(self, "batches", []):
            self.notify("Trash is empty", severity="warning")
            return
        self._do_restore(self.batches[-1][0])

    def action_restore_selected(self) -> None:
        self.restore_selected()

    def action_restore_latest(self) -> None:
        self.restore_latest()

    async def action_refresh(self) -> None:
        await self.reload_batches()

    @on(Button.Pressed, "#refresh")
    def refresh_button(self) -> None:
        asyncio.create_task(self.reload_batches())

    @on(Button.Pressed, "#back")
    def back(self) -> None:
        self.dismiss()

    def action_back(self) -> None:
        self.dismiss()


# --------------------------------------------------------------------------- #
# TUI app                                                                     #
# --------------------------------------------------------------------------- #


class DedupeApp(App):
    TITLE = "dedupe"
    CSS = """
    Screen { background: #0d1117; color: #c9d4e2; }
    Header { background: #102033; color: #7dd3fc; }
    Footer { background: #102033; color: #94a3b8; }
    #topbar {
        height: 3;
        padding: 1 2;
        background: #111b26;
    }
    #stats { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #bar { width: 16; height: 1; }
    #body { padding: 1 2; }
    #left, #right {
        border: solid #253244;
        background: #101822;
        padding: 1;
        margin: 0 1 0 0;
    }
    #left { width: 42%; }
    #right { width: 1fr; }
    .pane-title { color: #7dd3fc; text-style: bold; height: 1; }
    OptionList {
        height: 1fr;
        border: none;
        padding: 0;
        background: #101822;
        color: #c9d4e2;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    OptionList:focus {
        border: none;
        background-tint: transparent;
    }
    OptionList > .option-list--option { padding: 0 1; }
    OptionList > .option-list--option-highlighted { background: #172a38; }
    OptionList:focus > .option-list--option-highlighted { background: #1e3a4a; }
    #hint { color: #68798b; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #actions {
        height: 3;
        padding: 0 2;
        background: #111b26;
    }
    #actions > Button { width: auto; min-width: 8; margin: 0 1 0 0; }
    #keeperbar {
        height: 3;
        padding: 0 1;
        layout: grid;
        grid-size: 3 1;
    }
    #keeperbar > Button { width: 1fr; margin: 0 1 0 0; }
    #keeperbar > Button.-keeper-active { border: tall #7ee787; color: #7ee787; }
    #filebar {
        height: 6;
        padding: 0 1;
        layout: grid;
        grid-size: 4 2;
        grid-rows: 3;
    }
    #filebar > Button { width: 1fr; margin: 0 1 0 0; }
    #hint.-hidden { display: none; }
    #filterbar {
        height: 3;
        padding: 0 1;
    }
    #filterbar > Input { width: 1fr; }
    #filterbar > Button { width: auto; min-width: 6; margin: 0 0 0 1; }
    #preview {
        border: solid #1f2d3d;
        background: #0b1219;
        height: 12;
        display: none;
        padding: 0 1;
    }
    #preview.-visible { display: block; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("s", "sort", "Sort"),
        Binding("t", "tools", "Tools"),
        Binding("v", "trash", "Trash"),
        Binding("f", "focus_filter", "Filter"),
        Binding("space", "toggle", "Mark", priority=True),
        Binding("a", "select_all", "Select all"),
        Binding("g", "cycle_keep", "Keep strategy"),
        Binding("d", "delete", "Delete marked", priority=True),
        Binding("o", "open_file", "Open file"),
        Binding("c", "copy_path", "Copy path"),
        Binding("i", "file_info", "Info"),
        Binding("p", "preview_file", "Preview"),
    ]

    KEEP_STRATEGIES = ("shortest", "oldest", "newest")

    def __init__(self, root: Path, keep: str, sort_key: str, min_size: int) -> None:
        super().__init__()
        self.root = root
        self.keep = keep
        self.sort_key = sort_key
        self.min_size = min_size
        self.groups: list[Group] = []
        self.shown: list[Group] | None = None
        self.current = -1
        self.scanned = 0
        self.file_index: int | None = None
        self.scan_state = ScanProgress("walking")
        self._scan_task: asyncio.Task[None] | None = None
        self._scan_cancel: threading.Event | None = None
        self.server_process: subprocess.Popen[str] | None = None
        self.server_url = ""
        self.server_password: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="topbar"):
            yield Static("", id="stats")
            yield ProgressBar(id="bar", show_percentage=False)
        with Horizontal(id="actions"):
            yield Button("Rescan", id="btn-rescan")
            yield Button(f"Sort: {self.sort_key}", id="btn-sort")
            yield Button("Select all", id="btn-select-all")
            yield Button("Delete marked", id="btn-delete", variant="error")
            yield Button("Trash", id="btn-trash")
            yield Button("Tools", id="btn-tools")
            yield Button("Quit", id="btn-quit")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("  DUPLICATE GROUPS", classes="pane-title")
                yield Horizontal(
                    Input(placeholder="filter groups by name…", id="filter"),
                    Button("Filter", id="btn-filter"),
                    Button("Clear", id="btn-filter-clear"),
                    id="filterbar",
                )
                yield OptionList(id="groups")
            with Vertical(id="right"):
                yield Static("  FILES IN GROUP", id="detail-title", classes="pane-title")
                yield OptionList(id="files")
                with Horizontal(id="keeperbar"):
                    yield Button("shortest", id="btn-keep-shortest")
                    yield Button("oldest", id="btn-keep-oldest")
                    yield Button("newest", id="btn-keep-newest")
                with Horizontal(id="filebar"):
                    yield Button("Mark", id="btn-mark")
                    yield Button("Keep this", id="btn-keep")
                    yield Button("Open", id="btn-open")
                    yield Button("Reveal", id="btn-reveal")
                    yield Button("Copy path", id="btn-copy")
                    yield Button("Info", id="btn-info")
                    yield Button("Preview", id="btn-preview")
                    yield Button("Hide info", id="btn-hide-info")
                yield Static("", id="preview")
                yield Static("", id="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#bar", ProgressBar).styles.display = "none"
        self._update_keeper_buttons()
        task = self.start_scan()
        await asyncio.wait({task}, timeout=0.15)
        self.query_one("#groups", OptionList).focus()

    def on_unmount(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()

    def selected_path(self) -> Path | None:
        if self.current < 0:
            return None
        files = self.groups[self.current].files
        index = self.file_index
        if index is None or not 0 <= index < len(files):
            return None
        return files[index]

    def _set_hint(self, text: Text | str) -> None:
        self.query_one("#hint", Static).update(text)

    def action_open_file(self) -> None:
        path = self.selected_path()
        if path is None:
            self.notify("No file selected", severity="warning")
            return
        opener = {
            "darwin": ["open"],
            "linux": ["xdg-open"],
            "windows": ["cmd", "/c", "start", ""],
        }.get(sys.platform)
        if opener is None:
            self.notify(f"No opener for {sys.platform}", severity="warning")
            return
        try:
            subprocess.Popen([*opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(f"Opened {path.name}")
        except OSError as error:
            self.notify(f"Open failed: {error}", severity="error")

    def action_reveal(self) -> None:
        path = self.selected_path()
        if path is None:
            self.notify("No file selected", severity="warning")
            return
        folder = str(path.parent)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", folder])
            self.notify(f"Revealed {path.parent}")
        except OSError as error:
            self.notify(f"Reveal failed: {error}", severity="error")

    def action_copy_path(self) -> None:
        path = self.selected_path()
        if path is None:
            self.notify("No file selected", severity="warning")
            return
        try:
            self.app.copy_to_clipboard(str(path))
            self._set_hint(f"  copied: {path}")
            self.notify("Full path copied to clipboard")
        except Exception as error:  # noqa: BLE001 - clipboard is best-effort
            self._set_hint(f"  {path}")
            self.notify(f"Copy failed: {error}", severity="error")

    def action_file_info(self) -> None:
        path = self.selected_path()
        if path is None:
            self.notify("No file selected", severity="warning")
            return
        try:
            info = path.stat()
        except OSError as error:
            self.notify(f"Info failed: {error}", severity="error")
            return
        text = Text()
        text.append(f"{path}\n", style="white")
        text.append(
            f"{human_bytes(info.st_size)} · modified "
            f"{datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M:%S}",
            style="dim",
        )
        self.query_one("#preview", Static).update(text)
        self.query_one("#preview").add_class("-visible")
        self.query_one("#hint").add_class("-hidden")

    async def action_preview_file(self) -> None:
        path = self.selected_path()
        if path is None:
            self.notify("No file selected", severity="warning")
            return
        body = await asyncio.to_thread(self._read_preview, path)
        self.query_one("#preview", Static).update(body)
        self.query_one("#preview").add_class("-visible")
        self.query_one("#hint").add_class("-hidden")

    @staticmethod
    def _read_preview(path: Path, lines: int = 20) -> Text:
        text = Text()
        try:
            with path.open("rb") as handle:
                head = handle.read(64 * 1024)
        except OSError as error:
            return Text(f"cannot read {path}: {error}", style="red")
        if b"\x00" in head:
            text.append(f"{path}\n", style="white")
            text.append("binary content — no text preview", style="dim")
            return text
        body = head.decode("utf-8", errors="replace").splitlines()
        text.append(f"{path}\n", style="white")
        for line in body[:lines]:
            text.append(line[:200] + "\n", style="white")
        if len(body) > lines:
            text.append(f"… ({len(body) - lines} more lines)", style="dim")
        return text

    def keep_file(self, index: int) -> None:
        if self.current < 0:
            return
        group = self.groups[self.current]
        if not 0 <= index < len(group.files):
            return
        group.keeper = group.files[index]
        group.marked.discard(group.keeper)
        self._refresh_file_rows(preserve_index=index)

    def action_cycle_keep(self) -> None:
        current = self.KEEP_STRATEGIES.index(self.keep) if self.keep in self.KEEP_STRATEGIES else -1
        self.set_keep_strategy(self.KEEP_STRATEGIES[(current + 1) % len(self.KEEP_STRATEGIES)])

    def set_keep_strategy(self, strategy: str) -> None:
        if strategy not in self.KEEP_STRATEGIES:
            return
        self.keep = strategy
        for group in self.groups:
            try:
                group.keeper = choose_keeper(
                    group.files, strategy, mtimes=group.mtimes or None
                )
                group.marked.discard(group.keeper)
            except OSError:
                continue
        self._update_keeper_buttons()
        self.notify(f"Keeper strategy: {strategy}")
        self._refresh_file_rows(preserve_index=self.file_index)

    def _update_keeper_buttons(self) -> None:
        for name in self.KEEP_STRATEGIES:
            button = self.query_one(f"#btn-keep-{name}", Button)
            if name == self.keep:
                button.add_class("-keeper-active")
            else:
                button.remove_class("-keeper-active")

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def _visible_groups(self) -> list[Group]:
        return self.shown if self.shown is not None else self.groups

    def apply_filter(self, pattern: str) -> None:
        pattern = pattern.strip().lower()
        if not pattern:
            return self.clear_filter()
        visible = [
            group
            for group in self.groups
            if any(pattern in str(path).lower() for path in group.files)
        ]
        asyncio.create_task(self._render_groups(visible))

    def clear_filter(self) -> None:
        self.query_one("#filter", Input).value = ""
        self.shown = None
        asyncio.create_task(self._render_groups(self.groups, filtered=False))

    async def _render_groups(
        self, groups: list[Group], *, filtered: bool = True
    ) -> None:
        self.shown = groups if filtered else None
        view = self.query_one("#groups", OptionList)
        view.clear_options()
        view.add_options(_group_row(group, self.root) for group in groups)
        if groups:
            view.highlighted = 0
            await self.show_group(groups[0].index, focus=False)
        else:
            self.current = -1
            self.file_index = None
            self.query_one("#detail-title", Static).update("  FILES IN GROUP")
            self.query_one("#files", OptionList).clear_options()
            self._set_hint("  no matching groups")

    # -- scanning -------------------------------------------------------- #

    def _progress(self, state: ScanProgress) -> None:
        self.scan_state = state

    def start_scan(self) -> asyncio.Task[None]:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
        cancel = threading.Event()
        self._scan_cancel = cancel
        task = asyncio.create_task(self.perform_scan(cancel))
        self._scan_task = task
        return task

    def _write_summary_stats(self) -> None:
        wasted = sum(group.wasted for group in self.groups)
        self._write_stats(
            Text("  root ")
            .append(self.root.name or str(self.root), style="bold cyan")
            .append(f"   files {self.scanned}", style="white")
            .append(f"   groups {len(self.groups)}", style="white")
            .append(f"   reclaimable {human_bytes(wasted)}", style="bold red")
        )

    async def perform_scan(self, cancel: threading.Event | None = None) -> None:
        if cancel is None:
            if self._scan_cancel is not None:
                self._scan_cancel.set()
            cancel = threading.Event()
            self._scan_cancel = cancel

        bar = self.query_one("#bar", ProgressBar)
        bar.styles.display = "block"
        self.scan_state = ScanProgress("walking")
        bar.total = None
        bar.progress = 0
        self.groups = []
        self.shown = None
        self.current = -1
        self.file_index = None
        self.query_one("#groups", OptionList).clear_options()
        self.query_one("#files", OptionList).clear_options()

        job = asyncio.create_task(
            asyncio.to_thread(
                scan, self.root, self.keep, self._progress, self.min_size, cancel
            )
        )
        spinner = "-\\|/"
        step = 0
        try:
            while not job.done():
                if cancel.is_set():
                    return
                state = self.scan_state
                if state.total:
                    bar.total = state.total
                    bar.progress = state.files
                else:
                    bar.total = None
                text = Text("  scanning ")
                text.append(f"[{spinner[step % 4]}] ", style="bold cyan")
                text.append(f"{state.label} ", style="white")
                if state.total:
                    text.append(f"{state.files}/{state.total} ", style="white")
                else:
                    text.append(f"{state.files} files ", style="white")
                if state.bytes_read:
                    text.append(f"· {human_bytes(state.bytes_read)} read ", style="dim")
                text.append(short_path(self.root, self.root.parent), style="dim")
                self._write_stats(text)
                await asyncio.sleep(0.08)
                step += 1
            result = await job
        except asyncio.CancelledError:
            cancel.set()
            return
        except Exception as error:  # noqa: BLE001 - surface scan failures in UI
            if not cancel.is_set():
                self.notify(f"Scan failed: {error}", severity="error")
                self._write_stats(Text("  scan failed"))
                bar.styles.display = "none"
            return

        if cancel.is_set():
            return

        bar.styles.display = "none"
        self.root = result.root
        self.groups = result.groups
        if self.sort_key == "name":
            self.groups.sort(key=lambda g: str(g.files[0]))
            for index, group in enumerate(self.groups):
                group.index = index
        self.scanned = result.scanned
        self._write_summary_stats()
        await self.rebuild_groups()
        self.query_one("#groups", OptionList).focus()

    def _write_stats(self, text: Text) -> None:
        self.query_one("#stats", Static).update(text)

    # -- list rendering -------------------------------------------------- #

    async def rebuild_groups(self) -> None:
        self.shown = None
        view = self.query_one("#groups", OptionList)
        view.clear_options()
        view.add_options(_group_row(group, self.root) for group in self.groups)
        if self.groups:
            view.highlighted = 0
            self.current = 0
            await self.show_group(0, focus=False)
        else:
            self.current = -1
            self.file_index = None
            self.query_one("#detail-title", Static).update("  FILES IN GROUP")
            self.query_one("#files", OptionList).clear_options()
            self._set_hint("  no duplicate files found")

    def _update_group_hint(self, group: Group) -> None:
        self.query_one("#hint").remove_class("-hidden")
        self.query_one("#hint", Static).update(
            Text("  ").append(
                f"{len(group.marked)} marked · space mark · d delete · buttons below: open / reveal / copy / info / preview",
                style="dim",
            )
        )

    def _refresh_file_rows(self, preserve_index: int | None = None) -> None:
        if not 0 <= self.current < len(self.groups):
            return
        group = self.groups[self.current]
        view = self.query_one("#files", OptionList)
        if view.option_count == len(group.files):
            for idx, path in enumerate(group.files):
                view.replace_option_prompt_at_index(idx, _file_row(group, path, self.root))
        else:
            view.clear_options()
            view.add_options(_file_row(group, path, self.root) for path in group.files)
        if group.files:
            idx = 0 if preserve_index is None else min(preserve_index, len(group.files) - 1)
            self.file_index = idx
            view.highlighted = idx
        self._update_group_hint(group)

    async def show_group(self, index: int, focus: bool = False) -> None:
        if not 0 <= index < len(self.groups):
            return
        self.current = index
        group = self.groups[index]
        self.file_index = 0
        title = self.query_one("#detail-title", Static)
        title.update(
            Text("  GROUP ")
            .append(f"{index + 1:02}", style="bold cyan")
            .append(f"   {len(group.files)} copies", style="white")
            .append(f"   {human_bytes(group.wasted)} wasted", style="bold red")
        )
        view = self.query_one("#files", OptionList)
        view.clear_options()
        view.add_options(_file_row(group, path, self.root) for path in group.files)
        if group.files:
            view.highlighted = 0
        self._update_group_hint(group)
        if focus:
            view.highlighted = 0
            view.focus()

    async def refresh_files(self) -> None:
        if self.current >= 0:
            self._refresh_file_rows(preserve_index=self.file_index)

    # -- events ---------------------------------------------------------- #

    @on(OptionList.OptionHighlighted, "#groups")
    async def highlight_group(self, event: OptionList.OptionHighlighted) -> None:
        visible = self._visible_groups()
        pos = event.option_index
        if 0 <= pos < len(visible):
            target = visible[pos].index
            if target != self.current:
                await self.show_group(target)

    @on(OptionList.OptionSelected, "#groups")
    async def select_group(self, event: OptionList.OptionSelected) -> None:
        visible = self._visible_groups()
        pos = event.option_index
        if 0 <= pos < len(visible):
            await self.show_group(visible[pos].index, focus=True)

    @on(OptionList.OptionSelected, "#files")
    async def select_file(self, event: OptionList.OptionSelected) -> None:
        self.file_index = event.option_index
        self.toggle_file(event.option_index)

    @on(OptionList.OptionHighlighted, "#files")
    def track_file(self, event: OptionList.OptionHighlighted) -> None:
        self.file_index = event.option_index

    @on(Input.Submitted, "#filter")
    async def filter_submitted(self, event: Input.Submitted) -> None:
        self.apply_filter(event.value)
        self.query_one("#groups", OptionList).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        handlers = {
            "btn-rescan": self.action_rescan,
            "btn-sort": self.action_sort,
            "btn-select-all": self.action_select_all,
            "btn-delete": self.action_delete,
            "btn-trash": self.action_trash,
            "btn-tools": self.action_tools,
            "btn-quit": self.action_quit,
            "btn-filter": self._filter_from_input,
            "btn-filter-clear": self.clear_filter,
            "btn-mark": self._mark_selected,
            "btn-keep": self._keep_selected,
            "btn-open": self.action_open_file,
            "btn-reveal": self.action_reveal,
            "btn-copy": self.action_copy_path,
            "btn-info": self.action_file_info,
            "btn-preview": lambda: asyncio.create_task(self.action_preview_file()),
            "btn-hide-info": self._hide_info,
            "btn-keep-shortest": lambda: self.set_keep_strategy("shortest"),
            "btn-keep-oldest": lambda: self.set_keep_strategy("oldest"),
            "btn-keep-newest": lambda: self.set_keep_strategy("newest"),
        }
        handler = handlers.get(button_id)
        if handler is not None:
            result = handler()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    def _hide_info(self) -> None:
        self.query_one("#preview").remove_class("-visible")
        self.query_one("#hint").remove_class("-hidden")

    def _filter_from_input(self) -> None:
        self.apply_filter(self.query_one("#filter", Input).value)

    def _mark_selected(self) -> None:
        index = self.file_index
        if index is None:
            self.notify("No file selected", severity="warning")
            return
        self.toggle_file(index)

    def _keep_selected(self) -> None:
        index = self.file_index
        if index is None:
            self.notify("No file selected", severity="warning")
            return
        self.keep_file(index)

    def action_trash(self) -> None:
        def closed(_screen=None) -> None:
            screen = getattr(self, "_trash_screen", None)
            if screen is not None and getattr(screen, "restored", False):
                self.start_scan()

        self._trash_screen = TrashScreen()
        self.push_screen(self._trash_screen, closed)

    # -- actions --------------------------------------------------------- #

    def toggle_file(self, index: int) -> None:
        if self.current < 0 or not 0 <= index < len(self.groups[self.current].files):
            return
        group = self.groups[self.current]
        path = group.files[index]
        if path == group.keeper:
            self.notify("The keeper cannot be marked", severity="warning")
            return
        if path in group.marked:
            group.marked.discard(path)
        else:
            group.marked.add(path)
        view = self.query_one("#files", OptionList)
        if 0 <= index < view.option_count:
            view.replace_option_prompt_at_index(index, _file_row(group, path, self.root))
            self._update_group_hint(group)
        else:
            self._refresh_file_rows(preserve_index=index)

    def action_toggle(self) -> None:
        if not self.groups or self.current < 0:
            return
        files = self.query_one("#files", OptionList)
        if files.has_focus:
            self.toggle_file(files.highlighted or 0)
            return
        group = self.groups[self.current]
        if group.marked:
            group.marked.clear()
        else:
            group.marked.update(path for path in group.files if path != group.keeper)
        self._refresh_file_rows(preserve_index=self.file_index)

    def action_select_all(self) -> None:
        if not self.groups:
            return
        for group in self.groups:
            group.marked.update(
                path for path in group.files if path != group.keeper
            )
        count = sum(len(group.marked) for group in self.groups)
        self.notify(f"Marked all {count} duplicate(s)")
        self._refresh_file_rows(preserve_index=self.file_index)

    def action_sort(self) -> None:
        key = "name" if self.sort_key == "waste" else "waste"
        self.sort_key = key
        self.groups.sort(
            key=lambda g: str(g.files[0]) if key == "name" else (-g.wasted, str(g.files[0]))
        )
        for index, group in enumerate(self.groups):
            group.index = index
        self.notify(f"Sorted by {'name' if key == 'name' else 'wasted space'}")
        self.query_one("#btn-sort", Button).label = f"Sort: {key}"
        pattern = self.query_one("#filter", Input).value.strip()
        if pattern:
            self.apply_filter(pattern)
        else:
            asyncio.create_task(self.rebuild_groups())

    def action_delete(self) -> None:
        paths = [
            path
            for group in self.groups
            for path in group.marked
            if path != group.keeper
        ]
        if not paths:
            self.notify("No files marked for deletion", severity="warning")
            return
        wasted = sum(
            group.size
            for group in self.groups
            for path in group.marked
            if path != group.keeper
        )

        def confirmed(answer: bool) -> None:
            if answer:
                asyncio.create_task(self.finish_delete(paths))

        self.push_screen(ConfirmScreen(len(paths), wasted), confirmed)

    async def finish_delete(self, paths: list[Path]) -> None:
        try:
            await asyncio.to_thread(trash_files, paths)
        except OSError as error:
            self.notify(f"Trash failed: {error}", severity="error")
            return
        self.notify(f"Moved {len(paths)} files to trash")
        removed = set(paths)
        remaining: list[Group] = []
        for group in self.groups:
            kept_files = [path for path in group.files if path not in removed]
            if len(kept_files) < 2:
                continue
            group.files = kept_files
            group.marked.difference_update(removed)
            for path in removed:
                group.mtimes.pop(path, None)
            if group.keeper not in kept_files:
                group.keeper = choose_keeper(
                    kept_files, self.keep, mtimes=group.mtimes or None
                )
                group.marked.discard(group.keeper)
            remaining.append(group)
        self.groups = remaining
        for index, group in enumerate(self.groups):
            group.index = index
        self.scanned = max(0, self.scanned - len(paths))
        self._write_summary_stats()
        pattern = self.query_one("#filter", Input).value.strip()
        if pattern:
            self.apply_filter(pattern)
        else:
            await self.rebuild_groups()

    async def action_rescan(self) -> None:
        self.start_scan()

    def action_tools(self) -> None:
        self.push_screen(ToolsScreen())

# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _cli_progress(state: ScanProgress) -> None:
    if not sys.stderr.isatty():
        return
    if state.phase == "walking":
        line = f"  walking {state.files} files"
    elif state.phase in ("sampling", "hashing"):
        count = f"{state.files}/{state.total}" if state.total else str(state.files)
        line = (
            f"  {state.label} {count} · {human_bytes(state.bytes_read)} read"
        )
    else:
        return
    print(f"\r{line:<74}", end="", file=sys.stderr, flush=True)



def headless_report(result: ScanResult) -> None:
    print(f"root: {result.root}")
    print(f"files scanned: {result.scanned}")
    print(f"duplicate groups: {len(result.groups)}")
    print(f"reclaimable: {human_bytes(result.wasted)}\n")
    for group in result.groups:
        print(f"[{group.index + 1:02}] {human_bytes(group.wasted)} reclaimable")
        for path in group.files:
            tag = "keep" if path == group.keeper else "drop"
            print(f"     {tag}  {path}")
        print()


TOOL_COMMANDS = {
    "zip",
    "unzip",
    "arclist",
    "backup",
    "compare",
    "treesize",
    "office",
    "detab",
    "crlf",
    "checksum",
    "verify",
    "serve",
}


def build_tool_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedupe <tool>",
        description="File utilities; run 'dedupe <tool> --help' for details.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tool = sub.add_parser("zip", help="create a zip archive")
    tool.add_argument("paths", nargs="+")
    tool.add_argument("-o", "--output", required=True)
    tool.add_argument("--exclude", action="append", default=[], help="skip matching paths (repeatable)")

    tool = sub.add_parser("unzip", help="extract a zip or tar archive")
    tool.add_argument("archive")
    tool.add_argument("-d", "--dest")

    tool = sub.add_parser("arclist", help="list archive contents, including nested archives")
    tool.add_argument("paths", nargs="+")
    tool.add_argument("--max-depth", type=int, default=6)

    tool = sub.add_parser("backup", help="copy a tree with exclusions")
    tool.add_argument("src")
    tool.add_argument("dst")
    tool.add_argument("--exclude", action="append", default=[])
    tool.add_argument("--dry-run", action="store_true")

    tool = sub.add_parser("compare", help="list added, removed and changed files")
    tool.add_argument("src")
    tool.add_argument("dst")
    tool.add_argument("--exclude", action="append", default=[])

    tool = sub.add_parser("treesize", help="show cumulative sizes of subfolders")
    tool.add_argument("path", nargs="?", default=".")

    tool = sub.add_parser("office", help="search text in office documents")
    tool.add_argument("pattern")
    tool.add_argument("paths", nargs="+")

    tool = sub.add_parser("detab", help="replace tabs with spaces")
    tool.add_argument("paths", nargs="+")
    tool.add_argument("--tabs", type=int, default=4)
    tool.add_argument("--in-place", action="store_true")

    tool = sub.add_parser("crlf", help="convert line endings")
    tool.add_argument("paths", nargs="+")
    tool.add_argument("--to", choices=("lf", "crlf"), default="lf")
    tool.add_argument("--in-place", action="store_true")

    tool = sub.add_parser("checksum", help="write a checksum list")
    tool.add_argument("paths", nargs="+")
    tool.add_argument("--algo", default="md5")
    tool.add_argument("-o", "--output")

    tool = sub.add_parser("verify", help="verify a checksum list")
    tool.add_argument("manifest")

    tool = sub.add_parser("serve", help="share a file or folder over HTTP with a QR link")
    tool.add_argument("path", nargs="?", default=".")
    tool.add_argument("--host", default="0.0.0.0")
    tool.add_argument("--port", type=int, default=8000)
    tool.add_argument("--png")
    auth = tool.add_mutually_exclusive_group()
    auth.add_argument(
        "--password",
        metavar="PASSWORD",
        help="require this password (or set DEDUPE_SHARE_PASSWORD)",
    )
    auth.add_argument(
        "--auth", action="store_true", help="generate and print a random password"
    )
    return parser


def run_tool(argv: list[str]) -> int:
    args = build_tool_parser().parse_args(argv)
    from dtools import archives, checksum, compare, office, server, textops

    handlers = {
        "zip": archives.cmd_zip,
        "unzip": archives.cmd_unzip,
        "arclist": archives.cmd_arclist,
        "backup": compare.cmd_backup,
        "compare": compare.cmd_compare,
        "treesize": compare.cmd_treesize,
        "office": office.cmd_office,
        "detab": textops.cmd_detab,
        "crlf": textops.cmd_crlf,
        "checksum": checksum.cmd_checksum,
        "verify": checksum.cmd_verify,
        "serve": server.cmd_serve,
    }
    return handlers[args.command](args)


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in TOOL_COMMANDS:
        return run_tool(raw)
    parser = argparse.ArgumentParser(
        prog="dedupe",
        description="Find duplicate files and remove them safely through an interactive TUI.",
        epilog="Tools: " + ", ".join(sorted(TOOL_COMMANDS)) + "\nRun 'dedupe <tool> --help' for details.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", nargs="?", default=".", help="directory to scan")
    parser.add_argument("--scan", action="store_true", help="headless report; no changes")
    parser.add_argument("--restore", action="store_true", help="restore the last trash batch")
    parser.add_argument(
        "--keep",
        choices=("shortest", "oldest", "newest"),
        default="shortest",
        help="which duplicate to keep (default: shortest path)",
    )
    parser.add_argument(
        "--sort",
        choices=("waste", "name"),
        default="waste",
        help="group ordering (default: wasted space)",
    )
    parser.add_argument(
        "--min-size", type=int, default=0, help="ignore files smaller than N bytes"
    )
    args = parser.parse_args(raw)

    if args.restore:
        count = restore_last_batch()
        print(f"Restored {count} file(s)." if count else "Nothing to restore.")
        return 0

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"dedupe: no such path: {root}", file=sys.stderr)
        return 1

    if args.scan:
        result = scan(root, args.keep, _cli_progress, args.min_size)
        if sys.stderr.isatty():
            print("", file=sys.stderr, flush=True)
        headless_report(result)
        return 0

    DedupeApp(root, args.keep, args.sort, args.min_size).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
