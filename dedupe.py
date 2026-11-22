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
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import shlex
import socket
import subprocess
import os
import secrets
import shutil
import stat
import sys
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from dtools.common import human_bytes, iter_files

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    ProgressBar,
    Static,
)

TRASH_ROOT = Path.home() / ".dedupe_trash"
HASH_CHUNK = 4 * 1024 * 1024
SAMPLE_BYTES = 64 * 1024
HASH_WORKERS = min(8, os.cpu_count() or 4)


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


def digest_file(path: Path) -> tuple[bytes, int] | None:
    digest = hashlib.blake2b(digest_size=16)
    read = 0
    try:
        with path.open("rb", buffering=0) as handle:
            while chunk := handle.read(HASH_CHUNK):
                digest.update(chunk)
                read += len(chunk)
    except OSError:
        return None
    return digest.digest(), read


def quick_key(path: Path, size: int) -> bytes | None:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(size.to_bytes(8, "big"))
    try:
        with path.open("rb", buffering=0) as handle:
            digest.update(handle.read(SAMPLE_BYTES))
            if size > SAMPLE_BYTES * 2:
                handle.seek(size - SAMPLE_BYTES)
                digest.update(handle.read(SAMPLE_BYTES))
    except OSError:
        return None
    return digest.digest()

def choose_keeper(files: list[Path], strategy: str) -> Path:
    if strategy == "oldest":
        return min(files, key=lambda p: (p.stat().st_mtime, len(p.parts), str(p)))
    if strategy == "newest":
        return max(files, key=lambda p: (p.stat().st_mtime, -len(p.parts), str(p)))
    return min(files, key=lambda p: (len(p.parts), len(str(p)), str(p)))


def scan(
    root: Path,
    keep: str = "shortest",
    progress: Callable[[ScanProgress], None] | None = None,
    min_size: int = 0,
) -> ScanResult:
    root = root.expanduser().resolve()
    last_progress: ScanProgress | None = None

    def report(state: ScanProgress) -> None:
        nonlocal last_progress
        if progress is not None and state != last_progress:
            progress(state)
            last_progress = state
    by_size: dict[int, list[Path]] = {}
    scanned = 0
    last_report = 0

    for path in iter_files(root):
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size < min_size:
            continue
        scanned += 1
        if info.st_size == 0:
            continue
        by_size.setdefault(info.st_size, []).append(path)
        if scanned - last_report >= 100:
            last_report = scanned
            report(ScanProgress("walking", files=scanned))
    report(ScanProgress("walking", files=scanned))

    candidates: list[tuple[int, Path]] = []
    sampling_total = sum(len(paths) for paths in by_size.values() if len(paths) > 1)
    sampling_done = 0
    sampled_bytes = 0
    last_report = 0
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        quick: dict[bytes, list[Path]] = {}
        for path in paths:
            key = quick_key(path, size)
            if key is not None:
                quick.setdefault(key, []).append(path)
                sampled_bytes += min(size, SAMPLE_BYTES * 2)
        for bucket in quick.values():
            if len(bucket) > 1:
                candidates.extend((size, path) for path in bucket)
        sampling_done += len(paths)
        if sampling_done - last_report >= 100:
            last_report = sampling_done
            report(
                ScanProgress(
                    "sampling", sampling_done, sampling_total, sampled_bytes
                )
            )
    report(ScanProgress("sampling", sampling_done, sampling_total, sampled_bytes))

    full: dict[tuple[int, bytes], list[Path]] = {}
    hash_total = len(candidates)
    completed = 0
    hashed_bytes = 0
    last_count = 0
    last_bytes = 0
    count_step = max(1, hash_total // 100)
    byte_step = 32 * 1024 * 1024
    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
        futures = {
            pool.submit(digest_file, path): (size, path)
            for size, path in candidates
        }
        for future in as_completed(futures):
            size, path = futures[future]
            try:
                result = future.result()
            except OSError:
                continue
            if result is None:
                continue
            digest, read = result
            full.setdefault((size, digest), []).append(path)
            completed += 1
            hashed_bytes += read
            if completed - last_count >= count_step or hashed_bytes - last_bytes >= byte_step:
                last_count = completed
                last_bytes = hashed_bytes
                report(
                    ScanProgress("hashing", completed, hash_total, hashed_bytes)
                )
    report(ScanProgress("hashing", completed, hash_total, hashed_bytes))

    groups: list[Group] = []
    for (size, _digest), bucket in full.items():
        if len(bucket) < 2:
            continue
        groups.append(
            Group(
                index=len(groups),
                files=sorted(bucket, key=str),
                size=size,
                keeper=choose_keeper(bucket, keep),
            )
        )
    report(ScanProgress("grouping", len(groups), len(groups), hashed_bytes))

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

    for path in sorted(paths, key=str):
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
        save()


def restore_last_batch() -> int:
    if not TRASH_ROOT.exists():
        return 0
    manifests = sorted(
        TRASH_ROOT.glob("*/manifest.json"),
        key=lambda path: json.loads(path.read_text())["created"],
    )
    if not manifests:
        return 0

    manifest_path = manifests[-1]
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


# --------------------------------------------------------------------------- #
# TUI widgets                                                                 #
# --------------------------------------------------------------------------- #


def short_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) if path != root else path.name
    except ValueError:
        return str(path)


class GroupItem(ListItem):
    def __init__(self, group: Group, root: Path) -> None:
        super().__init__()
        self.group = group
        self.root = root

    def compose(self) -> ComposeResult:
        row = Text()
        row.append(f"{self.group.index + 1:02}  ", style="dim")
        row.append(f"{human_bytes(self.group.wasted):>9}", style="bold red")
        row.append(f"  {len(self.group.files)}x ", style="cyan")
        row.append(short_path(self.group.files[0], self.root), style="white")
        yield Static(row, classes="group-row")


class FileItem(ListItem):
    def __init__(self, group: Group, path: Path, root: Path) -> None:
        super().__init__()
        self.group = group
        self.path = path
        self.root = root

    def compose(self) -> ComposeResult:
        marked = self.path in self.group.marked
        row = Text()
        row.append("[x] " if marked else "[ ] ", style="green" if marked else "dim")
        row.append("keep " if self.path == self.group.keeper else "drop ", style="dim")
        row.append(short_path(self.path, self.root), style="white")
        yield Static(row, classes="file-row")


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
    ServerScreen ListItem { height: 1; padding: 0 1; }
    ServerScreen .entry-name { height: 1; text-wrap: nowrap; }
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
        Binding("enter", "activate", "Open/Select", priority=True),
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
        self.selected: Path | None = None
        self.server_url = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="columns"):
            with Vertical(id="browser"):
                yield Static("  FILES IN PWD", classes="pane-title")
                yield Static("", id="cwd")
                yield ListView(id="entries")
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
        self.query_one("#entries", ListView).focus()
        proc = getattr(self.app, "server_process", None)
        if proc is not None and proc.poll() is None:
            url = getattr(self.app, "server_url", "")
            self._show_server(url)
            self._set_auth(getattr(self.app, "server_password", None), "running")
            self._set_status("Server is running; press x to stop.")
        else:
            self._set_selection(None)
            self._set_auth(None)
            self._set_status("Click a file or use arrows + Enter.")

    async def reload_entries(self) -> None:
        view = self.query_one("#entries", ListView)
        await view.clear()
        self.entries = []
        if self.root.parent != self.root:
            self.entries.append(self.root.parent)
        try:
            children = sorted(
                self.root.iterdir(), key=lambda path: (path.is_dir(), path.name.lower())
            )
        except OSError as error:
            self._set_status(f"Cannot read {self.root}: {error}")
            return
        self.entries.extend(path for path in children if not path.is_symlink())

        rows: list[ListItem] = []
        for path in self.entries:
            if path == self.root.parent:
                row = Text("..", style="bold cyan")
            elif path.is_dir():
                row = Text("dir", style="bold cyan").append(f"  {path.name}")
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                row = Text("file", style="dim").append(
                    f" {human_bytes(size):>8}  {path.name}"
                )
            rows.append(ListItem(Static(row, classes="entry-name")))
        await view.extend(rows)
        self.query_one("#cwd", Static).update(str(self.root))
        if self.entries:
            view.index = 0

    @on(ListView.Highlighted, "#entries")
    def preview_entry(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self.entries):
            self._set_selection(self.entries[index])

    @on(ListView.Selected, "#entries")
    async def activate_entry(self, event: ListView.Selected) -> None:
        index = event.index
        if index is None or not 0 <= index < len(self.entries):
            return
        path = self.entries[index]
        if path == self.root.parent:
            self.root = path
            await self.reload_entries()
            self._set_status(f"Opened {path}; press u to go up.")
        elif path.is_dir():
            self.root = path
            await self.reload_entries()
            self._set_status(f"Opened {path.name}; click a file to select it.")
        else:
            self.selected = path
            self._set_selection(path)
            self._set_status(f"Selected {path.name}; press s or click Share. Flags are optional.")

    def _set_selection(self, path: Path | None) -> None:
        target = path or self.root
        kind = "folder" if target.is_dir() else "file"
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
    ListView { background: #101822; color: #c9d4e2; }
    ListItem { padding: 0 1; }
    ListItem:focus > Static { background: #1e3a4a; }
    #hint { color: #68798b; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .group-row, .file-row {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("s", "sort", "Sort"),
        Binding("t", "tools", "Tools"),
        Binding("space", "toggle", "Mark", priority=True),
        Binding("a", "select_all", "Select all"),
        Binding("d", "delete", "Delete marked", priority=True),
    ]

    def __init__(self, root: Path, keep: str, sort_key: str, min_size: int) -> None:
        super().__init__()
        self.root = root
        self.keep = keep
        self.sort_key = sort_key
        self.min_size = min_size
        self.groups: list[Group] = []
        self.current = -1
        self.scanned = 0
        self.scan_state = ScanProgress("walking")
        self.server_process: subprocess.Popen[str] | None = None
        self.server_url = ""
        self.server_password: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="topbar"):
            yield Static("", id="stats")
            yield ProgressBar(id="bar", show_percentage=False)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("  DUPLICATE GROUPS", classes="pane-title")
                yield ListView(id="groups")
            with Vertical(id="right"):
                yield Static("  FILES IN GROUP", id="detail-title", classes="pane-title")
                yield ListView(id="files")
                yield Static("", id="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#bar", ProgressBar).styles.display = "none"
        await self.perform_scan()
        self.query_one("#groups", ListView).focus()

    # -- scanning -------------------------------------------------------- #

    def _progress(self, state: ScanProgress) -> None:
        self.call_from_thread(self._set_scan_state, state)

    def _set_scan_state(self, state: ScanProgress) -> None:
        self.scan_state = state

    async def perform_scan(self) -> None:
        bar = self.query_one("#bar", ProgressBar)
        bar.styles.display = "block"
        self.scan_state = ScanProgress("walking")
        bar.total = None
        bar.progress = 0
        self.groups = []
        self.current = -1
        await self.query_one("#groups", ListView).clear()
        await self.query_one("#files", ListView).clear()

        job = asyncio.create_task(
            asyncio.to_thread(
                scan, self.root, self.keep, self._progress, self.min_size
            )
        )
        spinner = "-\\|/"
        step = 0
        while not job.done():
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
        try:
            result = await job
        except Exception as error:  # noqa: BLE001 - surface scan failures in UI
            self.notify(f"Scan failed: {error}", severity="error")
            self._write_stats(Text("  scan failed"))
            bar.styles.display = "none"
            return

        bar.styles.display = "none"
        self.root = result.root
        self.groups = result.groups
        self.scanned = result.scanned
        self._write_stats(
            Text("  root ")
            .append(result.root.name or str(result.root), style="bold cyan")
            .append(f"   files {result.scanned}", style="white")
            .append(f"   groups {len(result.groups)}", style="white")
            .append(f"   reclaimable {human_bytes(result.wasted)}", style="bold red")
        )
        await self.rebuild_groups()
        self.query_one("#groups", ListView).focus()

    def _write_stats(self, text: Text) -> None:
        self.query_one("#stats", Static).update(text)

    # -- list rendering -------------------------------------------------- #

    async def rebuild_groups(self) -> None:
        view = self.query_one("#groups", ListView)
        await view.clear()
        await view.extend(GroupItem(group, self.root) for group in self.groups)
        if self.groups:
            view.index = 0
            self.current = 0
            await self.show_group(0, focus=False)

    async def show_group(self, index: int, focus: bool = False) -> None:
        if not 0 <= index < len(self.groups):
            return
        self.current = index
        group = self.groups[index]
        title = self.query_one("#detail-title", Static)
        title.update(
            Text("  GROUP ")
            .append(f"{index + 1:02}", style="bold cyan")
            .append(f"   {len(group.files)} copies", style="white")
            .append(f"   {human_bytes(group.wasted)} wasted", style="bold red")
        )
        view = self.query_one("#files", ListView)
        await view.clear()
        await view.extend(
            FileItem(group, path, self.root)
            for path in group.files
        )
        self.query_one("#hint", Static).update(
            Text("  ").append(
                f"{len(group.marked)} marked · space mark · d delete · restore: dedupe --restore",
                style="dim",
            )
        )
        if focus:
            view.index = 0
            view.focus()

    async def refresh_files(self) -> None:
        if self.current >= 0:
            await self.show_group(self.current)

    # -- events ---------------------------------------------------------- #

    @on(ListView.Highlighted, "#groups")
    async def highlight_group(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and index != self.current:
            await self.show_group(index)

    @on(ListView.Selected, "#groups")
    async def select_group(self, event: ListView.Selected) -> None:
        await self.show_group(event.index, focus=True)

    @on(ListView.Selected, "#files")
    async def select_file(self, event: ListView.Selected) -> None:
        self.toggle_file(event.index)

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
        asyncio.create_task(self.refresh_files())

    def action_toggle(self) -> None:
        if not self.groups:
            return
        files = self.query_one("#files", ListView)
        if files.has_focus:
            self.toggle_file(files.index or 0)
            return
        group = self.groups[self.current]
        if group.marked:
            group.marked.clear()
        else:
            group.marked.update(path for path in group.files if path != group.keeper)
        asyncio.create_task(self.refresh_files())

    def action_select_all(self) -> None:
        if not self.groups:
            return
        for group in self.groups:
            group.marked.update(
                path for path in group.files if path != group.keeper
            )
        count = sum(len(group.marked) for group in self.groups)
        self.notify(f"Marked all {count} duplicate(s)")
        asyncio.create_task(self.refresh_files())

    def action_sort(self) -> None:
        key = "name" if self.sort_key == "waste" else "waste"
        self.sort_key = key
        self.groups.sort(
            key=lambda g: str(g.files[0]) if key == "name" else (-g.wasted, str(g.files[0]))
        )
        for index, group in enumerate(self.groups):
            group.index = index
        self.notify(f"Sorted by {'name' if key == 'name' else 'wasted space'}")
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
        wasted = sum(p.stat().st_size for p in paths if p.exists())

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
        await self.perform_scan()

    async def action_rescan(self) -> None:
        await self.perform_scan()

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
