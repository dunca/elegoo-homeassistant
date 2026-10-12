"""
Filament state in words, and when the printer's current stage began.

These replace Home Assistant template helpers that combined the toolhead's
runout sensor, the stage and the print status by hand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.elegoo_printer import cc2_extras
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.sdcp.models.enums import (
    ElegooPrintStatus,
    PrinterType,
)
from custom_components.elegoo_printer.sdcp.models.printer import Printer

S = ElegooPrintStatus


def _sensor(  # noqa: PLR0913
    cls: type,
    *,
    detected: int | None,
    print_status: ElegooPrintStatus | None = S.IDLE,
    layer: int = 0,
    sub_status: int | None = None,
    machine_status: int | None = 1,
) -> Any:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    frame: dict[str, Any] = {"machine_status": {}, "extruder": {}}
    if detected is not None:
        frame["extruder"]["filament_detected"] = detected
    if sub_status is not None:
        frame["machine_status"]["sub_status"] = sub_status
    if machine_status is not None:
        frame["machine_status"]["status"] = machine_status
    client._cached_status = frame
    client.printer_data.status.print_info.status = print_status
    client.printer_data.status.print_info.current_layer = layer
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.data = client.printer_data
    return cls(coordinator)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"detected": 1, "print_status": S.PRINTING, "layer": 5}, "Loaded"),
        ({"detected": 1}, "Loaded"),
        ({"detected": 0, "sub_status": 1133}, "Changing filament"),
        ({"detected": 0, "sub_status": 1144}, "Changing filament"),
        ({"detected": 0, "print_status": S.PRINTING, "layer": 12}, "Ran out"),
        ({"detected": 0, "print_status": S.PAUSED}, "Ran out"),
        ({"detected": 0, "print_status": S.PAUSING}, "Ran out"),
        ({"detected": 0, "print_status": S.FILAMENT_UNLOAD_PAUSED}, "Ran out"),
        ({"detected": 0, "print_status": S.PRINTING, "layer": 0}, "Loading"),
        ({"detected": 0, "print_status": S.PREHEATING}, "Loading"),
        ({"detected": 0, "print_status": S.AUTO_FEEDING}, "Loading"),
        ({"detected": 0, "print_status": S.RESUMING}, "Loading"),
        ({"detected": 0, "print_status": S.IDLE}, "Back in CANVAS"),
        ({"detected": 0, "print_status": S.COMPLETE}, "Back in CANVAS"),
        ({"detected": 0, "print_status": None}, "Back in CANVAS"),
        ({"detected": None}, None),
    ],
)
def test_filament_state(kwargs: dict[str, Any], expected: str | None) -> None:
    sensor = _sensor(cc2_extras.ElegooFilamentStateSensor, **kwargs)
    assert sensor.native_value == expected


def test_stage_text_matches_the_stage_sensor() -> None:
    sensor = _sensor(cc2_extras.ElegooStageSensor, detected=1, sub_status=1405)
    assert cc2_extras.stage_text(sensor) == "Heating bed"
    assert sensor.native_value == "Heating bed"


def test_stage_since_marks_when_the_step_changed() -> None:
    sensor = _sensor(cc2_extras.ElegooStageSensor, detected=1, sub_status=1405)
    sensor.async_write_ha_state = MagicMock()
    sensor.hass = MagicMock()
    first = datetime(2026, 10, 12, 18, 0, tzinfo=cc2_extras.UTC)
    later = datetime(2026, 10, 12, 18, 5, tzinfo=cc2_extras.UTC)

    with patch.object(cc2_extras, "datetime") as fake:
        fake.now.return_value = first
        sensor._handle_coordinator_update()
        assert sensor.extra_state_attributes["since"] == first.isoformat()

        fake.now.return_value = later
        sensor._handle_coordinator_update()  # same step: since stays
        assert sensor.extra_state_attributes["since"] == first.isoformat()

        sensor.coordinator.config_entry.runtime_data.api.client._cached_status[
            "machine_status"
        ]["sub_status"] = 2801
        sensor._handle_coordinator_update()  # new step: since moves
        assert sensor.native_value == "Homing"
        assert sensor.extra_state_attributes["since"] == later.isoformat()


def test_stage_since_is_blank_before_any_update() -> None:
    sensor = _sensor(cc2_extras.ElegooStageSensor, detected=1, sub_status=1405)
    assert sensor.extra_state_attributes["since"] is None
