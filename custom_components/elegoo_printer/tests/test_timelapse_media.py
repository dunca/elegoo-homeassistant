"""Tests for sharing and remembering timelapse fetches."""

from __future__ import annotations

import asyncio
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
