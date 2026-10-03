"""
Every job a Centauri Carbon 2 has reported, kept beyond the 50 it remembers.

The printer's own history (method 1036) drops its oldest job once it holds 50.
Each refresh is merged into a Home Assistant store, together with the slicer
metadata of each job's file while that file is still on the printer, so a job
keeps its name, colours and grams after both the job and the file are gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from .cc2.timelapse import CC2PrintTask

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .sdcp.models.file_info import PrinterFile

STORAGE_VERSION = 1
SAVE_DELAY = 10  # seconds
_TASK_FIELDS = (
    "task_id",
    "file_name",
    "begin_time",
    "end_time",
    "task_status",
    "timelapse_status",
    "timelapse_url",
    "timelapse_size",
)
_ARCHIVES: dict[str, JobArchive] = {}


def file_meta(file: PrinterFile) -> dict[str, Any]:
    """Return the slicer metadata worth keeping from a file on the printer."""
    return {
        "filament_grams": round(file.filament_used, 2) or None,
        "filament_colors": [c for c in file.colors if c],
        "filament_materials": [m for m in file.materials if m],
        "estimated_seconds": file.print_time or None,
        "size": file.size or None,
    }


class JobArchive:
    """Jobs and file metadata of one printer, persisted in ``.storage``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the archive; call ``async_load`` before use."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"elegoo_printer.{entry_id}.jobs"
        )
        self._jobs: dict[str, dict[str, Any]] = {}
        self._files: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read what was archived before."""
        data = await self._store.async_load() or {}
        self._jobs = dict(data.get("jobs") or {})
        self._files = dict(data.get("files") or {})

    def _data(self) -> dict[str, Any]:
        return {"jobs": self._jobs, "files": self._files}

    @callback
    def merge(self, tasks: list[CC2PrintTask], files: dict[str, PrinterFile]) -> None:
        """Add or refresh jobs from the printer and metadata of files on it."""
        changed = False
        for task in tasks:
            row = {field: getattr(task, field) for field in _TASK_FIELDS}
            if self._jobs.get(task.task_id) != row:
                self._jobs[task.task_id] = row
                changed = True
            file = files.get(task.file_name)
            if file is not None:
                meta = file_meta(file)
                if self._files.get(task.file_name) != meta:
                    self._files[task.file_name] = meta
                    changed = True
        if changed:
            self._store.async_delay_save(self._data, SAVE_DELAY)

    def tasks(self) -> list[CC2PrintTask]:
        """Every archived job, oldest first."""
        rows = sorted(self._jobs.values(), key=lambda row: row.get("end_time", 0))
        return [CC2PrintTask(**{f: row[f] for f in _TASK_FIELDS}) for row in rows]

    def meta(self, file_name: str) -> dict[str, Any] | None:
        """Return the last metadata seen for a file, even after it left the printer."""
        return self._files.get(file_name)


async def async_setup_archive(hass: HomeAssistant, entry_id: str) -> JobArchive:
    """Load and register the archive of a config entry."""
    archive = JobArchive(hass, entry_id)
    await archive.async_load()
    _ARCHIVES[entry_id] = archive
    return archive


def get_archive(entry_id: str) -> JobArchive | None:
    """Return the loaded archive of a config entry, if any."""
    return _ARCHIVES.get(entry_id)
