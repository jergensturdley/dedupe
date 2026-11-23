"""Regression and correctness tests for fast scanning and virtualized TUI."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dedupe as dedupe_mod
from dedupe import DedupeApp, ServerScreen, ScanProgress, ScanResult, scan
from dtools.common import iter_file_stats, iter_files
from dtools.compare import cmd_treesize
from textual.widgets import Input, OptionList


def test_iter_files_and_stats_skip_symlinks(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    real = sub / "photo.jpg"
    real.write_bytes(b"123456")
    (tmp_path / "link_file.jpg").symlink_to(real)
    (tmp_path / "link_dir").symlink_to(sub)

    found = list(iter_files(tmp_path))
    assert found == [real]

    stats = list(iter_file_stats(tmp_path))
    assert len(stats) == 1
    assert Path(stats[0][0]) == real
    assert stats[0][1].st_size == 6


def test_scan_small_and_large_files_including_middle_diff(tmp_path):
    # Small duplicates (<= SAMPLE_BYTES * 2 -> hashed in one pass during sampling)
    small_data = b"S" * 1024
    (tmp_path / "s1.jpg").write_bytes(small_data)
    (tmp_path / "s2.jpg").write_bytes(small_data)

    # Large duplicates (> SAMPLE_BYTES * 2 -> sampled then full hashed)
    head = b"H" * dedupe_mod.SAMPLE_BYTES
    tail = b"T" * dedupe_mod.SAMPLE_BYTES
    mid_a = b"A" * 4096
    mid_b = b"B" * 4096
    (tmp_path / "l1.jpg").write_bytes(head + mid_a + tail)
    (tmp_path / "l2.jpg").write_bytes(head + mid_a + tail)
    # Same size, head, and tail as l1/l2, but different middle -> must NOT group with l1/l2
    (tmp_path / "l_diff.jpg").write_bytes(head + mid_b + tail)

    result = scan(tmp_path, keep="shortest")
    assert result.scanned == 5
    assert len(result.groups) == 2

    sizes = sorted(g.size for g in result.groups)
    assert sizes == [1024, len(head) + len(mid_a) + len(tail)]
    for group in result.groups:
        names = {p.name for p in group.files}
        assert "l_diff.jpg" not in names
        assert set(group.mtimes.keys()) == set(group.files)


def test_scan_cancellation(tmp_path):
    for i in range(50):
        (tmp_path / f"f_{i}.bin").write_bytes(b"x" * 2048)
    cancel = threading.Event()
    cancel.set()
    result = scan(tmp_path, cancel=cancel)
    assert result.groups == []


def test_trash_files_batch_manifest(tmp_path, monkeypatch):
    trash = tmp_path / "trash"
    monkeypatch.setattr(dedupe_mod, "TRASH_ROOT", trash)
    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for i in range(260):
        p = src / f"img_{i:03d}.jpg"
        p.write_bytes(b"img")
        paths.append(p)

    dedupe_mod.trash_files(paths)
    batches = dedupe_mod.trash_batches()
    assert len(batches) == 1
    assert len(batches[0][1]["entries"]) == 260
    assert all(not p.exists() for p in paths)


@pytest.mark.asyncio
async def test_tui_nonblocking_mount_on_slow_scan(tmp_path, monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()

    def slow_scan(root, keep="shortest", progress=None, min_size=0, cancel=None):
        started.set()
        for i in range(30):
            if cancel is not None and cancel.is_set():
                cancelled.set()
                return ScanResult(root=root, scanned=i * 10, groups=[])
            if progress is not None:
                progress(ScanProgress("walking", files=i * 10))
            time.sleep(0.05)
        return ScanResult(root=root, scanned=300, groups=[])

    monkeypatch.setattr(dedupe_mod, "scan", slow_scan)
    app = DedupeApp(tmp_path, "shortest", "waste", 0)
    t0 = time.perf_counter()
    async with app.run_test() as pilot:
        elapsed = time.perf_counter() - t0
        assert started.is_set()
        assert elapsed < 0.8
        assert app._scan_task is not None and not app._scan_task.done()
        await pilot.pause(0.05)
    # Exiting app triggers on_unmount -> cancels background scan promptly.
    assert cancelled.wait(timeout=1.0)


@pytest.mark.asyncio
async def test_filtered_group_highlight_maps_to_correct_group(tmp_path):
    (tmp_path / "alpha_1.jpg").write_bytes(b"A" * 4096)
    (tmp_path / "alpha_2.jpg").write_bytes(b"A" * 4096)
    (tmp_path / "zeta_1.jpg").write_bytes(b"Z" * 1024)
    (tmp_path / "zeta_2.jpg").write_bytes(b"Z" * 1024)

    app = DedupeApp(tmp_path, "shortest", "waste", 0)
    async with app.run_test() as pilot:
        assert len(app.groups) == 2
        # alpha is group 0 (larger waste), zeta is group 1
        assert "alpha" in app.groups[0].files[0].name
        assert "zeta" in app.groups[1].files[0].name

        app.query_one("#filter", Input).value = "zeta"
        app._filter_from_input()
        await pilot.pause()

        assert app.shown is not None and len(app.shown) == 1
        assert app.current == 1
        assert "zeta" in app.selected_path().name


@pytest.mark.asyncio
async def test_server_screen_lists_and_navigates_with_option_list(tmp_path, monkeypatch):
    sub = tmp_path / "album"
    sub.mkdir()
    photo = sub / "pic.jpg"
    photo.write_bytes(b"JPEG")
    (tmp_path / "sym.jpg").symlink_to(photo)
    monkeypatch.chdir(tmp_path)

    app = DedupeApp(tmp_path, "shortest", "waste", 0)
    async with app.run_test() as pilot:
        screen = ServerScreen()
        await app.push_screen(screen)
        await pilot.pause()

        view = screen.query_one("#entries", OptionList)
        assert view.option_count == len(screen.entries)
        assert all(p.name != "sym.jpg" for p in screen.entries)
        album_idx = next(i for i, p in enumerate(screen.entries) if p == sub)
        await screen._activate_index(album_idx)
        await pilot.pause()
        assert screen.root == sub
        assert any(p == photo for p in screen.entries)


def test_treesize_uses_iter_file_stats(tmp_path, capsys):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "img.jpg").write_bytes(b"x" * 2048)
    (tmp_path / "a" / "other.jpg").write_bytes(b"y" * 1024)

    rc = cmd_treesize(argparse.Namespace(path=str(tmp_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "3.0 KB" in out
    assert "a/b" in out
