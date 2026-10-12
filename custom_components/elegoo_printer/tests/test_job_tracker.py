"""
Energy, cost, tray and filament recorded per CC2 job (``job_tracker``).

These replace a Home Assistant automation that raced the ~18 s in which the
printer still reports a finished job's tray and filament, and a pair of
template sensors that diffed the plug's energy counter around each print.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.elegoo_printer import job_tracker
from custom_components.elegoo_printer.job_tracker import (
    IDLE_ENDS_JOB_SECONDS,
    JobTracker,
    energy_kwh,
    get_tracker,
    register_tracker,
    unregister_tracker,
    used_kwh,
)
from custom_components.elegoo_printer.sdcp.models.enums import ElegooPrintStatus
from custom_components.elegoo_printer.sdcp.models.printer import PrinterData

S = ElegooPrintStatus
PLUG = "sensor.plug_energy"


class FakeArchive:
    """Just the archive calls the tracker makes."""

    def __init__(self, running: dict[str, Any] | None = None) -> None:
        """Start with an optional running job."""
        self.running = running
        self.extras: dict[str, dict[str, Any]] = {}

    def set_running(self, running: dict[str, Any] | None) -> None:
        self.running = dict(running) if running else None

    def set_extras(self, key: str, extras: dict[str, Any]) -> None:
        self.extras[key] = extras


def _hass(reading: float | None = 10.0, unit: str = "kWh") -> MagicMock:
    hass = MagicMock()
    hass.config.currency = "RON"
    hass._reading = {"value": reading, "unit": unit}

    def get(entity_id: str) -> Any:
        if entity_id != PLUG or hass._reading["value"] is None:
            return None
        return SimpleNamespace(
            state=str(hass._reading["value"]),
            attributes={"unit_of_measurement": hass._reading["unit"]},
        )

    hass.states.get = get
    return hass


def _set_reading(hass: MagicMock, value: float | None) -> None:
    hass._reading["value"] = value


def _ams(tray_id: str | None, name: str = "PETG HF", color: str = "#000000") -> Any:
    trays = [
        SimpleNamespace(id=f"{i:02d}", filament_name=f"T{i}", filament_color="#FFFFFF")
        for i in range(4)
    ]
    if tray_id is not None:
        trays[int(tray_id)] = SimpleNamespace(
            id=tray_id, filament_name=name, filament_color=color
        )
    return SimpleNamespace(
        ams_current_enabled=(
            {"AmsId": "0", "TrayId": tray_id} if tray_id is not None else None
        ),
        ams_boxes=[SimpleNamespace(id="0", tray_list=trays)],
    )


def _data(
    status: ElegooPrintStatus | None,
    task_id: str = "T1",
    file_name: str = "ECC2_0.4_clip_Elegoo PETG HF _0.2_10m.gcode",
    tray_id: str | None = "02",
) -> PrinterData:
    data = PrinterData()
    data.status.print_info.status = status
    data.status.print_info.task_id = task_id
    data.status.print_info.filename = file_name
    data.ams_status = _ams(tray_id)
    return data


def _tracker(
    *,
    reading: float | None = 10.0,
    price: float | None = 1.16,
    energy_entity: str | None = PLUG,
    running: dict[str, Any] | None = None,
) -> tuple[JobTracker, FakeArchive, MagicMock]:
    hass = _hass(reading)
    archive = FakeArchive(running)
    return JobTracker(hass, archive, energy_entity, price), archive, hass


# --- energy_kwh -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2.5, "kWh", 2.5),
        (2500, "Wh", 2.5),
        (0.0025, "MWh", 2.5),
    ],
)
def test_energy_kwh_converts_units(value: float, unit: str, expected: float) -> None:
    hass = _hass(value, unit)
    assert energy_kwh(hass, PLUG) == pytest.approx(expected)


def test_energy_kwh_defaults_to_kwh_without_a_unit() -> None:
    hass = MagicMock()
    hass.states.get = lambda _e: SimpleNamespace(state="3", attributes={})
    assert energy_kwh(hass, PLUG) == 3.0


@pytest.mark.parametrize("state", ["unknown", "unavailable", "", "not a number"])
def test_energy_kwh_none_for_unusable_states(state: str) -> None:
    hass = MagicMock()
    hass.states.get = lambda _e: SimpleNamespace(
        state=state, attributes={"unit_of_measurement": "kWh"}
    )
    assert energy_kwh(hass, PLUG) is None


def test_energy_kwh_none_for_an_unknown_unit() -> None:
    hass = _hass(5, "J")
    assert energy_kwh(hass, PLUG) is None


def test_energy_kwh_none_without_entity_or_state() -> None:
    hass = _hass(5)
    assert energy_kwh(hass, None) is None
    assert energy_kwh(hass, "") is None
    assert energy_kwh(hass, "sensor.missing") is None


# --- used_kwh ---------------------------------------------------------------


def test_used_kwh_is_the_difference_rounded() -> None:
    assert used_kwh(10.0, 10.12345) == 0.123


def test_used_kwh_after_a_counter_reset_counts_since_the_reset() -> None:
    assert used_kwh(120.0, 0.3) == 0.3


def test_used_kwh_none_when_a_reading_is_missing() -> None:
    assert used_kwh(None, 1.0) is None
    assert used_kwh(1.0, None) is None


# --- tray label -------------------------------------------------------------


def test_tray_label_names_the_active_tray() -> None:
    data = _data(S.PRINTING, tray_id="02")
    assert job_tracker._tray_label(data) == ("A3", "PETG HF", "#000000")


def test_tray_label_blank_without_canvas_or_active_tray() -> None:
    data = _data(S.PRINTING, tray_id=None)
    assert job_tracker._tray_label(data) == ("", "", "")
    data.ams_status = None
    assert job_tracker._tray_label(data) == ("", "", "")


def test_tray_label_blank_for_the_idle_marker() -> None:
    data = _data(S.PRINTING)
    data.ams_status.ams_current_enabled["TrayId"] = "-1"
    assert job_tracker._tray_label(data) == ("", "", "")


def test_tray_label_blank_when_the_tray_is_not_listed() -> None:
    data = _data(S.PRINTING)
    data.ams_status.ams_current_enabled["AmsId"] = "9"
    assert job_tracker._tray_label(data) == ("", "", "")


# --- tracking a job ---------------------------------------------------------


def test_a_print_starting_is_remembered_with_its_tray_and_energy() -> None:
    tracker, archive, _ = _tracker(reading=10.0)
    tracker.update(_data(S.PREHEATING), now=1000)

    assert archive.running == {
        "task_id": "T1",
        "file_name": "ECC2_0.4_clip_Elegoo PETG HF _0.2_10m.gcode",
        "started": 1000,
        "energy_start": 10.0,
        "tray": "A3",
        "filament_name": "PETG HF",
        "filament_color": "#000000",
    }
    assert archive.extras == {}


def test_without_a_task_id_nothing_is_tracked() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING, task_id=""), now=1000)
    assert archive.running is None


@pytest.mark.parametrize(
    ("end", "result"), [(S.COMPLETE, "complete"), (S.STOPPED, "stopped")]
)
def test_a_finished_print_records_energy_cost_and_tray(
    end: ElegooPrintStatus, result: str
) -> None:
    tracker, archive, hass = _tracker(reading=10.0, price=1.16)
    tracker.update(_data(S.PRINTING), now=1000)
    _set_reading(hass, 10.5)
    tracker.update(_data(end), now=4600)

    assert archive.running is None
    assert archive.extras["T1"] == {
        "file_name": "ECC2_0.4_clip_Elegoo PETG HF _0.2_10m.gcode",
        "finished": 4600,
        "result": result,
        "energy_kwh": 0.5,
        "cost": 0.58,
        "currency": "RON",
        "tray": "A3",
        "filament_name": "PETG HF",
        "filament_color": "#000000",
    }


def test_the_start_of_job_idle_flicker_does_not_end_the_job() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING), now=1000)
    tracker.update(_data(S.IDLE), now=1005)
    tracker.update(_data(S.IDLE), now=1005 + IDLE_ENDS_JOB_SECONDS - 1)
    tracker.update(_data(S.PRINTING), now=1070)

    assert archive.running is not None
    assert archive.extras == {}


def test_idle_for_a_minute_ends_the_job_without_a_result() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING), now=1000)
    tracker.update(_data(S.IDLE), now=2000)
    tracker.update(_data(S.IDLE), now=2000 + IDLE_ENDS_JOB_SECONDS)

    assert archive.running is None
    assert archive.extras["T1"]["result"] is None
    assert archive.extras["T1"]["finished"] == 2060


def test_filament_changes_mid_print_do_not_end_the_job() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING), now=1000)
    for status in (S.LOADING, S.AUTO_FEEDING, S.FILAMENT_UNLOADING, S.PAUSED):
        tracker.update(_data(status), now=1100)
    assert archive.running is not None
    assert archive.extras == {}


def test_a_new_task_id_ends_the_previous_job_and_starts_the_next() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING, task_id="T1"), now=1000)
    tracker.update(_data(S.PRINTING, task_id="T2"), now=5000)

    assert archive.extras["T1"]["result"] is None
    assert archive.running["task_id"] == "T2"


def test_a_tray_seen_later_replaces_a_blank_one() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PREHEATING, tray_id=None), now=1000)
    assert archive.running["tray"] == ""
    tracker.update(_data(S.PRINTING, tray_id="00"), now=1100)
    assert archive.running["tray"] == "A1"
    tracker.update(_data(S.PRINTING, tray_id=None), now=1200)
    assert archive.running["tray"] == "A1"


def test_a_job_running_across_a_restart_is_still_recorded() -> None:
    running = {
        "task_id": "T1",
        "file_name": "f.gcode",
        "started": 1000,
        "energy_start": 20.0,
        "tray": "A2",
        "filament_name": "PLA",
        "filament_color": "#FFFFFF",
    }
    tracker, archive, _ = _tracker(reading=20.25, running=running)
    tracker.update(_data(S.COMPLETE), now=3000)

    assert archive.extras["T1"]["energy_kwh"] == 0.25
    assert archive.extras["T1"]["tray"] == "A2"


def test_without_an_energy_sensor_only_tray_and_filament_are_recorded() -> None:
    tracker, archive, _ = _tracker(energy_entity=None)
    assert tracker.energy_configured is False
    tracker.update(_data(S.PRINTING), now=1000)
    tracker.update(_data(S.COMPLETE), now=2000)

    extras = archive.extras["T1"]
    assert extras["energy_kwh"] is None
    assert extras["cost"] is None
    assert extras["currency"] is None
    assert extras["tray"] == "A3"


@pytest.mark.parametrize("price", [None, 0, -1])
def test_without_a_price_there_is_no_cost(price: float | None) -> None:
    tracker, archive, hass = _tracker(price=price)
    tracker.update(_data(S.PRINTING), now=1000)
    _set_reading(hass, 11.0)
    tracker.update(_data(S.COMPLETE), now=2000)
    assert archive.extras["T1"]["energy_kwh"] == 1.0
    assert archive.extras["T1"]["cost"] is None


def test_a_counter_reset_during_the_job_counts_from_the_reset() -> None:
    tracker, archive, hass = _tracker(reading=300.0)
    tracker.update(_data(S.PRINTING), now=1000)
    _set_reading(hass, 0.2)
    tracker.update(_data(S.COMPLETE), now=2000)
    assert archive.extras["T1"]["energy_kwh"] == 0.2


def test_an_unknown_status_is_ignored() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.PRINTING), now=1000)
    tracker.update(_data(None), now=1500)
    assert archive.running is not None


def test_an_ended_status_without_a_running_job_does_nothing() -> None:
    tracker, archive, _ = _tracker()
    tracker.update(_data(S.COMPLETE), now=1000)
    assert archive.extras == {}
    assert archive.running is None


def test_current_energy_and_cost_while_printing_and_none_otherwise() -> None:
    tracker, _archive, hass = _tracker(reading=10.0, price=2.0)
    assert tracker.current_energy() is None
    assert tracker.current_cost() is None
    tracker.update(_data(S.PRINTING), now=1000)
    _set_reading(hass, 10.4)
    assert tracker.current_energy() == 0.4
    assert tracker.current_cost() == 0.8


def test_registry_round_trip() -> None:
    tracker, _, _ = _tracker()
    register_tracker("E1", tracker)
    assert get_tracker("E1") is tracker
    unregister_tracker("E1")
    assert get_tracker("E1") is None
    unregister_tracker("E1")  # a second unregister is harmless
