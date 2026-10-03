"""Tests for the job archive and the G-code archive."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from custom_components.elegoo_printer import gcode_archive
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list
from custom_components.elegoo_printer.job_archive import JobArchive
from custom_components.elegoo_printer.sdcp.models.file_info import PrinterFile

if TYPE_CHECKING:
    from pathlib import Path


def _tasks(*rows: tuple[str, str, int]) -> list:
    return parse_task_list(
        {
            "history_task_list": [
                {"task_id": t, "task_name": f, "end_time": e, "task_status": 1}
                for t, f, e in rows
            ]
        }
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    gcode_archive._SAVED.clear()
    gcode_archive._FAILED.clear()


def _archive() -> JobArchive:
    with patch("custom_components.elegoo_printer.job_archive.Store") as store:
        archive = JobArchive(MagicMock(), "E")
    archive._store = store.return_value
    return archive


def test_archive_keeps_jobs_the_printer_forgets() -> None:
    archive = _archive()
    file = PrinterFile(
        {
            "filename": "a.gcode",
            "total_filament_used": 3.76,
            "color_map": [{"t": 0, "color": "#FFFFFF", "name": "PLA"}],
        }
    )
    archive.merge(
        _tasks(("1", "a.gcode", 100), ("2", "b.gcode", 200)), {"a.gcode": file}
    )
    # later the printer only lists job 2, and a.gcode is gone
    archive.merge(_tasks(("2", "b.gcode", 200)), {})
    assert [t.task_id for t in archive.tasks()] == ["1", "2"]
    meta = archive.meta("a.gcode")
    assert meta is not None
    assert meta["filament_colors"] == ["#FFFFFF"]
    assert archive._store.async_delay_save.call_count == 1


def test_files_to_copy_newest_first_once() -> None:
    gcode_archive._SAVED.add(gcode_archive.file_key("kept.gcode"))
    tasks = _tasks(
        ("1", "old.gcode", 1),
        ("2", "kept.gcode", 2),
        ("3", "new.gcode", 3),
        ("4", "gone.gcode", 4),
        ("5", "new.gcode", 5),
    )
    on_printer = {"old.gcode", "kept.gcode", "new.gcode"}
    assert gcode_archive.files_to_copy(on_printer, tasks) == ["new.gcode", "old.gcode"]


def test_prune_drops_oldest_copies_over_the_cap(tmp_path: Path) -> None:
    paths = []
    for i, name in enumerate(["a", "b", "c"]):
        p = tmp_path / f"{name}.gcode"
        p.write_bytes(b"x" * 100)
        os.utime(p, (1000 + i, 1000 + i))
        paths.append(p)
    with patch.object(gcode_archive, "MAX_BYTES", 150):
        removed = gcode_archive._prune(tmp_path, paths[2])
    assert removed == ["a", "b"]
    assert [p.exists() for p in paths] == [False, False, True]
