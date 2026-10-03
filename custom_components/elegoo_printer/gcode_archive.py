"""
A copy of every printed G-code file, kept in Home Assistant.

Files are copied off the printer one at a time while it is idle (never during
a print or an upload), newest job first, into ``<config>/elegoo_printer/gcode``.
Once the folder passes ``MAX_BYTES`` the least recently copied files go first.
A kept file can be downloaded, and sent back to the printer to reprint a job
whose file was deleted from it (see the ``restore_gcode`` service).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.core import callback

from .cc2.timelapse import TimelapseDownloadError
from .const import LOGGER
from .sdcp.models.enums import ElegooPrintStatus

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cc2.client import ElegooCC2Client
    from .cc2.timelapse import CC2PrintTask

CACHE_DIR = ("elegoo_printer", "gcode")
MAX_BYTES = 4 * 1024**3
IDLE_STATES = {
    ElegooPrintStatus.IDLE,
    ElegooPrintStatus.COMPLETE,
    ElegooPrintStatus.STOPPED,
}
_SAVED: set[str] = set()
_FAILED: set[str] = set()
_RUNNING: set[str] = set()


def file_key(file_name: str) -> str:
    """Return the cache key of a file (same as its preview's)."""
    return hashlib.sha256(file_name.encode()).hexdigest()[:24]


def archive_path(hass: HomeAssistant, file_name: str) -> Path:
    """Where the copy of a file lives."""
    return Path(hass.config.path(*CACHE_DIR, f"{file_key(file_name)}.gcode"))


def is_archived(file_name: str) -> bool:
    """Whether Home Assistant holds a copy of the file."""
    return file_key(file_name) in _SAVED


def forget_saved(hass: HomeAssistant, file_name: str) -> Path | None:
    """Drop a kept copy from the index; return its file to delete."""
    key = file_key(file_name)
    if key not in _SAVED:
        return None
    _SAVED.discard(key)
    return archive_path(hass, file_name)


def _saved_keys(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.glob("*.gcode")}


async def async_load_saved(hass: HomeAssistant) -> None:
    """Remember which files are already kept, after a restart."""
    directory = Path(hass.config.path(*CACHE_DIR))
    _SAVED.update(await hass.async_add_executor_job(_saved_keys, directory))


def files_to_copy(on_printer: set[str], tasks: list[CC2PrintTask]) -> list[str]:
    """Files still on the printer that are not kept yet, newest job first."""
    wanted: list[str] = []
    for task in reversed(tasks):
        name = task.file_name
        key = file_key(name)
        if (
            name in on_printer
            and key not in _SAVED
            and key not in _FAILED
            and name not in wanted
        ):
            wanted.append(name)
    return wanted


def _prune(directory: Path, keep: Path) -> list[str]:
    """Delete the oldest copies until the folder fits ``MAX_BYTES``."""
    files = sorted(directory.glob("*.gcode"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed: list[str] = []
    for path in files:
        if total <= MAX_BYTES:
            break
        if path == keep:
            continue
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        removed.append(path.stem)
    return removed


async def _copy(hass: HomeAssistant, client: ElegooCC2Client, name: str) -> None:
    # imported here: file_download imports this module
    from .file_download import open_printer_file  # noqa: PLC0415

    path = archive_path(hass, name)
    partial = path.with_suffix(".part")
    await hass.async_add_executor_job(
        lambda: path.parent.mkdir(parents=True, exist_ok=True)
    )
    download = await open_printer_file(client, name)
    try:
        handle = await hass.async_add_executor_job(partial.open, "wb")
        try:
            async for data in download.iter_body():
                await hass.async_add_executor_job(handle.write, data)
        finally:
            await hass.async_add_executor_job(handle.close)
    finally:
        await download.close()
    await hass.async_add_executor_job(partial.replace, path)
    _SAVED.add(file_key(name))
    # now that the whole file is here, lift its preview to the big thumbnail
    # the slicer baked in (the printer's own API only serves the small one)
    from .job_previews import async_upgrade_from_gcode  # noqa: PLC0415

    await async_upgrade_from_gcode(hass, name, path)
    for key in await hass.async_add_executor_job(_prune, path.parent, path):
        _SAVED.discard(key)
    LOGGER.debug("Kept a copy of %s", name)


@callback
def async_schedule_copy(
    hass: HomeAssistant,
    entry_id: str,
    client: ElegooCC2Client,
    tasks: list[CC2PrintTask],
) -> None:
    """Copy new files off the printer in the background while it is idle."""
    if entry_id in _RUNNING or client.upload_lock.locked():
        return
    if client.printer_data.status.print_info.status not in IDLE_STATES:
        return
    wanted = files_to_copy(set(client.printer_data.file_list), tasks)
    if not wanted:
        return

    async def run() -> None:
        _RUNNING.add(entry_id)
        try:
            for name in wanted:
                status = client.printer_data.status.print_info.status
                if status not in IDLE_STATES or client.upload_lock.locked():
                    return  # a print started; carry on next time
                try:
                    await _copy(hass, client, name)
                except (TimelapseDownloadError, OSError) as err:
                    _FAILED.add(file_key(name))
                    LOGGER.info("Could not keep a copy of %s: %s", name, err)
        finally:
            _RUNNING.discard(entry_id)

    hass.async_create_background_task(run(), name=f"elegoo_printer gcode {entry_id}")
