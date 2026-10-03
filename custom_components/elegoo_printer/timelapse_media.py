"""
Centauri Carbon 2 timelapses as playable media.

A timelapse is fetched the first time it is played: frames are composed into
an MP4 on the printer if that has not happened yet, the video is downloaded
once and kept under ``<config>/elegoo_printer/timelapses``, outside ``www``,
so it is only ever served through the authenticated view below.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .cc2.client import ElegooCC2Client
from .cc2.timelapse import TimelapseDownloadError
from .const import DOMAIN, LOGGER
from .sdcp.exceptions import (
    ElegooPrinterConnectionError,
    ElegooPrinterNotConnectedError,
    ElegooPrinterTimeoutError,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cc2.timelapse import CC2PrintTask

CACHE_DIR = ("elegoo_printer", "timelapses")
COMPOSE_POLL_INTERVAL = 3  # seconds
# An 11 minute print composed in under 30 s; frames the printer has not
# turned into a video by then are not going to be.
COMPOSE_TIMEOUT = 90  # seconds
# After a failure, answer straight away instead of asking the printer again.
FAILURE_MEMORY = 600  # seconds
# Frames are composed while they are fresh. Older ones were never seen to
# survive: the printer accepts 1051 for them but never serves a video.
PREFETCH_WINDOW = 6 * 3600  # seconds after a job ends
VIEW_URL = "/api/elegoo_printer/timelapse/{entry_id}/{task_id}.mp4"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IN_FLIGHT: dict[str, asyncio.Future[Path]] = {}
_FAILED: dict[str, tuple[float, str]] = {}
_SAVED: set[str] = set()

PRINTER_ERRORS = (
    ElegooPrinterConnectionError,
    ElegooPrinterNotConnectedError,
    ElegooPrinterTimeoutError,
    TimelapseDownloadError,
)


class TimelapseError(HomeAssistantError):
    """A timelapse could not be made available."""


def timelapse_url(entry_id: str, task_id: str) -> str:
    """Return the authenticated URL a timelapse is served from."""
    return VIEW_URL.format(entry_id=entry_id, task_id=task_id)


def cc2_client(hass: HomeAssistant, entry_id: str) -> ElegooCC2Client:
    """Return the loaded CC2 client for a config entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        msg = "Unknown printer"
        raise TimelapseError(msg)
    if entry.state is not ConfigEntryState.LOADED:
        msg = f"{entry.title} is not loaded"
        raise TimelapseError(msg)
    client = entry.runtime_data.api.client
    if not isinstance(client, ElegooCC2Client):
        msg = "Timelapses are only available on a Centauri Carbon 2"
        raise TimelapseError(msg)
    return client


def _cache_path(hass: HomeAssistant, task_id: str) -> Path:
    return Path(hass.config.path(*CACHE_DIR, f"{task_id}.mp4"))


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)


async def _download_composed(
    client: ElegooCC2Client, task: CC2PrintTask, video_url: str
) -> bytes:
    """
    Download a timelapse that 1051 was just asked to compose.

    The job list cannot be trusted to say when it is done: on firmware
    02.01.00.00 composing an older job left that job at status 1 and set
    status 3 on an unrelated one. So try the path 1051 answered with until it
    serves an MP4, and take the list's word only as a second opinion.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMPOSE_TIMEOUT
    while True:
        try:
            return await client.download_timelapse(video_url)
        except TimelapseDownloadError as err:
            LOGGER.debug("Timelapse of %s not downloadable yet: %s", task.task_id, err)
            if loop.time() >= deadline:
                msg = (
                    "The printer has no video for this timelapse. Its frames are "
                    "probably gone; timelapses are now saved to Home Assistant "
                    "as soon as a print finishes so this does not happen again."
                )
                raise TimelapseError(msg) from err
        await asyncio.sleep(COMPOSE_POLL_INTERVAL)
        tasks = await client.get_print_task_list()
        listed = next((t for t in tasks if t.task_id == task.task_id), None)
        if listed is not None and listed.video_ready:
            video_url = listed.timelapse_url


async def _fetch(
    hass: HomeAssistant, client: ElegooCC2Client, task: CC2PrintTask, path: Path
) -> None:
    video_url = await client.get_timelapse_video_url(task)
    if task.video_ready:
        data = await client.download_timelapse(video_url)
    else:
        LOGGER.info("Composing the timelapse of %s on the printer", task.file_name)
        data = await _download_composed(client, task, video_url)
    await hass.async_add_executor_job(_write_atomically, path, data)
    LOGGER.info("Saved the timelapse of %s (%d bytes)", task.file_name, len(data))


async def _get_or_fetch(
    hass: HomeAssistant, entry_id: str, task_id: str, path: Path
) -> Path:
    if await hass.async_add_executor_job(path.is_file):
        _SAVED.add(task_id)
        return path
    client = cc2_client(hass, entry_id)
    try:
        tasks = await client.get_print_task_list()
        task = next((t for t in tasks if t.task_id == task_id), None)
        if task is None or not task.has_timelapse:
            msg = "The printer has no timelapse for that job"
            raise TimelapseError(msg)
        await _fetch(hass, client, task, path)
        _SAVED.add(task_id)
    except PRINTER_ERRORS as err:
        msg = f"Could not get the timelapse from the printer: {err}"
        raise TimelapseError(msg) from err
    return path


async def async_get_timelapse_file(
    hass: HomeAssistant, entry_id: str, task_id: str
) -> Path:
    """
    Return the local copy of a job's timelapse, fetching it on first use.

    Callers asking for the same job while it is being fetched share that one
    fetch, and a failure is remembered for ten minutes so a timelapse the
    printer cannot produce fails at once instead of making every viewer wait
    out the compose timeout again.
    """
    if not _SAFE_ID.match(entry_id) or not _SAFE_ID.match(task_id):
        msg = "Invalid timelapse id"
        raise TimelapseError(msg)
    loop = asyncio.get_running_loop()
    if (failed := _FAILED.get(task_id)) and loop.time() - failed[0] < FAILURE_MEMORY:
        raise TimelapseError(failed[1])
    if (pending := _IN_FLIGHT.get(task_id)) is not None:
        return await asyncio.shield(pending)
    path = _cache_path(hass, task_id)
    future: asyncio.Future[Path] = loop.create_future()
    _IN_FLIGHT[task_id] = future
    try:
        result = await _get_or_fetch(hass, entry_id, task_id, path)
    except TimelapseError as err:
        _FAILED[task_id] = (loop.time(), str(err))
        future.set_exception(err)
        future.exception()  # retrieved here so an unawaited future stays quiet
        raise
    except BaseException:
        future.cancel()
        raise
    else:
        _FAILED.pop(task_id, None)
        future.set_result(result)
        return result
    finally:
        del _IN_FLIGHT[task_id]


async def _prefetch(hass: HomeAssistant, entry_id: str, task_id: str) -> None:
    try:
        await async_get_timelapse_file(hass, entry_id, task_id)
    except TimelapseError as err:
        LOGGER.info("Could not save timelapse %s: %s", task_id, err)


@callback
def async_schedule_prefetch(
    hass: HomeAssistant, entry_id: str, tasks: list[CC2PrintTask]
) -> None:
    """
    Save new timelapses to Home Assistant in the background.

    Frames are composed while the printer still has them, and finished videos
    are copied before the printer can drop them, so a timelapse stays
    playable for as long as Home Assistant keeps the file.
    """
    now = time.time()
    loop_now = asyncio.get_running_loop().time()
    for task in tasks:
        if not task.has_timelapse or task.task_id in _SAVED:
            continue
        if task.task_id in _IN_FLIGHT:
            continue
        failed = _FAILED.get(task.task_id)
        if failed and loop_now - failed[0] < FAILURE_MEMORY:
            continue
        if not task.video_ready and now - task.end_time > PREFETCH_WINDOW:
            continue
        hass.async_create_background_task(
            _prefetch(hass, entry_id, task.task_id),
            name=f"elegoo_printer timelapse {task.task_id}",
        )


class ElegooTimelapseView(HomeAssistantView):
    """Serve a timelapse to signed-in users and signed media URLs."""

    url = VIEW_URL
    name = "api:elegoo_printer:timelapse"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, task_id: str
    ) -> web.StreamResponse:
        """Return the MP4, fetching it from the printer if it is not cached."""
        hass = request.app["hass"]
        try:
            path = await async_get_timelapse_file(hass, entry_id, task_id)
        except TimelapseError as err:
            return web.Response(status=404, text=str(err))
        return web.FileResponse(path, headers={"Content-Type": "video/mp4"})
