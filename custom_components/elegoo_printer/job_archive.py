"""
Every job a Centauri Carbon 2 has reported, kept beyond the 50 it remembers.

The printer's own history (method 1036) drops its oldest job once it holds 50.
Each refresh is merged into a Home Assistant store, together with the slicer
metadata of each job's file while that file is still on the printer, so a job
keeps its name, colours and grams after both the job and the file are gone.

The store also keeps what only Home Assistant sees while a job runs (see
``job_tracker``): the energy it used, its cost, and the tray and filament it
printed from. The printer forgets those within seconds of the job ending.
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
# A job's extras are matched by task id; failing that, by file name and an end time
# this close to the moment the tracker saw the job finish.
EXTRAS_MATCH_SECONDS = 600


JOB_EXTRA_FIELDS = (
    "energy_kwh",
    "cost",
    "currency",
    "tray",
    "filament_name",
    "filament_color",
)


def job_extras(extras: dict[str, Any] | None) -> dict[str, Any]:
    """Return the per-job fields recorded by Home Assistant (energy, tray, ...)."""
    extras = extras or {}
    return {field: extras.get(field) or None for field in JOB_EXTRA_FIELDS}


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
        self._extras: dict[str, dict[str, Any]] = {}
        self._running: dict[str, Any] | None = None

    async def async_load(self) -> None:
        """Read what was archived before."""
        data = await self._store.async_load() or {}
        self._jobs = dict(data.get("jobs") or {})
        self._files = dict(data.get("files") or {})
        self._extras = dict(data.get("extras") or {})
        self._running = data.get("running") or None

    def _data(self) -> dict[str, Any]:
        return {
            "jobs": self._jobs,
            "files": self._files,
            "extras": self._extras,
            "running": self._running,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

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
            self._save()

    @callback
    def remove(self, task_id: str) -> CC2PrintTask | None:
        """Forget a job; return it, or None if it was not archived."""
        row = self._jobs.pop(task_id, None)
        if row is None:
            return None
        self._extras.pop(task_id, None)
        self._save()
        return CC2PrintTask(**{f: row[f] for f in _TASK_FIELDS})

    def uses_file(self, file_name: str) -> bool:
        """Whether any archived job printed this file."""
        return any(row.get("file_name") == file_name for row in self._jobs.values())

    def tasks(self) -> list[CC2PrintTask]:
        """Every archived job, oldest first."""
        rows = sorted(self._jobs.values(), key=lambda row: row.get("end_time", 0))
        return [CC2PrintTask(**{f: row[f] for f in _TASK_FIELDS}) for row in rows]

    def meta(self, file_name: str) -> dict[str, Any] | None:
        """Return the last metadata seen for a file, even after it left the printer."""
        return self._files.get(file_name)

    @callback
    def set_extras(self, key: str, extras: dict[str, Any]) -> None:
        """
        Store what Home Assistant recorded about a finished job.

        ``key`` is the job's task id. ``extras`` must carry ``file_name`` and
        ``finished`` (epoch seconds) so the job can still be matched if the
        printer's job list reports it under another id.
        """
        if self._extras.get(key) != extras:
            self._extras[key] = dict(extras)
            self._save()

    def extras(self, task: CC2PrintTask) -> dict[str, Any] | None:
        """Return a job's recorded extras: by task id, else by file and end time."""
        if found := self._extras.get(task.task_id):
            return found
        best: dict[str, Any] | None = None
        best_gap = EXTRAS_MATCH_SECONDS + 1
        for extra in self._extras.values():
            if extra.get("file_name") != task.file_name:
                continue
            gap = abs(float(extra.get("finished") or 0) - float(task.end_time or 0))
            if gap <= EXTRAS_MATCH_SECONDS and gap < best_gap:
                best, best_gap = extra, gap
        return best

    def last_extras(self) -> dict[str, Any] | None:
        """Return the extras of the job that finished most recently."""
        if not self._extras:
            return None
        return max(self._extras.values(), key=lambda e: float(e.get("finished") or 0))

    @property
    def running(self) -> dict[str, Any] | None:
        """The job being tracked right now, kept across Home Assistant restarts."""
        return self._running

    @callback
    def set_running(self, running: dict[str, Any] | None) -> None:
        """Remember (or clear) the job being tracked."""
        if self._running != running:
            self._running = dict(running) if running else None
            self._save()


async def async_setup_archive(hass: HomeAssistant, entry_id: str) -> JobArchive:
    """Load and register the archive of a config entry."""
    archive = JobArchive(hass, entry_id)
    await archive.async_load()
    _ARCHIVES[entry_id] = archive
    return archive


def get_archive(entry_id: str) -> JobArchive | None:
    """Return the loaded archive of a config entry, if any."""
    return _ARCHIVES.get(entry_id)
