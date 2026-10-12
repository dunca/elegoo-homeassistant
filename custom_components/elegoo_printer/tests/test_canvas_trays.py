"""
Per-tray CANVAS sensors (Tray A1-A4) and plain colour names.

The A1-A4 Name/Color sensors follow the slicer's filament slots during a job
with slot data, so they can show the job's filament for every tray. The Tray
sensors always describe the physical tray, and say ``Empty`` when it is.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.elegoo_printer import cc2_extras
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.filament_colors import color_name
from custom_components.elegoo_printer.sdcp.models.ams import AMSStatus
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.printer import Printer


@pytest.mark.parametrize(
    ("hex_color", "expected"),
    [
        ("#000000", "Black"),
        ("#FFFFFF", "White"),
        ("#ffffff", "White"),
        ("FFFFFF", "White"),
        ("#333333", "Dark grey"),
        ("#808080", "Grey"),
        ("#C0C0C0", "Light grey"),
        ("#FF0000", "Red"),
        ("#FB0207", "Red"),
        ("#FFB6C1", "Pink"),
        ("#8B4513", "Brown"),
        ("#FF8800", "Orange"),
        ("#FFD700", "Yellow"),
        ("#B8860B", "Gold"),
        ("#00FF00", "Green"),
        ("#00FFFF", "Cyan"),
        ("#0000FF", "Blue"),
        ("#800080", "Purple"),
        ("#FF1493", "Magenta"),
        ("#FFF5F5", "White"),
    ],
)
def test_color_name(hex_color: str, expected: str) -> None:
    assert color_name(hex_color) == expected


@pytest.mark.parametrize("bad", [None, "", "#FFF", "red", "#GGGGGG", "#1234567"])
def test_color_name_rejects_non_hex(bad: str | None) -> None:
    assert color_name(bad) is None


def _canvas(trays: list[dict[str, Any]], active_tray: int | None = 2) -> AMSStatus:
    return AMSStatus(
        {
            "active_canvas_id": 0,
            "active_tray_id": active_tray,
            "auto_refill": False,
            "canvas_list": [{"canvas_id": 0, "connected": 1, "tray_list": trays}],
        }
    )


TRAYS = [
    {
        "tray_id": 0,
        "brand": "ELEGOO",
        "filament_type": "PLA",
        "filament_name": "PLA",
        "filament_color": "#FFFFFF",
        "min_nozzle_temp": 190,
        "max_nozzle_temp": 230,
        "status": 1,
    },
    {
        "tray_id": 1,
        "brand": "ELEGOO",
        "filament_type": "PLA",
        "filament_name": "",
        "filament_color": "#000000",
        "status": 1,
    },
    {
        "tray_id": 2,
        "brand": "ELEGOO",
        "filament_type": "PETG",
        "filament_name": "PETG HF",
        "filament_color": "000000",
        "min_nozzle_temp": 230,
        "max_nozzle_temp": 260,
        "status": 1,
    },
    {
        "tray_id": 3,
        "brand": "Generic",
        "filament_type": "PETG",
        "filament_name": "PETG",
        "filament_color": "#FFFFFF",
        "status": 0,
    },
]


def _tray(index: int, ams: AMSStatus | None) -> Any:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    client.printer_data.ams_status = ams
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.data = client.printer_data
    return cc2_extras.ElegooCanvasTraySensor(coordinator, index)


def test_a_loaded_tray_names_colour_and_filament() -> None:
    sensor = _tray(0, _canvas(TRAYS))
    assert sensor.native_value == "White PLA"
    attrs = sensor.extra_state_attributes
    assert attrs["empty"] is False
    assert attrs["active"] is False
    assert attrs["filament"] == "PLA"
    assert attrs["type"] == "PLA"
    assert attrs["brand"] == "ELEGOO"
    assert attrs["color"] == "#FFFFFF"
    assert attrs["color_name"] == "White"
    assert attrs["nozzle_temp_min"] == 190
    assert attrs["nozzle_temp_max"] == 230


def test_the_active_tray_is_marked() -> None:
    sensor = _tray(2, _canvas(TRAYS, active_tray=2))
    assert sensor.native_value == "Black PETG HF"
    assert sensor.extra_state_attributes["active"] is True


def test_a_tray_without_a_name_uses_its_type() -> None:
    assert _tray(1, _canvas(TRAYS)).native_value == "Black PLA"


def test_an_empty_tray_says_empty_and_blanks_its_details() -> None:
    sensor = _tray(3, _canvas(TRAYS))
    assert sensor.native_value == "Empty"
    attrs = sensor.extra_state_attributes
    assert attrs["empty"] is True
    assert attrs["active"] is False
    assert attrs["filament"] is None
    assert attrs["color"] is None
    assert attrs["color_name"] is None


def test_a_tray_canvas_does_not_list_is_empty() -> None:
    sensor = _tray(3, _canvas(TRAYS[:2]))
    assert sensor.native_value == "Empty"
    assert sensor.extra_state_attributes == {}


def test_without_canvas_status_the_tray_is_unknown() -> None:
    sensor = _tray(0, None)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_a_tray_with_an_odd_colour_shows_the_filament_only() -> None:
    trays = [dict(TRAYS[0], filament_color="")]
    assert _tray(0, _canvas(trays)).native_value == "PLA"


def test_tray_sensors_have_their_own_ids() -> None:
    ids = {_tray(i, _canvas(TRAYS)).unique_id for i in range(4)}
    assert ids == {"x_tray_a1", "x_tray_a2", "x_tray_a3", "x_tray_a4"}
