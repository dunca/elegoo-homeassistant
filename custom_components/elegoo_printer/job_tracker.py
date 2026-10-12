"""
What Home Assistant records about a Centauri Carbon 2 job while it runs.

The printer forgets a job's tray and filament within seconds of the job ending,
and it knows nothing about the energy the printer drew. While a job runs the
tracker remembers its task id, the tray and filament it prints from, and the
reading of an optional energy sensor (the printer's smart plug) at the start.
When the job ends it stores the energy used, its cost, the tray and the filament
with the job in the archive (``job_archive``), where the print history shows them.

The running job is kept in the archive's store, so a Home Assistant restart in
the middle of a print still records the whole job.

The CC2's print status flickers out of an active state and back at the start of
a job, so only COMPLETE or STOPPED ends a job straight away; IDLE ends it once it
has lasted ``IDLE_ENDS_JOB_SECONDS``, and a different task id ends it at once.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

from .sdcp.models.enums import ElegooPrintStatus

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .job_archive import JobArchive
    from .sdcp.models.printer import PrinterData

ENDED = frozenset({ElegooPrintStatus.COMPLETE, ElegooPrintStatus.STOPPED})
IDLE_ENDS_JOB_SECONDS = 60
# kWh per unit of the energy sensor's unit_of_measurement
_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_NOT_A_JOB = frozenset(
    {
        ElegooPrintStatus.IDLE,
        ElegooPrintStatus.STOPPED,
        ElegooPrintStatus.COMPLETE,
        ElegooPrintStatus.ERROR,
        ElegooPrintStatus.LOADING,
        ElegooPrintStatus.FILAMENT_UNLOADING,
        ElegooPrintStatus.AUTO_FEEDING,
    }
)
_TRACKERS: dict[str, JobTracker] = {}


def energy_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Return an energy sensor's reading in kWh, or None if it has none."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get("unit_of_measurement") or "kWh"
    factor = _TO_KWH.get(unit)
    return None if factor is None else value * factor


def used_kwh(start: float | None, end: float | None) -> float | None:
    """
    Energy used between two readings of a cumulative sensor.

    Plug counters reset (daily, monthly). A reading lower than the start means
    the counter restarted during the job; the energy since the reset is then the
    best figure available.
    """
    if start is None or end is None:
        return None
    used = end - start if end >= start else end
    return round(max(used, 0.0), 3)


def _tray_label(printer_data: PrinterData) -> tuple[str, str, str]:
    """Return (tray, filament name, colour) of the active Canvas tray, or blanks."""
    ams = getattr(printer_data, "ams_status", None)
    active = getattr(ams, "ams_current_enabled", None) if ams else None
    if not active:
        return "", "", ""
    tray_id = str(active.get("TrayId") or "")
    if not tray_id or tray_id.startswith("-"):
        return "", "", ""
    for box in getattr(ams, "ams_boxes", []):
        if box.id != active.get("AmsId"):
            continue
        for tray in box.tray_list:
            if tray.id == tray_id:
                try:
                    label = f"A{int(tray_id) + 1}"
                except ValueError:
                    label = tray_id
                return label, tray.filament_name or "", tray.filament_color or ""
    return "", "", ""


class JobTracker:
    """Follows the running job of one printer and records it when it ends."""

    def __init__(
        self,
        hass: HomeAssistant,
        archive: JobArchive,
        energy_entity: str | None,
        price_per_kwh: float | None,
    ) -> None:
        """Create a tracker writing into ``archive``."""
        self._hass = hass
        self._archive = archive
        self._energy_entity = energy_entity or None
        self._price = price_per_kwh if price_per_kwh and price_per_kwh > 0 else None
        self._idle_since: float | None = None

    @property
    def energy_configured(self) -> bool:
        """Whether an energy sensor is set in the options."""
        return self._energy_entity is not None

    def _currency(self) -> str | None:
        return getattr(self._hass.config, "currency", None)

    def _cost(self, kwh: float | None) -> float | None:
        if kwh is None or self._price is None:
            return None
        return round(kwh * self._price, 2)

    @callback
    def update(self, printer_data: PrinterData, now: float | None = None) -> None:
        """Feed one coordinator refresh."""
        now = time.time() if now is None else now
        info = printer_data.status.print_info
        status = info.status
        if status is None:
            return
        task_id = (info.task_id or "").strip()
        running = self._archive.running

        if running and task_id and task_id != running["task_id"]:
            # a new job started before the last one was seen to end
            self._finish(running, None, now)
            running = None

        if status in ENDED:
            self._idle_since = None
            if running:
                self._finish(running, status.name.lower(), now)
            return

        if status == ElegooPrintStatus.IDLE:
            if running:
                self._idle_since = self._idle_since or now
                if now - self._idle_since >= IDLE_ENDS_JOB_SECONDS:
                    self._finish(running, None, now)
            return
        self._idle_since = None

        if status in _NOT_A_JOB or not task_id:
            return
        tray, name, color = _tray_label(printer_data)
        if running is None:
            self._archive.set_running(
                {
                    "task_id": task_id,
                    "file_name": (info.filename or "").strip(),
                    "started": now,
                    "energy_start": energy_kwh(self._hass, self._energy_entity),
                    "tray": tray,
                    "filament_name": name,
                    "filament_color": color,
                }
            )
            return
        # keep the last tray seen; it can be blank while the printer is preheating
        if tray and (tray, name, color) != (
            running.get("tray"),
            running.get("filament_name"),
            running.get("filament_color"),
        ):
            self._archive.set_running(
                {
                    **running,
                    "tray": tray,
                    "filament_name": name,
                    "filament_color": color,
                }
            )

    def _finish(self, running: dict[str, Any], result: str | None, now: float) -> None:
        kwh = used_kwh(
            running.get("energy_start"), energy_kwh(self._hass, self._energy_entity)
        )
        self._archive.set_extras(
            running["task_id"],
            {
                "file_name": running.get("file_name") or "",
                "finished": round(now),
                "result": result,
                "energy_kwh": kwh,
                "cost": self._cost(kwh),
                "currency": self._currency() if kwh is not None else None,
                "tray": running.get("tray") or "",
                "filament_name": running.get("filament_name") or "",
                "filament_color": running.get("filament_color") or "",
            },
        )
        self._archive.set_running(None)
        self._idle_since = None

    def current_energy(self) -> float | None:
        """Energy of the running job so far, or None when no job runs."""
        running = self._archive.running
        if not running:
            return None
        return used_kwh(
            running.get("energy_start"), energy_kwh(self._hass, self._energy_entity)
        )

    def current_cost(self) -> float | None:
        """Cost of the running job so far."""
        return self._cost(self.current_energy())


def register_tracker(entry_id: str, tracker: JobTracker) -> None:
    """Make a config entry's tracker available to the coordinator and sensors."""
    _TRACKERS[entry_id] = tracker


def get_tracker(entry_id: str) -> JobTracker | None:
    """Return a config entry's tracker, if any."""
    return _TRACKERS.get(entry_id)


def unregister_tracker(entry_id: str) -> None:
    """Forget a config entry's tracker."""
    _TRACKERS.pop(entry_id, None)
