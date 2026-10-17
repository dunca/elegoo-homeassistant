"""Tests for choosing which job previews to fetch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.elegoo_printer import job_previews
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list
from custom_components.elegoo_printer.tests.test_gcode_thumbnail import _gcode, _png


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    job_previews._SAVED.clear()
    job_previews._FAILED.clear()
    job_previews._RUNNING.clear()


def _tasks(*names: str) -> list:
    rows = [{"task_id": str(i), "task_name": n} for i, n in enumerate(names)]
    return parse_task_list({"history_task_list": rows})


def test_fetches_files_on_the_printer_newest_first_once() -> None:
    job_previews._SAVED.add(job_previews.preview_key("done.gcode"))
    job_previews._FAILED.add(job_previews.preview_key("bad.gcode"))
    on_printer = {"old.gcode", "new.gcode", "done.gcode", "bad.gcode"}
    tasks = _tasks(
        "old.gcode", "gone.gcode", "done.gcode", "bad.gcode", "new.gcode", "new.gcode"
    )
    assert job_previews.files_to_fetch(on_printer, tasks) == ["new.gcode", "old.gcode"]


def test_schedules_one_fetch_per_printer() -> None:
    client = MagicMock()
    client.printer_data.file_list = {"a.gcode": 1}
    hass = MagicMock()
    hass.async_create_background_task.side_effect = lambda coro, **_: coro.close()
    job_previews.async_schedule_fetch(hass, "E", client, _tasks("a.gcode"))
    assert hass.async_create_background_task.call_count == 1
    job_previews._RUNNING.add("E")
    job_previews.async_schedule_fetch(hass, "E", client, _tasks("a.gcode"))
    assert hass.async_create_background_task.call_count == 1


def test_preview_url_only_for_saved_and_carries_token() -> None:
    assert job_previews.preview_url("a.gcode") is None
    job_previews._SAVED.add(job_previews.preview_key("a.gcode"))
    url = job_previews.preview_url("a.gcode")
    assert url is not None
    assert url.startswith("/api/elegoo_printer/preview/")
    assert "?token=" in url


def test_is_hires_png() -> None:
    assert not job_previews.is_hires_png(b"not png")
    assert not job_previews.is_hires_png(_png(144, 144))
    assert job_previews.is_hires_png(_png(512, 512))


@pytest.mark.asyncio
async def test_async_get_preview_upgrades_from_gcode(tmp_path: Path) -> None:
    hass = MagicMock()
    previews_dir = tmp_path / "elegoo_printer" / "previews"
    gcode_dir = tmp_path / "elegoo_printer" / "gcode"
    previews_dir.mkdir(parents=True)
    gcode_dir.mkdir(parents=True)

    hass.config.path.side_effect = lambda *parts: str(tmp_path / Path(*parts))
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))

    gfile = _gcode(gcode_dir, 144, 512)
    # Target archive filename: <key>.gcode
    key = job_previews.preview_key("job.gcode")
    target_gcode = gcode_dir / f"{key}.gcode"
    gfile.replace(target_gcode)

    preview = await job_previews.async_get_preview(hass, "job.gcode")
    assert preview is not None
    assert job_previews.is_hires_png(preview)
    assert key in job_previews._HIRES
    assert key in job_previews._SAVED
