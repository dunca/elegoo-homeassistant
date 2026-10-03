"""Tests for sharing and remembering timelapse fetches."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.elegoo_printer import timelapse_media
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list
from custom_components.elegoo_printer.timelapse_media import (
    TimelapseError,
    async_get_timelapse_file,
)

ENTRY = "01M3H1QPHWZ1AY5SV86KJTNXC2"
TASK = "896c3f5a-80d1-413c-95ed-1e9812d912b8"


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    timelapse_media._IN_FLIGHT.clear()
    timelapse_media._FAILED.clear()


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.config.path = lambda *parts: str(Path("/tmp", *parts))  # noqa: S108
    return hass


def test_concurrent_requests_share_one_fetch() -> None:
    calls = 0

    async def slow_fetch(*_args: object) -> Path:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return Path("/tmp/video.mp4")  # noqa: S108

    async def run() -> list[Path]:
        with patch.object(timelapse_media, "_get_or_fetch", slow_fetch):
            return await asyncio.gather(
                *(async_get_timelapse_file(_hass(), ENTRY, TASK) for _ in range(3))
            )

    results = asyncio.run(run())
    assert calls == 1
    assert results == [Path("/tmp/video.mp4")] * 3  # noqa: S108


def test_failure_is_remembered() -> None:
    calls = 0

    async def failing_fetch(*_args: object) -> Path:
        nonlocal calls
        calls += 1
        msg = "The printer is still composing the timelapse"
        raise TimelapseError(msg)

    async def run() -> None:
        with patch.object(timelapse_media, "_get_or_fetch", failing_fetch):
            for _ in range(2):
                with pytest.raises(TimelapseError, match="composing"):
                    await async_get_timelapse_file(_hass(), ENTRY, TASK)

    asyncio.run(run())
    assert calls == 1


def test_rejects_path_tricks() -> None:
    async def run() -> None:
        await async_get_timelapse_file(_hass(), ENTRY, "../../secrets")

    with pytest.raises(TimelapseError, match="Invalid"):
        asyncio.run(run())


def test_prefetch_picks_fresh_frames_and_ready_videos() -> None:
    now = int(time.time())
    rows = [
        # frames from a print that just ended: compose and save
        {
            "task_id": "fresh",
            "end_time": now - 60,
            "time_lapse_video_status": 1,
            "time_lapse_video_url": "picture/a",
        },
        # frames from last month: the printer cannot compose these any more
        {
            "task_id": "stale",
            "end_time": now - 30 * 86400,
            "time_lapse_video_status": 1,
            "time_lapse_video_url": "picture/b",
        },
        # a finished video, however old: copy it
        {
            "task_id": "ready",
            "end_time": now - 30 * 86400,
            "time_lapse_video_status": 2,
            "time_lapse_video_url": "video/c.mp4",
        },
        # no timelapse at all
        {"task_id": "none", "end_time": now - 60, "time_lapse_video_status": 0},
        # already saved
        {
            "task_id": "saved",
            "end_time": now - 60,
            "time_lapse_video_status": 2,
            "time_lapse_video_url": "video/d.mp4",
        },
    ]
    timelapse_media._SAVED.add("saved")
    hass = _hass()

    async def run() -> None:
        with patch.object(timelapse_media, "_prefetch", MagicMock()) as prefetch:
            timelapse_media.async_schedule_prefetch(
                hass, ENTRY, parse_task_list({"history_task_list": rows})
            )
            scheduled = [c.args[2] for c in prefetch.call_args_list]
            assert scheduled == ["fresh", "ready"]

    asyncio.run(run())
    assert hass.async_create_background_task.call_count == 2


def test_is_playable() -> None:
    now = time.time()
    fresh, stale, ready = parse_task_list(
        {
            "history_task_list": [
                {"task_id": "f", "end_time": now - 60, "time_lapse_video_status": 1},
                {"task_id": "s", "end_time": now - 86400, "time_lapse_video_status": 1},
                {
                    "task_id": "r",
                    "end_time": now - 86400,
                    "time_lapse_video_status": 2,
                    "time_lapse_video_url": "video/r.mp4",
                },
            ]
        }
    )
    assert timelapse_media.is_playable(fresh, now)
    assert not timelapse_media.is_playable(stale, now)
    assert timelapse_media.is_playable(ready, now)
    timelapse_media._SAVED.add("s")
    assert timelapse_media.is_playable(stale, now)


def test_pending_until_saved_or_failed_twice() -> None:
    now = time.time()
    (task,) = parse_task_list(
        {
            "history_task_list": [
                {"task_id": "p", "end_time": now - 60, "time_lapse_video_status": 1}
            ]
        }
    )
    assert timelapse_media.is_pending(task, now)
    timelapse_media._ATTEMPTS["p"] = 2
    assert not timelapse_media.is_pending(task, now)
    timelapse_media._ATTEMPTS.clear()
    timelapse_media._SAVED.add("p")
    assert not timelapse_media.is_pending(task, now)


def _video(directory: Path, name: str, size: int, age: float, now: float) -> Path:
    path = directory / f"{name}.mp4"
    path.write_bytes(b"\0" * size)
    os.utime(path, (now - age, now - age))
    return path


def test_prune_removes_the_oldest_until_it_fits(tmp_path: Path) -> None:
    now = time.time()
    day = 86400
    _video(tmp_path, "old", 40, 3 * day, now)
    _video(tmp_path, "mid", 40, 2 * day, now)
    new = _video(tmp_path, "new", 40, 0, now)
    with patch.object(timelapse_media, "MAX_BYTES", 90):
        removed = timelapse_media._prune(tmp_path, new, now)
    assert removed == ["old"]
    assert sorted(p.stem for p in tmp_path.glob("*.mp4")) == ["mid", "new"]
    assert (tmp_path / "pruned.txt").read_text().split() == ["old"]


def test_prune_never_removes_a_fresh_video(tmp_path: Path) -> None:
    now = time.time()
    _video(tmp_path, "fresh", 40, 3600, now)
    new = _video(tmp_path, "new", 40, 0, now)
    with patch.object(timelapse_media, "MAX_BYTES", 50):
        assert timelapse_media._prune(tmp_path, new, now) == []
    assert len(list(tmp_path.glob("*.mp4"))) == 2


def test_a_pruned_timelapse_is_not_copied_again() -> None:
    now = time.time()
    (task,) = parse_task_list(
        {
            "history_task_list": [
                {"task_id": "gone", "end_time": now - 60, "time_lapse_video_status": 2}
            ]
        }
    )
    assert timelapse_media.is_pending(task, now)
    timelapse_media._PRUNED.add("gone")
    try:
        assert not timelapse_media.is_pending(task, now)
    finally:
        timelapse_media._PRUNED.clear()


def test_usage_reports_against_the_20_gb_limit() -> None:
    timelapse_media._USAGE.update({"bytes": 5 * 1024**3, "count": 3})
    try:
        assert timelapse_media.usage() == {
            "used_gb": 5.0,
            "used_mb": 5120,
            "limit_gb": 20,
            "percent": 25.0,
            "count": 3,
        }
    finally:
        timelapse_media._USAGE.update({"bytes": 0, "count": 0})
