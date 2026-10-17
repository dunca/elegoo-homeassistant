"""
Slicer previews for every job in a Centauri Carbon 2's history.

The printer serves the preview PNG of any file still in its storage (method
1045). Each is fetched once, in the background, and kept under
``<config>/elegoo_printer/previews``, so a job keeps its picture after its file
is deleted from the printer. They are served the way Home Assistant serves
image entities: a plain URL carrying an access token that only signed-in users
see (it is in the Print History sensor's attributes), so an ``<img>`` on a
dashboard can load it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
import secrets
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import callback

from .const import LOGGER
from .sdcp.exceptions import PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cc2.client import ElegooCC2Client
    from .cc2.timelapse import CC2PrintTask

CACHE_DIR = ("elegoo_printer", "previews")
VIEW_URL = "/api/elegoo_printer/preview/{key}.png"
# A failed file is not asked for again until Home Assistant restarts.
_FAILED: set[str] = set()
_SAVED: set[str] = set()
# previews already taken from a gcode's big embedded thumbnail (not re-done)
_HIRES: set[str] = set()
_TOKEN = secrets.token_urlsafe(24)
_RUNNING: set[str] = set()


def preview_key(file_name: str) -> str:
    """Return the cache key of a file's preview."""
    return hashlib.sha256(file_name.encode()).hexdigest()[:24]


def preview_url(file_name: str) -> str | None:
    """Return the dashboard URL of a saved preview, or None."""
    key = preview_key(file_name)
    if key not in _SAVED:
        return None
    return VIEW_URL.format(key=key) + f"?token={_TOKEN}"


def forget_saved(hass: HomeAssistant, file_name: str) -> Path | None:
    """Drop a saved preview from the index; return its file to delete."""
    key = preview_key(file_name)
    if key not in _SAVED:
        return None
    _SAVED.discard(key)
    _HIRES.discard(key)
    return _directory(hass) / f"{key}.png"


def _directory(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(*CACHE_DIR))


def _saved_keys(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob("*.png")}


async def async_load_saved(hass: HomeAssistant) -> None:
    """Remember which previews are already saved, after a restart."""
    _SAVED.update(await hass.async_add_executor_job(_saved_keys, _directory(hass)))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_LEN = 24
_PNG_IHDR_OFFSET = 16
_SCREEN_THUMB_SIZE = 144
_THUMB_BEGIN = re.compile(rb"^;\s*thumbnail(?:_[A-Za-z0-9]+)? begin\s+(\d+)x(\d+)\b")


def extract_gcode_thumbnail(path: Path) -> bytes | None:
    """
    Return the largest embedded PNG thumbnail from a sliced gcode file.

    ElegooSlicer writes one ``; thumbnail begin WxH`` block per configured
    size near the top of the file, each a base64 PNG with every line prefixed
    ``; ``. The CC2's own screen uses the small 144x144 one; a larger size
    added to the slicer profile rides along for a sharper dashboard preview.
    Only the header is scanned, so this is cheap even on a big gcode.
    """
    best: bytes | None = None
    best_area = 0
    area = 0
    buf: list[bytes] = []
    collecting = False
    try:
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                m = _THUMB_BEGIN.match(line)
                if m:
                    area = int(m.group(1)) * int(m.group(2))
                    buf = []
                    collecting = True
                    continue
                if collecting and line.startswith(b"; thumbnail end"):
                    collecting = False
                    if area > best_area:
                        try:
                            png = base64.b64decode(b"".join(buf))
                        except (binascii.Error, ValueError):
                            png = b""
                        if png.startswith(_PNG_MAGIC):
                            best, best_area = png, area
                    continue
                if collecting and line.startswith(b";"):
                    buf.append(line[1:].strip())
                    continue
                # thumbnails sit in the header; stop once real gcode starts
                if not line.startswith(b";") and best is not None:
                    break
    except OSError:
        return None
    return best


def is_hires_png(png: bytes) -> bool:
    """Return True if png dimensions exceed the 144x144 touchscreen thumbnail."""
    if len(png) >= _PNG_HEADER_LEN and png.startswith(_PNG_MAGIC):
        w, h = struct.unpack(">II", png[_PNG_IHDR_OFFSET:_PNG_HEADER_LEN])
        return w > _SCREEN_THUMB_SIZE or h > _SCREEN_THUMB_SIZE
    return False


async def async_upgrade_from_gcode(
    hass: HomeAssistant, file_name: str, gcode_path: Path
) -> None:
    """Replace a job's preview with the big thumbnail baked into its gcode."""
    key = preview_key(file_name)
    if key in _HIRES:
        return
    png = await hass.async_add_executor_job(extract_gcode_thumbnail, gcode_path)
    if not png:
        return
    await hass.async_add_executor_job(_write, _directory(hass) / f"{key}.png", png)
    _SAVED.add(key)
    _HIRES.add(key)
    _FAILED.discard(key)
    LOGGER.debug(
        "Upgraded preview of %s from its gcode (%d bytes)", file_name, len(png)
    )


async def async_get_preview(hass: HomeAssistant, file_name: str) -> bytes | None:
    """Return saved preview PNG bytes for a file, upgrading from gcode."""
    key = preview_key(file_name)
    directory = _directory(hass)
    preview_file = directory / f"{key}.png"

    # If already confirmed high-res in this session, read and return it
    if key in _HIRES:
        try:
            return await hass.async_add_executor_job(preview_file.read_bytes)
        except OSError:
            pass

    # Check if a preview already exists on disk and is high-res (e.g. before restart)
    if key in _SAVED:
        try:
            data = await hass.async_add_executor_job(preview_file.read_bytes)
            if is_hires_png(data):
                _HIRES.add(key)
                return data
        except OSError:
            data = None
    else:
        data = None

    # Try upgrading from archived gcode
    from .gcode_archive import archive_path  # noqa: PLC0415

    gcode_file = archive_path(hass, file_name)
    if await hass.async_add_executor_job(gcode_file.is_file):
        await async_upgrade_from_gcode(hass, file_name, gcode_file)
        if key in _HIRES:
            try:
                return await hass.async_add_executor_job(preview_file.read_bytes)
            except OSError:
                pass

    return data


async def _fetch_all(
    hass: HomeAssistant, client: ElegooCC2Client, file_names: list[str]
) -> None:
    directory = _directory(hass)
    for name in file_names:
        key = preview_key(name)
        try:
            data = await client.get_file_thumbnail(name)
        except (*PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError):
            return  # printer went away; try again on the next refresh
        if data is None:
            _FAILED.add(key)
            continue
        await hass.async_add_executor_job(_write, directory / f"{key}.png", data)
        _SAVED.add(key)
        LOGGER.debug("Saved the preview of %s", name)
        # one MQTT request at a time, without crowding out status updates
        await asyncio.sleep(0.5)


def files_to_fetch(on_printer: set[str], tasks: list[CC2PrintTask]) -> list[str]:
    """Files still on the printer whose preview is not saved, newest job first."""
    wanted: list[str] = []
    for task in reversed(tasks):
        key = preview_key(task.file_name)
        if (
            task.file_name in on_printer
            and key not in _SAVED
            and key not in _FAILED
            and task.file_name not in wanted
        ):
            wanted.append(task.file_name)
    return wanted


@callback
def async_schedule_fetch(
    hass: HomeAssistant,
    entry_id: str,
    client: ElegooCC2Client,
    tasks: list[CC2PrintTask],
) -> None:
    """Fetch missing previews of files still on the printer, newest job first."""
    if entry_id in _RUNNING:
        return
    wanted = files_to_fetch(set(client.printer_data.file_list), tasks)
    if not wanted:
        return

    async def run() -> None:
        _RUNNING.add(entry_id)
        try:
            await _fetch_all(hass, client, wanted)
        finally:
            _RUNNING.discard(entry_id)

    hass.async_create_background_task(run(), name=f"elegoo_printer previews {entry_id}")


class ElegooPreviewView(HomeAssistantView):
    """Serve saved previews to requests carrying the access token."""

    url = VIEW_URL
    name = "api:elegoo_printer:preview"
    requires_auth = False

    async def get(self, request: web.Request, key: str) -> web.StreamResponse:
        """Return the PNG if the token matches and the preview exists."""
        token = request.query.get("token", "")
        if not secrets.compare_digest(token, _TOKEN) or key not in _SAVED:
            return web.Response(status=404)
        path = _directory(request.app["hass"]) / f"{key}.png"
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "image/png",
                "Cache-Control": "private, max-age=86400",
            },
        )
