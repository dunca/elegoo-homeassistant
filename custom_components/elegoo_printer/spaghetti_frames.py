"""
The camera frame behind every spaghetti check, kept per Centauri Carbon 2 job.

The ``elegoo_spaghetti_detection`` integration (a separate project) sends one
snapshot per check to Obico, scores it and throws it away, so when a failure
slips through there is nothing to look at afterwards. After each scheduled
check it fires ``elegoo_spaghetti_detection_result``; on that event this
module fetches the printer's snapshot once (``http://<host>:8080/snapshot``,
a fraction of a second after the detector did) and files it under the job
being printed, named by local time and score::

    <config>/elegoo_printer/spaghetti_frames/<job id>/HHMMSS_SSSSS.jpg
    <config>/elegoo_printer/spaghetti_frames/<job id>/index.json

The printer has 108 MB of RAM, so there is never more than one request per
check: one GET with a 10 s timeout, no retry, and an event that arrives while
the previous fetch of that printer is still running is skipped.

Limits, per printer: the last ``keep_prints`` prints; at most ``max_frames``
frames per print, past which every other frame scored below ``KEEP_SCORE`` is
dropped (a long print keeps its whole timeline at a coarser step); and
``max_bytes`` on disk, the oldest prints going first.

The folder is outside ``www``. Frames are served by the views below to
signed-in users, or to requests carrying the access token that only
signed-in users can read (in the Print History sensor's attributes), which is
what lets a dashboard open the viewer in an iframe or a new tab. The plan
(docs/plans/2026-10-11-spaghetti-frames.md) explains why.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CC2_VIDEO_PORT,
    CONF_SPAGHETTI_FRAMES_ENABLED,
    CONF_SPAGHETTI_FRAMES_KEEP_PRINTS,
    CONF_SPAGHETTI_FRAMES_MAX_MB,
    CONF_SPAGHETTI_FRAMES_MAX_PER_PRINT,
    DEFAULT_SPAGHETTI_FRAMES_KEEP_PRINTS,
    DEFAULT_SPAGHETTI_FRAMES_MAX_MB,
    DEFAULT_SPAGHETTI_FRAMES_MAX_PER_PRINT,
    LOGGER,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DETECTOR_DOMAIN = "elegoo_spaghetti_detection"
EVENT_RESULT = f"{DETECTOR_DOMAIN}_result"
CACHE_DIR = ("elegoo_printer", "spaghetti_frames")
VIEWER_FILE = Path(__file__).parent / "static" / "spaghetti_frames.html"
SNAPSHOT_PATH = "/snapshot"
FETCH_TIMEOUT = 10  # seconds, one try
MAX_JPEG_BYTES = 5 * 1024 * 1024  # a 1280x720 frame is about 130 KB
NEW_PRINT_GAP = 900  # seconds without a frame before a job-less frame starts a print
KEEP_SCORE = 0.1  # thinning never drops a frame scored at or above this
INDEX = "index.json"
VIEW_BASE = "/api/elegoo_printer/frames"
MB = 1024 * 1024

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FRAME_NAME = re.compile(r"^\d{6}_\d{5}\.jpg$")
_JPEG_SOI = b"\xff\xd8"
# Random per Home Assistant run, like the job previews' token.
_TOKEN = secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class Limits:
    """How much is kept, per printer."""

    keep_prints: int = DEFAULT_SPAGHETTI_FRAMES_KEEP_PRINTS
    max_frames: int = DEFAULT_SPAGHETTI_FRAMES_MAX_PER_PRINT
    max_bytes: int = DEFAULT_SPAGHETTI_FRAMES_MAX_MB * MB


@dataclass(frozen=True, slots=True)
class PrintSummary:
    """What is known about one stored print without reading its index."""

    print_id: str
    entry_id: str
    task_id: str
    name: str
    start: float
    end: float
    count: int
    max_score: float
    size: int

    def as_row(self) -> dict[str, Any]:
        """Return the print as the viewer's index lists it."""
        return {
            "id": self.print_id,
            "task": self.task_id,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "count": self.count,
            "max": self.max_score,
        }


@dataclass(slots=True)
class SaveResult:
    """What a save changed, to be applied to the in-memory summaries."""

    print_id: str
    frame: str
    updated: dict[str, PrintSummary] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Capture:
    """A printer whose checks are being kept."""

    entry_id: str
    host: str
    limits: Limits
    current_job: Callable[[], tuple[str, str]]


# Shared by every config entry; touched on the event loop only.
_PRINTS: dict[str, PrintSummary] = {}
_CAPTURING: dict[str, _Capture] = {}
_BUSY: set[str] = set()
_LOCK = asyncio.Lock()
_STATE: dict[str, Any] = {"loaded": False, "viewer": None, "unsub": None}


# --- options ---------------------------------------------------------------


def limits_from_options(options: Mapping[str, Any]) -> Limits:
    """Return the limits set in an entry's options, or the defaults."""
    return Limits(
        keep_prints=int(
            options.get(
                CONF_SPAGHETTI_FRAMES_KEEP_PRINTS, DEFAULT_SPAGHETTI_FRAMES_KEEP_PRINTS
            )
        ),
        max_frames=int(
            options.get(
                CONF_SPAGHETTI_FRAMES_MAX_PER_PRINT,
                DEFAULT_SPAGHETTI_FRAMES_MAX_PER_PRINT,
            )
        ),
        max_bytes=int(
            options.get(CONF_SPAGHETTI_FRAMES_MAX_MB, DEFAULT_SPAGHETTI_FRAMES_MAX_MB)
        )
        * MB,
    )


def detector_installed(hass: HomeAssistant) -> bool:
    """Whether the spaghetti detection integration has a config entry."""
    return bool(hass.config_entries.async_entries(DETECTOR_DOMAIN))


# --- storage (executor) ----------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_atomically(path: Path, data: bytes) -> None:
    partial_path = path.with_name(path.name + ".part")
    partial_path.write_bytes(data)
    partial_path.replace(path)


def _write_json(path: Path, data: Any) -> None:
    _write_atomically(path, json.dumps(data, separators=(",", ":")).encode())


def _jpeg_bytes(folder: Path) -> int:
    try:
        return sum(
            entry.stat().st_size
            for entry in os.scandir(folder)
            if entry.is_file() and entry.name.endswith(".jpg")
        )
    except OSError:
        return 0


def _summary(print_id: str, meta: dict[str, Any], size: int) -> PrintSummary:
    frames = meta.get("frames") or []
    return PrintSummary(
        print_id=print_id,
        entry_id=str(meta.get("entry_id") or ""),
        task_id=str(meta.get("task") or ""),
        name=str(meta.get("name") or ""),
        start=float(meta.get("start") or 0),
        end=float(meta.get("end") or 0),
        count=len(frames),
        max_score=max((float(fr.get("s") or 0) for fr in frames), default=0.0),
        size=size,
    )


def load_summaries(root: Path) -> dict[str, PrintSummary]:
    """Read every stored print's index (once, at startup)."""
    found: dict[str, PrintSummary] = {}
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        if not folder.is_dir() or not _SAFE_ID.match(folder.name):
            continue
        meta = _read_json(folder / INDEX)
        if not isinstance(meta, dict) or not meta.get("frames"):
            continue
        found[folder.name] = _summary(folder.name, meta, _jpeg_bytes(folder))
    return found


def read_print_index(root: Path, print_id: str) -> dict[str, Any] | None:
    """Return a stored print's index, or None."""
    if not _SAFE_ID.match(print_id):
        return None
    meta = _read_json(root / print_id / INDEX)
    return meta if isinstance(meta, dict) else None


def _thin(folder: Path, frames: list[dict[str, Any]]) -> tuple[list[dict], int]:
    """Drop every other low-score frame; return the frames kept and bytes freed."""
    low = [i for i, fr in enumerate(frames) if float(fr.get("s") or 0) < KEEP_SCORE]
    drop = set(low[1::2])
    freed = 0
    for i in drop:
        path = folder / str(frames[i].get("f"))
        try:
            freed += path.stat().st_size
            path.unlink()
        except OSError:
            pass
    return [fr for i, fr in enumerate(frames) if i not in drop], freed


def _choose_print(
    root: Path,
    own: list[PrintSummary],
    known: dict[str, PrintSummary],
    task_id: str,
    now: float,
) -> tuple[str, list[str]]:
    """
    Return the print a frame belongs to, and any print id it replaces.

    With a job id the frame goes to that job. Without one it joins the
    printer's latest print unless that went quiet ``NEW_PRINT_GAP`` ago. A
    print that started without a job id is renamed to the job id when one
    turns up inside the gap, so it lines up with the job history.
    """
    latest = own[-1] if own else None
    recent = latest is not None and now - latest.end < NEW_PRINT_GAP
    if task_id:
        if (
            task_id not in known
            and recent
            and latest is not None
            and not latest.task_id
            and not (root / task_id).exists()
        ):
            (root / latest.print_id).rename(root / task_id)
            return task_id, [latest.print_id]
        return task_id, []
    if recent and latest is not None:
        return latest.print_id, []
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    return f"print-{stamp}", []


def _prune(
    root: Path,
    prints: dict[str, PrintSummary],
    entry_id: str,
    current: str,
    limits: Limits,
) -> list[str]:
    """Remove a printer's oldest prints beyond the count and size limits."""
    own = sorted(
        (p for p in prints.values() if p.entry_id == entry_id),
        key=lambda p: p.start,
    )
    removed = [
        summary.print_id
        for summary in own[: max(0, len(own) - limits.keep_prints)]
        if summary.print_id != current
    ]
    total = sum(p.size for p in own if p.print_id not in removed)
    for summary in own:
        if total <= limits.max_bytes:
            break
        if summary.print_id == current or summary.print_id in removed:
            continue
        removed.append(summary.print_id)
        total -= summary.size
    for print_id in removed:
        shutil.rmtree(root / print_id, ignore_errors=True)
    return removed


def save_frame(  # noqa: PLR0913
    root: Path,
    prints: dict[str, PrintSummary],
    *,
    entry_id: str,
    task_id: str,
    file_name: str,
    jpeg: bytes,
    score: float,
    detections: int,
    now: float,
    limits: Limits,
) -> SaveResult:
    """
    File one frame under its print and enforce the limits.

    Runs in the executor on a snapshot of the summaries; the caller applies
    the returned changes. Files are written to ``.part`` and renamed.
    """
    root.mkdir(parents=True, exist_ok=True)
    own = sorted(
        (p for p in prints.values() if p.entry_id == entry_id), key=lambda p: p.end
    )
    print_id, replaced = _choose_print(root, own, prints, task_id, now)
    folder = root / print_id
    folder.mkdir(exist_ok=True)
    meta = _read_json(folder / INDEX)
    if not isinstance(meta, dict):
        meta = {"start": round(now, 1), "frames": []}
    frames: list[dict[str, Any]] = list(meta.get("frames") or [])

    stamp = time.strftime("%H%M%S", time.localtime(now))
    name = f"{stamp}_{round(score * 10000):05d}.jpg"
    # sizes are tracked as frames come and go, not by listing the folder
    previous = prints.get(replaced[0] if replaced else print_id)
    size = previous.size if previous is not None else _jpeg_bytes(folder)
    if any(fr.get("f") == name for fr in frames):
        size -= (folder / name).stat().st_size if (folder / name).is_file() else 0
        frames = [fr for fr in frames if fr.get("f") != name]
    _write_atomically(folder / name, jpeg)
    size += len(jpeg)
    frames.append({"t": round(now, 1), "f": name, "s": score, "n": detections})
    if len(frames) > limits.max_frames:
        frames, freed = _thin(folder, frames)
        size -= freed

    meta.update({"entry_id": entry_id, "end": round(now, 1), "frames": frames})
    if task_id:
        meta["task"] = task_id
    if file_name:
        meta["name"] = file_name
    _write_json(folder / INDEX, meta)

    summary = _summary(print_id, meta, max(0, size))
    working = {k: v for k, v in prints.items() if k not in replaced}
    working[print_id] = summary
    removed = [*replaced, *_prune(root, working, entry_id, print_id, limits)]
    return SaveResult(print_id, name, {print_id: summary}, removed)


def _delete_print(root: Path, print_id: str) -> None:
    shutil.rmtree(root / print_id, ignore_errors=True)


# --- capture (event loop) --------------------------------------------------


def _root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(*CACHE_DIR))


async def async_load(hass: HomeAssistant) -> None:
    """Learn which prints are stored, once per Home Assistant run."""
    if _STATE["loaded"]:
        return
    _STATE["loaded"] = True
    _PRINTS.update(await hass.async_add_executor_job(load_summaries, _root(hass)))


def snapshot_url(host: str) -> str:
    """Return the printer's JPEG snapshot URL."""
    return f"http://{host}:{CC2_VIDEO_PORT}{SNAPSHOT_PATH}"


async def _fetch_snapshot(hass: HomeAssistant, host: str) -> bytes | None:
    """GET one JPEG from the printer: one try, ``FETCH_TIMEOUT`` at most."""
    session = async_get_clientsession(hass)
    body = bytearray()
    try:
        async with session.get(
            snapshot_url(host), timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        ) as response:
            if response.status != HTTPStatus.OK:
                LOGGER.debug("Snapshot for a spaghetti frame: HTTP %s", response.status)
                return None
            async for chunk in response.content.iter_chunked(64 * 1024):
                body += chunk
                if len(body) > MAX_JPEG_BYTES:
                    LOGGER.debug("Snapshot for a spaghetti frame is too large")
                    return None
    except (aiohttp.ClientError, TimeoutError) as err:
        LOGGER.debug("Snapshot for a spaghetti frame failed: %r", err)
        return None
    if not body.startswith(_JPEG_SOI):
        LOGGER.debug("Snapshot for a spaghetti frame is not a JPEG")
        return None
    return bytes(body)


def _safe_task_id(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_ID.match(text) else ""


def _score(data: Mapping[str, Any]) -> float:
    for key in ("raw_score", "confidence"):
        try:
            return round(min(1.0, max(0.0, float(data[key]))), 4)
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def async_capture(  # noqa: PLR0913
    hass: HomeAssistant,
    *,
    entry_id: str,
    host: str,
    task_id: str,
    file_name: str,
    score: float,
    detections: int,
    limits: Limits,
    now: float | None = None,
) -> SaveResult | None:
    """Fetch the printer's snapshot and file it; None when nothing was saved."""
    jpeg = await _fetch_snapshot(hass, host)
    if jpeg is None:
        return None
    save = partial(
        save_frame,
        _root(hass),
        entry_id=entry_id,
        task_id=_safe_task_id(task_id),
        file_name=file_name[:200],
        jpeg=jpeg,
        score=score,
        detections=detections,
        now=time.time() if now is None else now,
        limits=limits,
    )
    async with _LOCK:
        try:
            result = await hass.async_add_executor_job(save, dict(_PRINTS))
        except OSError as err:
            LOGGER.warning("Could not save a spaghetti check frame: %s", err)
            return None
        for print_id in result.removed:
            _PRINTS.pop(print_id, None)
        _PRINTS.update(result.updated)
    for print_id in result.removed:
        if print_id not in result.updated:
            LOGGER.debug("Removed spaghetti frames of %s (limits)", print_id)
    return result


def owner_of(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """
    Return the capturing config entry a detector result is for.

    The printer whose host is in the detector's image URL, else the one that
    owns the detector's camera entity, else the only printer capturing.
    """
    if not _CAPTURING:
        return None
    host = urlsplit(str(data.get("image_url") or "")).hostname
    if host:
        for capture in _CAPTURING.values():
            if capture.host.lower() == host.lower():
                return capture.entry_id
    camera = data.get("camera")
    if isinstance(camera, str) and camera:
        entity = er.async_get(hass).async_get(camera)
        if entity is not None and entity.config_entry_id in _CAPTURING:
            return entity.config_entry_id
    if len(_CAPTURING) == 1:
        return next(iter(_CAPTURING))
    return None


async def _async_capture_event(
    hass: HomeAssistant, capture: _Capture, data: Mapping[str, Any]
) -> None:
    try:
        task_id, file_name = capture.current_job()
        await async_capture(
            hass,
            entry_id=capture.entry_id,
            host=capture.host,
            task_id=task_id,
            file_name=file_name,
            score=_score(data),
            detections=_count(data.get("detections")),
            limits=capture.limits,
        )
    finally:
        _BUSY.discard(capture.entry_id)


@callback
def async_handle_result(hass: HomeAssistant, event: Event) -> None:
    """Start one capture for a scheduled check, unless one is still running."""
    data = event.data
    if data.get("manual") is not False:
        return
    entry_id = owner_of(hass, data)
    if entry_id is None:
        return
    if entry_id in _BUSY:
        LOGGER.debug("Previous spaghetti frame still being fetched; skipping one")
        return
    _BUSY.add(entry_id)
    hass.async_create_background_task(
        _async_capture_event(hass, _CAPTURING[entry_id], data),
        name=f"elegoo_printer spaghetti frame {entry_id}",
    )


def _coordinator_job(entry: ConfigEntry) -> Callable[[], tuple[str, str]]:
    def current() -> tuple[str, str]:
        data = getattr(entry.runtime_data.coordinator, "data", None)
        info = getattr(getattr(data, "status", None), "print_info", None)
        return (
            str(getattr(info, "task_id", None) or ""),
            str(getattr(info, "filename", None) or ""),
        )

    return current


@callback
def async_setup_capture(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], None]:
    """Keep a frame per scheduled check for this printer; return the undo."""
    options = {**entry.data, **(entry.options or {})}
    host = entry.runtime_data.api.printer.connection_host
    if options.get(CONF_SPAGHETTI_FRAMES_ENABLED) is False or not host:
        return lambda: None
    _CAPTURING[entry.entry_id] = _Capture(
        entry_id=entry.entry_id,
        host=host,
        limits=limits_from_options(options),
        current_job=_coordinator_job(entry),
    )
    # one listener for every printer; owner_of picks the printer per event
    if _STATE.get("unsub") is None:
        _STATE["unsub"] = hass.bus.async_listen(
            EVENT_RESULT, partial(async_handle_result, hass)
        )

    @callback
    def undo() -> None:
        _CAPTURING.pop(entry.entry_id, None)
        if not _CAPTURING and (unsub := _STATE.get("unsub")) is not None:
            unsub()
            _STATE["unsub"] = None

    return undo


async def async_delete_print(hass: HomeAssistant, print_id: str) -> bool:
    """Delete a stored print (with its job); return whether there was one."""
    if not _SAFE_ID.match(print_id):
        return False
    async with _LOCK:
        known = _PRINTS.pop(print_id, None) is not None
        await hass.async_add_executor_job(_delete_print, _root(hass), print_id)
    return known


# --- what the dashboard and the media source read --------------------------


def prints_of(entry_id: str) -> list[PrintSummary]:
    """Return a printer's stored prints, newest first."""
    return sorted(
        (p for p in _PRINTS.values() if p.entry_id == entry_id),
        key=lambda p: p.start,
        reverse=True,
    )


def viewer_url(entry_id: str, print_id: str | None = None) -> str:
    """Return the viewer's URL, carrying the access token."""
    query = {"token": _TOKEN}
    if print_id:
        query["print"] = print_id
    return f"{VIEW_BASE}/{entry_id}/viewer?{urlencode(query)}"


def frame_url(entry_id: str, print_id: str, name: str, *, token: bool) -> str:
    """Return a frame's URL; without the token it needs a signed-in request."""
    url = f"{VIEW_BASE}/{entry_id}/{print_id}/{name}"
    return f"{url}?{urlencode({'token': _TOKEN})}" if token else url


def job_frames(entry_id: str, task_id: str) -> dict[str, Any]:
    """Frame details of one job for the Print History sensor."""
    summary = _PRINTS.get(task_id)
    if summary is None or summary.entry_id != entry_id or not summary.count:
        return {"frames": 0, "frames_max": None, "frames_url": None}
    return {
        "frames": summary.count,
        "frames_max": summary.max_score,
        "frames_url": viewer_url(entry_id, task_id),
    }


async def async_read_print(
    hass: HomeAssistant, entry_id: str, print_id: str
) -> dict[str, Any] | None:
    """Return one of a printer's stored prints' index, or None."""
    summary = _PRINTS.get(print_id)
    if summary is None or summary.entry_id != entry_id:
        return None
    return await hass.async_add_executor_job(read_print_index, _root(hass), print_id)


# --- views -----------------------------------------------------------------


def _authorized(request: web.Request) -> bool:
    """Allow a signed-in request (header or signed path) or the access token."""
    if request.get(KEY_AUTHENTICATED):
        return True
    token = request.query.get("token", "")
    return bool(token) and secrets.compare_digest(token, _TOKEN)


_NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_VIEWER_HEADERS = {
    **_NO_STORE,
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; connect-src 'self'; "
        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "frame-ancestors 'self'"
    ),
}


def _json(data: Any) -> web.Response:
    return web.Response(
        text=json.dumps(data, separators=(",", ":")),
        content_type="application/json",
        headers=_NO_STORE,
    )


async def _viewer_html(hass: HomeAssistant) -> str:
    if _STATE["viewer"] is None:
        _STATE["viewer"] = await hass.async_add_executor_job(
            partial(VIEWER_FILE.read_text, encoding="utf-8")
        )
    return _STATE["viewer"]


class ElegooFramesView(HomeAssistantView):
    """The viewer page and a printer's list of prints."""

    url = VIEW_BASE + "/{entry_id}/{name}"
    name = "api:elegoo_printer:frames"
    # checked in the handler: a signed-in request or the access token
    requires_auth = False

    async def get(
        self, request: web.Request, entry_id: str, name: str
    ) -> web.StreamResponse:
        """Return the viewer or the index of stored prints."""
        if not _authorized(request):
            return web.Response(status=401)
        if not _SAFE_ID.match(entry_id):
            return web.Response(status=404)
        if name == "viewer":
            return web.Response(
                text=await _viewer_html(request.app["hass"]),
                content_type="text/html",
                headers=_VIEWER_HEADERS,
            )
        if name == INDEX:
            return _json({"prints": [p.as_row() for p in prints_of(entry_id)]})
        return web.Response(status=404)


class ElegooFrameFileView(HomeAssistantView):
    """One print's index, and its frames."""

    url = VIEW_BASE + "/{entry_id}/{print_id}/{name}"
    name = "api:elegoo_printer:frames:file"
    requires_auth = False

    async def get(
        self, request: web.Request, entry_id: str, print_id: str, name: str
    ) -> web.StreamResponse:
        """Return the index or a JPEG of a stored print."""
        if not _authorized(request):
            return web.Response(status=401)
        summary = _PRINTS.get(print_id)
        if (
            not _SAFE_ID.match(print_id)
            or summary is None
            or summary.entry_id != entry_id
        ):
            return web.Response(status=404)
        hass = request.app["hass"]
        if name == INDEX:
            meta = await async_read_print(hass, entry_id, print_id)
            return web.Response(status=404) if meta is None else _json(meta)
        if not _FRAME_NAME.match(name):
            return web.Response(status=404)
        path = _root(hass) / print_id / name
        if not await hass.async_add_executor_job(path.is_file):
            return web.Response(status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )
