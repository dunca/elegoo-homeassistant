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
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .cc2.client import ElegooCC2Client
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
