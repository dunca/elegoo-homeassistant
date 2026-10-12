"""
Centauri Carbon 2 timelapses as playable media.

The printer turns each print's frames into an MP4 by itself when the print
ends. Home Assistant never asks it to (no method 1051): it waits until the
job list reports the video ready, downloads it once and keeps it under
``<config>/elegoo_printer/timelapses``, outside ``www``, so it is only ever
served through the authenticated view below. A second compose request raced
the printer's own encode on 12 Oct 2026 and every video came out empty.

The folder is capped at ``MAX_BYTES`` (10 GB): once a new video pushes it
over, the oldest videos go first. A video saved in the last
``PRUNE_MIN_AGE`` is never removed, and removed ones are listed in
``pruned.txt`` so they are not copied off the printer again.
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
# After a failure, answer straight away instead of asking the printer again.
FAILURE_MEMORY = 600  # seconds
PREFETCH_WINDOW = 6 * 3600  # seconds after a job ends
# How long after a job ends to wait for the printer's video. The printer needs
# a while to encode a fresh job, longer for more frames, so the window scales
# with the print's own length. While inside it the dashboard shows "Timelapse
# in progress" and prefetch checks again every COMPOSE_RETRY_GAP; past it a job
# that still has no video is reported as unavailable.
COMPOSE_WINDOW_MIN = 180  # seconds (floor, short prints)
COMPOSE_WINDOW_MAX = 1200  # seconds (cap, 20 min)
COMPOSE_WINDOW_FACTOR = 0.5  # of the print's own duration
COMPOSE_RETRY_GAP = 45  # seconds between checks while the printer is still encoding
VIEW_URL = "/api/elegoo_printer/timelapse/{entry_id}/{task_id}.mp4"
MAX_BYTES = 10 * 1024**3
# never remove a video this fresh, so prefetch cannot fetch it again
PRUNE_MIN_AGE = 2 * PREFETCH_WINDOW  # seconds
PRUNED_FILE = "pruned.txt"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IN_FLIGHT: dict[str, asyncio.Future[Path]] = {}
_FAILED: dict[str, tuple[float, str, float]] = {}
_SAVED: set[str] = set()
_ATTEMPTS: dict[str, int] = {}
_PRUNED: set[str] = set()
_USAGE: dict[str, int] = {"bytes": 0, "count": 0}
# A high cap only to stop runaway retries; the compose window is the real bound.
MAX_ATTEMPTS = 40

PRINTER_ERRORS = (
    ElegooPrinterConnectionError,
    ElegooPrinterNotConnectedError,
    ElegooPrinterTimeoutError,
    TimelapseDownloadError,
)


class TimelapseError(HomeAssistantError):
    """A timelapse could not be made available."""


class TimelapseNotReadyError(TimelapseError):
    """The printer has not finished encoding the video yet."""


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


def _saved_ids(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob("*.mp4")}


def _measure(directory: Path) -> dict[str, int]:
    files = list(directory.glob("*.mp4")) if directory.is_dir() else []
    return {"bytes": sum(p.stat().st_size for p in files), "count": len(files)}


def _read_pruned(directory: Path) -> set[str]:
    path = directory / PRUNED_FILE
    return set(path.read_text().split()) if path.is_file() else set()


def _prune(directory: Path, keep: Path, now: float) -> list[str]:
    """Delete the oldest videos until the folder fits ``MAX_BYTES``."""
    files = sorted(directory.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed: list[str] = []
    for path in files:
        if total <= MAX_BYTES:
            break
        if path == keep or now - path.stat().st_mtime < PRUNE_MIN_AGE:
            continue
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        removed.append(path.stem)
    if removed:
        pruned = _read_pruned(directory) | set(removed)
        (directory / PRUNED_FILE).write_text("\n".join(sorted(pruned)) + "\n")
    return removed


def usage() -> dict[str, float | int]:
    """How much of the timelapse folder's limit is in use."""
    used = _USAGE["bytes"]
    return {
        "used_gb": round(used / 1024**3, 2),
        "used_mb": round(used / 1024**2),
        "limit_gb": round(MAX_BYTES / 1024**3),
        "percent": round(100 * used / MAX_BYTES, 2),
        "count": _USAGE["count"],
    }


async def async_refresh_usage(hass: HomeAssistant) -> None:
    """Measure the timelapse folder again (after a save or a delete)."""
    directory = Path(hass.config.path(*CACHE_DIR))
    _USAGE.update(await hass.async_add_executor_job(_measure, directory))


async def async_load_saved(hass: HomeAssistant) -> None:
    """Remember which timelapses are already saved, after a restart."""
    directory = Path(hass.config.path(*CACHE_DIR))
    _SAVED.update(await hass.async_add_executor_job(_saved_ids, directory))
    _PRUNED.update(await hass.async_add_executor_job(_read_pruned, directory))
    await async_refresh_usage(hass)


def is_playable(task: CC2PrintTask, now: float | None = None) -> bool:
    """Whether a job's timelapse is saved, ready, or still being encoded."""
    return has_video(task) or is_composing(task, now)


def _compose_window(task: CC2PrintTask) -> float:
    """Seconds after a job ends to wait for its video, scaled to its length."""
    duration = max(0, task.end_time - task.begin_time)
    return min(
        max(duration * COMPOSE_WINDOW_FACTOR, COMPOSE_WINDOW_MIN), COMPOSE_WINDOW_MAX
    )


def has_video(task: CC2PrintTask) -> bool:
    """Whether a finished video exists to play now (saved here or on the printer)."""
    return task.task_id in _SAVED or task.video_ready


def is_composing(task: CC2PrintTask, now: float | None = None) -> bool:
    """Return whether a job's frames are not a video yet but still might become one."""
    if not task.has_timelapse or has_video(task):
        return False
    now = now if now is not None else time.time()
    return 0 <= now - task.end_time <= _compose_window(task)


def _cache_path(hass: HomeAssistant, task_id: str) -> Path:
    return Path(hass.config.path(*CACHE_DIR, f"{task_id}.mp4"))


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)


async def _fetch(
    hass: HomeAssistant, client: ElegooCC2Client, task: CC2PrintTask, path: Path
) -> None:
    if not task.video_ready:
        msg = "The printer is still making this timelapse; try again in a minute"
        raise TimelapseNotReadyError(msg)
    data = await client.download_timelapse(task.timelapse_url)
    await hass.async_add_executor_job(_write_atomically, path, data)
    LOGGER.info("Saved the timelapse of %s (%d bytes)", task.file_name, len(data))
    removed = await hass.async_add_executor_job(_prune, path.parent, path, time.time())
    for task_id in removed:
        _SAVED.discard(task_id)
        _PRUNED.add(task_id)
        LOGGER.info("Removed the oldest timelapse %s to stay under the limit", task_id)
    await async_refresh_usage(hass)


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
        _PRUNED.discard(task_id)  # asked for again, so keep it this time
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
    fetch. A failure is remembered for ten minutes so a timelapse the printer
    cannot provide fails at once; "not ready yet" only for COMPOSE_RETRY_GAP,
    so the video plays as soon as the printer has finished it.
    """
    if not _SAFE_ID.match(entry_id) or not _SAFE_ID.match(task_id):
        msg = "Invalid timelapse id"
        raise TimelapseError(msg)
    loop = asyncio.get_running_loop()
    if (failed := _FAILED.get(task_id)) and loop.time() - failed[0] < failed[2]:
        raise (
            TimelapseNotReadyError if failed[2] == COMPOSE_RETRY_GAP else TimelapseError
        )(failed[1])
    if (pending := _IN_FLIGHT.get(task_id)) is not None:
        return await asyncio.shield(pending)
    path = _cache_path(hass, task_id)
    future: asyncio.Future[Path] = loop.create_future()
    _IN_FLIGHT[task_id] = future
    try:
        result = await _get_or_fetch(hass, entry_id, task_id, path)
    except TimelapseError as err:
        memory = (
            COMPOSE_RETRY_GAP
            if isinstance(err, TimelapseNotReadyError)
            else FAILURE_MEMORY
        )
        _FAILED[task_id] = (loop.time(), str(err), memory)
        _ATTEMPTS[task_id] = _ATTEMPTS.get(task_id, 0) + 1
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


def forget_saved(hass: HomeAssistant, task_id: str) -> Path | None:
    """Drop a saved timelapse from the index; return its file to delete."""
    if task_id not in _SAVED:
        return None
    _SAVED.discard(task_id)
    return _cache_path(hass, task_id)


def is_saved(task_id: str) -> bool:
    """Whether the timelapse is already copied to Home Assistant."""
    return task_id in _SAVED


def is_pending(task: CC2PrintTask, now: float | None = None) -> bool:
    """
    Whether a timelapse still has to be copied off the printer.

    A ready video is copied however old it is; a job still stored as frames
    is waited for while inside its (length-scaled) window. Automations that
    power the printer off wait for this. A runaway job that has failed many
    times no longer counts, so it cannot hold the printer on.
    """
    now = now if now is not None else time.time()
    if task.task_id in _SAVED or task.task_id in _PRUNED or not task.has_timelapse:
        return False
    if _ATTEMPTS.get(task.task_id, 0) >= MAX_ATTEMPTS:
        return False
    if task.video_ready:
        return True  # a finished video, however old: copy it before it is dropped
    return is_composing(task, now)


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

    Finished videos are copied before the printer can drop them, so a
    timelapse stays playable for as long as Home Assistant keeps the file.
    """
    now = time.time()
    loop_now = asyncio.get_running_loop().time()
    for task in tasks:
        if not is_pending(task, now):
            continue
        if task.task_id in _IN_FLIGHT:
            continue
        failed = _FAILED.get(task.task_id)
        # While a fresh job is still composing, retry soon; otherwise back off.
        gap = COMPOSE_RETRY_GAP if is_composing(task, now) else FAILURE_MEMORY
        if failed and loop_now - failed[0] < gap:
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
