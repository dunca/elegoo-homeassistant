"""Tests for sharing and remembering timelapse fetches."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.elegoo_printer import timelapse_media
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
