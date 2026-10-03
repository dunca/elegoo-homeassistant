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
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
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
COMPOSE_TIMEOUT = 180  # seconds
VIEW_URL = "/api/elegoo_printer/timelapse/{entry_id}/{task_id}.mp4"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOCKS: dict[str, asyncio.Lock] = {}

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


async def _wait_until_ready(client: ElegooCC2Client, task_id: str) -> None:
    """Poll the job list until the printer has written the composed video."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMPOSE_TIMEOUT
    while True:
        tasks = await client.get_print_task_list()
        task = next((t for t in tasks if t.task_id == task_id), None)
        if task is not None and task.video_ready:
            return
        if loop.time() >= deadline:
            msg = "The printer is still composing the timelapse, try again shortly"
            raise TimelapseError(msg)
        await asyncio.sleep(COMPOSE_POLL_INTERVAL)


async def _fetch(
    hass: HomeAssistant, client: ElegooCC2Client, task: CC2PrintTask, path: Path
) -> None:
    video_url = await client.get_timelapse_video_url(task)
    if not task.video_ready:
        LOGGER.info("Composing the timelapse of %s on the printer", task.file_name)
        await _wait_until_ready(client, task.task_id)
    data = await client.download_timelapse(video_url)
    await hass.async_add_executor_job(_write_atomically, path, data)
    LOGGER.info("Saved the timelapse of %s (%d bytes)", task.file_name, len(data))


async def async_get_timelapse_file(
    hass: HomeAssistant, entry_id: str, task_id: str
) -> Path:
    """Return the local copy of a job's timelapse, fetching it on first use."""
    if not _SAFE_ID.match(entry_id) or not _SAFE_ID.match(task_id):
        msg = "Invalid timelapse id"
        raise TimelapseError(msg)
    path = _cache_path(hass, task_id)
    lock = _LOCKS.setdefault(task_id, asyncio.Lock())
    async with lock:
        if await hass.async_add_executor_job(path.is_file):
            return path
        client = cc2_client(hass, entry_id)
        try:
            tasks = await client.get_print_task_list()
            task = next((t for t in tasks if t.task_id == task_id), None)
            if task is None or not task.has_timelapse:
                msg = "The printer has no timelapse for that job"
                raise TimelapseError(msg)
            await _fetch(hass, client, task, path)
        except PRINTER_ERRORS as err:
            msg = f"Could not get the timelapse from the printer: {err}"
            raise TimelapseError(msg) from err
    return path


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
