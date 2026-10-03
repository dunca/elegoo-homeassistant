"""
Centauri Carbon 2 entities for status the firmware reports but was not mapped.

Read from the raw status frame (method 1002, kept current by the printer's
delta pushes), the disk info (1048) and the CANVAS state (2005), measured on
firmware 02.01.00.00:

* ``machine_status.exception_status`` - list of active fault codes, empty
  when there is none; ``sub_status_reason_code`` says why the printer is in
  its current sub-state.
* ``extruder.filament_detected`` / ``filament_detect_enable`` - the runout
  sensor at the toolhead and whether runout detection is on. With CANVAS the
  filament is pulled back when idle, so "no filament" is normal between
  prints; it only means a runout while printing.
* ``internal`` / ``usb`` ``total_bytes`` and ``used_bytes`` from 1048.
* ``canvas_info.auto_refill`` - CANVAS switches to a matching tray when one
  runs out; set with method 2004 ``{"auto_refill": bool}``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import (
    PERCENTAGE,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
    UnitOfInformation,
)
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from . import timelapse_media
from .cc2.client import ElegooCC2Client
from .const import CONF_POWER_SWITCH, DEFAULT_POWER_SWITCH
from .entity import ElegooPrinterEntity
from .sdcp.exceptions import PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError

if TYPE_CHECKING:
    from .coordinator import ElegooDataUpdateCoordinator

MB = 1024 * 1024


def cc2_client(coordinator: ElegooDataUpdateCoordinator) -> ElegooCC2Client | None:
    """Return the CC2 client behind a coordinator, if it is one."""
    client = coordinator.config_entry.runtime_data.api.client
    return client if isinstance(client, ElegooCC2Client) else None


class _CC2Entity(ElegooPrinterEntity):
    """Base for the entities here: a key, a name and the CC2 client."""

    def __init__(
        self, coordinator: ElegooDataUpdateCoordinator, key: str, name: str, icon: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.generate_unique_id(key)
        self._attr_name = name
        self._attr_icon = icon

    @property
    def _client(self) -> ElegooCC2Client | None:
        return cc2_client(self.coordinator)

    def _frame(self, *path: str) -> Any:
        client = self._client
        value: Any = client.status_frame if client else {}
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value


class ElegooFaultsSensor(_CC2Entity, SensorEntity):
    """Number of active faults, with the codes as an attribute."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "faults", "Faults", "mdi:alert-octagon-outline")

    @property
    def native_value(self) -> int | None:
        """Return how many faults the printer reports."""
        codes = self._frame("machine_status", "exception_status")
        return len(codes) if isinstance(codes, list) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the fault codes and the current sub-state and its reason."""
        codes = self._frame("machine_status", "exception_status")
        return {
            "codes": codes if isinstance(codes, list) else [],
            "sub_status": self._frame("machine_status", "sub_status"),
            "reason_code": self._frame("machine_status", "sub_status_reason_code"),
        }


# What the printer is doing, by sub-status code (machine_status.sub_status).
# Codes from Elegoo's elegoo-link CC2 adapter; 1066 seen between nozzle
# heating and leveling on 3 Oct 2026, meaning not published.
STAGE_NAMES: dict[int, str] = {
    1405: "Heating bed",
    1906: "Heating bed",
    1045: "Heating nozzle",
    1096: "Heating nozzle",
    2801: "Homing",
    2802: "Homing",
    2901: "Auto-leveling",
    2902: "Auto-leveling",
    2075: "Printing",
    2077: "Complete",
    2501: "Pausing",
    2502: "Paused",
    2505: "Paused",
    2401: "Resuming",
    2402: "Resuming",
    2503: "Stopping",
    2504: "Stopped",
    1133: "Loading filament",
    1134: "Loading filament",
    1135: "Loading filament",
    1136: "Loading filament",
    1061: "Loading filament",
    1063: "Loading filament",
    1144: "Unloading filament",
    1145: "Unloading filament",
    1062: "Unloading filament",
    1064: "Unloading filament",
    1503: "PID calibration",
    1504: "PID calibration",
    5934: "Resonance test",
    3000: "Receiving file",
    3001: "Receiving file",
}
# machine_status.status values that mean "busy with a print job"
_PRINT_JOB = 2


class ElegooStageSensor(_CC2Entity, SensorEntity):
    """The step the printer is on, including the ones before a print starts."""

    # big, and only there to be read once: kept out of the recorder
    _unrecorded_attributes = frozenset({"leveling_frames"})

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "stage", "Stage", "mdi:progress-wrench")

    @property
    def native_value(self) -> str | None:
        """Return the current step in words."""
        code = self._frame("machine_status", "sub_status")
        status = self._frame("machine_status", "status")
        if code is None and status is None:
            return None
        if code in STAGE_NAMES:
            return STAGE_NAMES[code]
        if status == _PRINT_JOB:
            layer = self._frame("print_status", "current_layer")
            return "Printing" if layer else "Preparing"
        return "Idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw codes behind the step."""
        client = self._client
        return {
            "code": self._frame("machine_status", "sub_status"),
            "machine_status": self._frame("machine_status", "status"),
            "leveling_frames": client.leveling_frames if client else {},
        }


class ElegooTimelapseStorageSensor(_CC2Entity, SensorEntity):
    """How much of Home Assistant's 10 GB timelapse folder is in use."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(
            coordinator, "timelapse_storage", "Timelapse storage", "mdi:filmstrip-box"
        )

    @property
    def available(self) -> bool:
        """The folder is on Home Assistant, so it is there with the printer off."""
        return True

    @property
    def native_value(self) -> float:
        """Return the gigabytes the saved timelapses take."""
        return timelapse_media.usage()["used_gb"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the limit, how full it is, and how many videos are kept."""
        info = timelapse_media.usage()
        return {k: info[k] for k in ("used_mb", "limit_gb", "percent", "count")}


class ElegooStorageSensor(_CC2Entity, SensorEntity):
    """How full the printer's internal storage is."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "storage_used", "Storage used", "mdi:harddisk")

    def _disk(self, name: str) -> dict[str, Any]:
        info = getattr(self.coordinator.data, "disk_info", None) or {}
        disk = info.get(name)
        return disk if isinstance(disk, dict) else {}

    @property
    def native_value(self) -> float | None:
        """Return the used share of the internal storage."""
        disk = self._disk("internal")
        total, used = disk.get("total_bytes"), disk.get("used_bytes")
        if not total or used is None:
            return None
        return round(used / total * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return sizes in MB, for the internal storage and any USB stick."""
        attrs: dict[str, Any] = {}
        for name in ("internal", "usb"):
            disk = self._disk(name)
            if disk.get("total_bytes"):
                total = disk["total_bytes"] / MB
                used = (disk.get("used_bytes") or 0) / MB
                attrs[f"{name}_total_mb"] = round(total)
                attrs[f"{name}_used_mb"] = round(used)
                attrs[f"{name}_free_mb"] = round(total - used)
        attrs["usb_inserted"] = bool(self._frame("external_device", "u_disk"))
        return attrs


class ElegooFilamentBinarySensor(_CC2Entity, BinarySensorEntity):
    """Whether filament is at the toolhead (the runout sensor)."""

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(
            coordinator,
            "filament_at_toolhead",
            "Filament at toolhead",
            "mdi:printer-3d-nozzle",
        )

    @property
    def is_on(self) -> bool | None:
        """Return True while the runout sensor sees filament."""
        value = self._frame("extruder", "filament_detected")
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether runout detection is switched on."""
        enabled = self._frame("extruder", "filament_detect_enable")
        return {"detection_enabled": None if enabled is None else bool(enabled)}


class ElegooAutoRefillSwitch(_CC2Entity, SwitchEntity):
    """
    CANVAS auto-refill: carry on from a matching tray when one runs out.

    The printer applies 2004 at once but keeps reporting the old value in its
    CANVAS status for up to a couple of minutes (measured: 1 min 55 s), so the
    requested value is shown until the printer agrees or ``PENDING_FOR`` ends.
    """

    _attr_entity_category = EntityCategory.CONFIG
    PENDING_FOR = 180  # seconds

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the switch."""
        super().__init__(
            coordinator, "canvas_auto_refill", "CANVAS auto-refill", "mdi:autorenew"
        )
        self._pending: tuple[bool, float] | None = None

    @property
    def is_on(self) -> bool | None:
        """Return the auto-refill setting, or the one just requested."""
        ams = getattr(self.coordinator.data, "ams_status", None)
        reported = None if ams is None else bool(ams.auto_refill)
        if self._pending is not None:
            wanted, since = self._pending
            if reported == wanted or time.monotonic() - since > self.PENDING_FOR:
                self._pending = None
            else:
                return wanted
        return reported

    async def _set(self, *, enabled: bool) -> None:
        client = self._client
        if client is None:
            msg = "Not a Centauri Carbon 2"
            raise HomeAssistantError(msg)
        try:
            await client.set_auto_refill(enabled=enabled)
        except (*PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError) as err:
            msg = f"Could not change auto-refill: {err}"
            raise HomeAssistantError(msg) from err
        self._pending = (enabled, time.monotonic())
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Turn auto-refill on."""
        await self._set(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Turn auto-refill off."""
        await self._set(enabled=False)


class ElegooConnectedBinarySensor(_CC2Entity, BinarySensorEntity):
    """
    Whether the printer is talking to Home Assistant right now.

    Always available, so a powered-off printer reads "off" rather than
    "unavailable" - the point of a connectivity sensor. Replaces the
    ``sdcp_status`` entity, which is hard-wired to on for the CC2.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "connected", "Connected", "mdi:lan-connect")

    @property
    def available(self) -> bool:
        """Stay available: being offline is the state, not a failure."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the MQTT session is up and polls succeed."""
        client = self._client
        return bool(
            client is not None
            and client.is_connected
            and self.coordinator.last_update_success
        )


class ElegooLastPrintSensor(_CC2Entity, SensorEntity):
    """
    The most recently finished job, from the printer's history and the archive.

    The printer blanks the live job fields the moment a print ends; this keeps
    the finished job's details (file, result, times, slicer filament figures)
    until the next one ends. The state is when it ended.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "last_print", "Last print", "mdi:history")

    def _task(self) -> Any:
        # imported here: definitions pulls in the timelapse and archive modules
        from .definitions import history_tasks  # noqa: PLC0415

        tasks, _ = history_tasks(self)
        finished = [t for t in tasks if t.end_time and t.result != "unknown"]
        return finished[-1] if finished else None

    @property
    def native_value(self) -> datetime | None:
        """Return when the last job ended."""
        task = self._task()
        return datetime.fromtimestamp(task.end_time, UTC) if task else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the job's details, with its file's slicer metadata if known."""
        # imported here: job_archive pulls in the store helper
        from .job_archive import file_meta, get_archive  # noqa: PLC0415

        task = self._task()
        if task is None:
            return {}
        files = getattr(self.coordinator.data, "file_list", None) or {}
        live = files.get(task.file_name)
        archive = get_archive(self.coordinator.config_entry.entry_id)
        meta = (
            file_meta(live)
            if live is not None
            else (archive.meta(task.file_name) if archive else None)
        ) or {}
        return {
            "task_id": task.task_id,
            "file": task.file_name,
            "result": task.result,
            "begin": datetime.fromtimestamp(task.begin_time, UTC).isoformat(),
            "end": datetime.fromtimestamp(task.end_time, UTC).isoformat(),
            "duration_seconds": max(0, task.end_time - task.begin_time),
            "filament_grams": meta.get("filament_grams"),
            "filament_colors": meta.get("filament_colors") or [],
            "filament_materials": meta.get("filament_materials") or [],
            "timelapse": task.timelapse,
        }


class ElegooOnlineSinceSensor(_CC2Entity, RestoreEntity, SensorEntity):
    """
    When the printer last came online after being off, for an uptime display.

    The CC2 reports no uptime. Two things restart it, both anchored to the
    moment the printer is reachable again (never to mere power-on, which leads
    the boot by a minute or two): a configured mains switch going off -> on
    (an unambiguous power-cycle; see ``CONF_POWER_SWITCH``), or - as a fallback
    for outages that do not cut that switch - a reconnect after at least
    ``MIN_OFFLINE`` continuously offline. Neither an integration reload nor a
    Home Assistant restart resets it; the value is restored across restarts. A
    first install takes over ``input_datetime.cc2_powered_on`` if it exists
    (where an automation used to keep this), otherwise starts from now.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # Seconds offline before a reconnect counts as a real power-on. The CC2's
    # link self-drops for up to ~50 s at a time; while offline the coordinator
    # polls every 30 s, so a flap can be measured as up to ~75 s. 90 clears that
    # with margin yet still catches any genuine disconnect of ~1.5 min or more.
    MIN_OFFLINE = 90
    LEGACY_HELPER = "input_datetime.cc2_powered_on"

    def __init__(self, coordinator: ElegooDataUpdateCoordinator) -> None:
        """Create the sensor."""
        super().__init__(
            coordinator, "online_since", "Online since", "mdi:timer-outline"
        )
        self._since: datetime | None = None
        self._offline_at: float | None = None
        # Set when the mains switch is seen powering on; consumed the moment the
        # printer next reports reachable, so uptime excludes the boot delay.
        self._reboot_pending: bool = False

    @property
    def available(self) -> bool:
        """Stay available, so the last power-on survives the printer going off."""
        return True

    def _connected(self) -> bool:
        client = self._client
        return bool(
            client is not None
            and client.is_connected
            and self.coordinator.last_update_success
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last power-on, or take over the legacy helper."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            try:
                self._since = datetime.fromisoformat(last.state)
            except ValueError:
                self._since = None
        if self._since is None:
            legacy = self.hass.states.get(self.LEGACY_HELPER)
            ts = legacy.attributes.get("timestamp") if legacy else None
            self._since = datetime.fromtimestamp(ts, UTC) if ts else datetime.now(UTC)
        if not self._connected():
            self._offline_at = time.monotonic()
        options = getattr(self.coordinator.config_entry, "options", None) or {}
        switch = options.get(CONF_POWER_SWITCH, DEFAULT_POWER_SWITCH)
        if switch:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [switch], self._power_switch_event
                )
            )

    @callback
    def _power_switch_event(self, event: Event) -> None:
        """Note a real power-cycle: the mains switch going off -> on."""
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        # Only an explicit off -> on. unavailable -> on is the plug's own comms
        # recovering (it drops to unavailable after outages) and does not mean
        # the printer rebooted; the MIN_OFFLINE fallback still covers that.
        off_then_on = (
            old is not None
            and old.state == STATE_OFF
            and new is not None
            and new.state == STATE_ON
        )
        if off_then_on:
            self._reboot_pending = True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Start a new uptime when the printer returns after a real outage."""
        if self._connected():
            outage = (
                self._offline_at is not None
                and time.monotonic() - self._offline_at >= self.MIN_OFFLINE
            )
            if self._reboot_pending or outage:
                self._since = datetime.now(UTC)
            self._reboot_pending = False
            self._offline_at = None
        elif self._offline_at is None:
            self._offline_at = time.monotonic()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        """Return when the printer came online."""
        return self._since

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether it is online now."""
        return {"online": self._connected()}
