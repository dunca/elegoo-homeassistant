"""
Readable CC2 job names (``job_names``) and where they are shown.

The file names below are real ones from a Centauri Carbon 2's history: projects
saved in ElegooSlicer, CLI-sliced projects with ``resolved(...)``, Elegoo app
uploads (cloud ids), MakerWorld downloads (UUIDs) and plain names.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.elegoo_printer import cc2_extras
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list
from custom_components.elegoo_printer.job_names import (
    MAX_STATE,
    JobName,
    display_model,
    parse_job_name,
)
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.printer import Printer


@pytest.mark.parametrize(
    ("file_name", "model", "filament", "layer", "estimate"),
    [
        (
            "ECC2_0.4_DHT22-A-case_Elegoo PLA _0.2_1h43m.gcode",
            "DHT22-A-case",
            "Elegoo PLA",
            0.2,
            "1h43m",
        ),
        (
            "ECC2_0.4_BagClip2_6mm_Elegoo PLA _0.2_30m1s.gcode",
            "BagClip2 6mm",
            "Elegoo PLA",
            0.2,
            "30m1s",
        ),
        (
            "ECC2_0.4_trash-bag-holder-d60-l160+-+vase-nozzle-0.4v1"
            "_Elegoo PLA _0.2_2h4m.gcode",
            "trash-bag-holder-d60-l160 - vase-nozzle-0.4v1",
            "Elegoo PLA",
            0.2,
            "2h4m",
        ),
        (
            "ECC2_0.4_Elegoo Spool(2)_Elegoo PLA Basic _0.2_2h12m.gcode",
            "Elegoo Spool(2)",
            "Elegoo PLA Basic",
            0.2,
            "2h12m",
        ),
        (
            "ECC2_0.4_shelf_arm_CC2_PETG-HF_Elegoo PETG HF "
            "resolved(shelf_arm_CC2_PETG-HF.3mf)_0.24_1h46m.gcode",
            "shelf arm CC2 PETG-HF",
            "Elegoo PETG HF",
            0.24,
            "1h46m",
        ),
        (
            "ECC2_0.4_1234567890123456789_Elegoo PLA _0.2_9m51s.gcode",
            "Cloud upload",
            "Elegoo PLA",
            0.2,
            "9m51s",
        ),
        (
            "ECC2_0.4_0a1b2c3d-1111-4222-8333-444455556666(2)_Elegoo PLA _0.2_25m50s"
            ".gcode",
            "Upload 0a1b2c3d",
            "Elegoo PLA",
            0.2,
            "25m50s",
        ),
        (
            "ECC2_0.4_Mouse trap - CC2 PETG HF fast_Elegoo PETG HF _0.28_2h6m.gcode",
            "Mouse trap - CC2 PETG HF fast",
            "Elegoo PETG HF",
            0.28,
            "2h6m",
        ),
        (
            "ECC2_0.4_Screw+Gauge+by+Bambu+Lab_Elegoo PLA _0.2_39m46s.gcode",
            "Screw Gauge by Bambu Lab",
            "Elegoo PLA",
            0.2,
            "39m46s",
        ),
        (
            "ECC2_0.4_twizzers_Elegoo PLA _0.12_21m33s.GCODE",
            "twizzers",
            "Elegoo PLA",
            0.12,
            "21m33s",
        ),
        (
            "ECC2_0.4_clips_Professional Lab PETG _0.2_17m5s.gcode",
            "clips",
            "Professional Lab PETG",
            0.2,
            "17m5s",
        ),
    ],
)
def test_parse_real_cc2_file_names(
    file_name: str, model: str, filament: str, layer: float, estimate: str
) -> None:
    assert parse_job_name(file_name) == JobName(model, filament, layer, estimate)


def test_a_non_cc2_name_is_kept_as_the_model() -> None:
    assert parse_job_name("benchy.gcode") == JobName("benchy", "", None, "")
    assert parse_job_name("my_part.gcode").model == "my part"


def test_a_cc2_prefix_without_the_usual_tail_keeps_the_body() -> None:
    assert parse_job_name("ECC2_0.4_odd.gcode") == JobName("odd", "", None, "")


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_name_is_none(empty: str | None) -> None:
    assert parse_job_name(empty) is None


def test_display_joins_model_and_filament() -> None:
    assert JobName("Clip", "Elegoo PLA", 0.2, "10m").display == "Clip (Elegoo PLA)"
    assert JobName("Clip", "", None, "").display == "Clip"


def test_display_is_capped_for_a_state() -> None:
    long = JobName("x" * 400, "PLA", 0.2, "1m")
    assert len(long.display) == MAX_STATE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345678-aaaa-4bbb-8ccc-dddddddddddd", "Upload 12345678"),
        ("9876543210987654321", "Cloud upload"),
        ("12345", "12345"),
        ("a__b", "a b"),
        ("  spaced  ", "spaced"),
    ],
)
def test_display_model(raw: str, expected: str) -> None:
    assert display_model(raw) == expected


# --- where the names are shown ------------------------------------------------


def _coordinator(live_file: str = "") -> MagicMock:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    client.printer_data.print_tasks = parse_task_list(
        {
            "history_task_list": [
                {
                    "task_id": "1",
                    "task_name": "ECC2_0.4_Old part_Elegoo PLA _0.2_10m.gcode",
                    "begin_time": 100,
                    "end_time": 200,
                    "task_status": 1,
                }
            ]
        }
    )
    client.printer_data.status.print_info.filename = live_file
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.config_entry.entry_id = "NOARCHIVE"
    coordinator.data = client.printer_data
    return coordinator


def test_print_name_shows_the_job_printing_now() -> None:
    coordinator = _coordinator("ECC2_0.4_New part_Elegoo PETG HF _0.2_5m.gcode")
    sensor = cc2_extras.ElegooPrintNameSensor(coordinator)
    assert sensor.native_value == "New part (Elegoo PETG HF)"
    attrs = sensor.extra_state_attributes
    assert attrs["model"] == "New part"
    assert attrs["filament"] == "Elegoo PETG HF"
    assert attrs["layer_height"] == 0.2
    assert attrs["estimate"] == "5m"


def test_print_name_falls_back_to_the_last_job_when_idle() -> None:
    sensor = cc2_extras.ElegooPrintNameSensor(_coordinator(""))
    assert sensor.native_value == "Old part (Elegoo PLA)"


def test_print_name_unknown_without_any_job() -> None:
    coordinator = _coordinator("")
    coordinator.data.print_tasks = []
    sensor = cc2_extras.ElegooPrintNameSensor(coordinator)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_last_print_and_history_carry_model_and_filament() -> None:
    from custom_components.elegoo_printer import definitions  # noqa: PLC0415

    coordinator = _coordinator("")
    last = cc2_extras.ElegooLastPrintSensor(coordinator).extra_state_attributes
    assert last["model"] == "Old part"
    assert last["filament"] == "Elegoo PLA"
    job = definitions._print_history_attributes(
        SimpleNamespace(coordinator=coordinator)
    )["jobs"][0]
    assert job["model"] == "Old part"
    assert job["filament"] == "Elegoo PLA"
