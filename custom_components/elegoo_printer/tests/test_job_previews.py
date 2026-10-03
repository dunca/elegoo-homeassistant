"""Tests for choosing which job previews to fetch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.elegoo_printer import job_previews
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list


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
