"""Switch platform for elegoo_printer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cc2_extras import ElegooAutoRefillSwitch, cc2_client

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import ElegooPrinterConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ElegooPrinterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches: CANVAS auto-refill on a Centauri Carbon 2 with CANVAS."""
    coordinator = entry.runtime_data.coordinator
    printer = entry.runtime_data.api.printer
    if cc2_client(coordinator) is not None and printer.has_canvas:
        async_add_entities([ElegooAutoRefillSwitch(coordinator)])
