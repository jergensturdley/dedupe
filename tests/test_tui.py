"""Headless checks for the mouse-first TUI additions.

Run with the project venv:  .venv/bin/python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dedupe as dedupe_mod
from dedupe import DedupeApp, TrashScreen
from textual.widgets import Button, Input


@pytest.fixture()
def fixture_dir(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    data = bytes(range(256)) * 128
    (tmp_path / "a.bin").write_bytes(data)
    (docs / "b.bin").write_bytes(data)
    (tmp_path / "readme.txt").write_text("hello world\n")
    (docs / "readme-copy.txt").write_text("hello world\n")
    return tmp_path


def make_app(path):
    return DedupeApp(path, "shortest", "waste", 0)


async def wait_for(pilot, condition, attempts=50):
    for _ in range(attempts):
        await pilot.pause(0.1)
        if condition():
            return True
    return False


@pytest.mark.asyncio
async def test_buttons_and_file_ops(fixture_dir, monkeypatch):
    trash = fixture_dir / "trash"
    monkeypatch.setattr(dedupe_mod, "TRASH_ROOT", trash)

    app = make_app(fixture_dir)
    async with app.run_test() as pilot:
        assert len(app.groups) == 2

        # All mouse controls are mounted.
        for btn in (
            "btn-rescan", "btn-sort", "btn-select-all", "btn-delete",
            "btn-trash", "btn-tools", "btn-quit", "btn-filter",
            "btn-filter-clear", "btn-mark", "btn-keep", "btn-open",
            "btn-reveal", "btn-copy", "btn-info", "btn-preview",
            "btn-hide-info", "btn-keep-shortest", "btn-keep-oldest",
            "btn-keep-newest",
        ):
            assert app.query_one(f"#{btn}") is not None

        assert app.file_index == 0
        if app.selected_path() == app.groups[0].keeper:
            app.file_index = 1
        target = app.selected_path()

        # Mark via button, keeper strategy via buttons.
        app.query_one("#btn-mark").press()
        await pilot.pause()
        group = app.groups[app.current]
        assert target in group.marked

        app.query_one("#btn-keep-oldest").press()
        await pilot.pause()
        assert app.keep == "oldest"
        assert group.keeper in group.files
        assert group.keeper not in group.marked

        # Info and preview populate the preview area.
        app.query_one("#btn-info").press()
        await pilot.pause()
        preview = app.query_one("#preview")
        assert preview.has_class("-visible")
        app.query_one("#btn-hide-info").press()
        await pilot.pause()
        assert not preview.has_class("-visible")

        # Filter narrows groups by name.
        flt = app.query_one("#filter", Input)
        flt.value = "readme"
        app._filter_from_input()
        await pilot.pause()
        shown = app.shown if app.shown is not None else app.groups
        assert len(shown) == 1 and "readme" in str(shown[0].files[0])
        app.clear_filter()
        await pilot.pause()

        # Select all + delete through buttons lands files in trash.
        app.query_one("#btn-select-all").press()
        await pilot.pause()
        assert sum(len(g.marked) for g in app.groups) == 2

        app.action_delete()
        await pilot.pause()
        app.screen.query_one("#confirm", Button).press()
        assert await wait_for(
            pilot, lambda: sum(len(g.files) for g in app.groups) == 0
        )
        assert list(trash.glob("*/manifest.json"))

        # Trash screen restores the batch and the app rescans.
        app.action_trash()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TrashScreen)
        assert len(screen.batches) == 1
        screen.restore_latest()
        assert await wait_for(pilot, lambda: len(screen.batches) == 0)
        screen.action_back()
        await pilot.pause()
        assert await wait_for(
            pilot, lambda: sum(len(g.files) for g in app.groups) == 4
        )


@pytest.mark.asyncio
async def test_keep_cycle_and_preview(fixture_dir):
    app = make_app(fixture_dir)
    async with app.run_test() as pilot:
        app.action_cycle_keep()
        await pilot.pause()
        assert app.keep == "oldest"
        app.action_cycle_keep()
        await pilot.pause()
        assert app.keep == "newest"
        app.action_cycle_keep()
        await pilot.pause()
        assert app.keep == "shortest"

        for gi, g in enumerate(app.groups):
            for fi, f in enumerate(g.files):
                if f.name == "readme-copy.txt":
                    app.current, app.file_index = gi, fi
        path = app.selected_path()
        assert path and path.name == "readme-copy.txt"
        await app.action_preview_file()
        await pilot.pause()
        assert app.query_one("#preview").has_class("-visible")
        assert "hello world" in str(DedupeApp._read_preview(path))


def test_trash_batches_listing(tmp_path, monkeypatch):
    root = tmp_path / "trash"
    monkeypatch.setattr(dedupe_mod, "TRASH_ROOT", root)
    batch = root / "20260101-000000-aaaaaa"
    batch.mkdir(parents=True)
    (batch / "manifest.json").write_text(
        '{"created": "2026-01-01T00:00:00", "entries": '
        '[{"original": "/x/old.txt", "stored": "old.txt"}]}'
    )
    batches = dedupe_mod.trash_batches()
    assert len(batches) == 1
    assert batches[0][1]["entries"][0]["stored"] == "old.txt"
