"""
Per-job extras: kept in the job archive, shown in history, last print and sensors.

Covers the archive's extras/running store, how the print history and Last print
sensors show the extras, the Print energy / Print cost sensors, the energy
options on the CC2 options form, and the coordinator feeding the tracker.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

import custom_components.elegoo_printer as integration
from custom_components.elegoo_printer import cc2_extras, definitions, job_archive
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.timelapse import CC2PrintTask, parse_task_list
from custom_components.elegoo_printer.config_flow import ElegooOptionsFlowHandler
from custom_components.elegoo_printer.const import (
    CONF_ENERGY_PRICE,
    CONF_ENERGY_SENSOR,
)
from custom_components.elegoo_printer.coordinator import ElegooDataUpdateCoordinator
from custom_components.elegoo_printer.job_archive import JobArchive
from custom_components.elegoo_printer.job_tracker import (
    register_tracker,
    unregister_tracker,
)
from custom_components.elegoo_printer.sdcp.models.enums import (
    PrinterType,
    TransportType,
)
from custom_components.elegoo_printer.sdcp.models.printer import Printer

EXTRAS = {
    "file_name": "b.gcode",
    "finished": 700,
    "result": "complete",
    "energy_kwh": 0.42,
    "cost": 0.49,
    "currency": "RON",
    "tray": "A3",
    "filament_name": "PETG HF",
    "filament_color": "#000000",
}


def _archive() -> JobArchive:
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    with patch.object(job_archive, "Store", return_value=store):
        return JobArchive(MagicMock(), "E")


def _task(task_id: str = "2", file_name: str = "b.gcode", end: int = 700) -> Any:
    return CC2PrintTask(
        task_id=task_id,
        file_name=file_name,
        begin_time=300,
        end_time=end,
        task_status=1,
        timelapse_status=0,
        timelapse_url="",
        timelapse_size=0,
    )


# --- archive ----------------------------------------------------------------


def test_extras_found_by_task_id() -> None:
    archive = _archive()
    archive.set_extras("2", EXTRAS)
    assert archive.extras(_task("2")) == EXTRAS


def test_extras_fall_back_to_file_and_end_time() -> None:
    archive = _archive()
    archive.set_extras("other-id", {**EXTRAS, "finished": 760})
    assert archive.extras(_task("2", end=700)) == {**EXTRAS, "finished": 760}


def test_extras_fallback_picks_the_closest_end_time() -> None:
    archive = _archive()
    archive.set_extras("a", {**EXTRAS, "finished": 1000, "tray": "A1"})
    archive.set_extras("b", {**EXTRAS, "finished": 720, "tray": "A2"})
    assert archive.extras(_task("x", end=700))["tray"] == "A2"


def test_extras_fallback_needs_the_same_file_and_a_close_time() -> None:
    archive = _archive()
    archive.set_extras("a", {**EXTRAS, "finished": 700 + 601})
    archive.set_extras("b", {**EXTRAS, "file_name": "other.gcode"})
    assert archive.extras(_task("x", end=700)) is None


def test_set_extras_saves_only_on_change() -> None:
    archive = _archive()
    archive.set_extras("2", EXTRAS)
    archive.set_extras("2", dict(EXTRAS))
    assert archive._store.async_delay_save.call_count == 1


def test_removing_a_job_drops_its_extras() -> None:
    archive = _archive()
    archive.merge([_task("2")], {})
    archive.set_extras("2", EXTRAS)
    archive.remove("2")
    assert archive.extras(_task("2")) is None


def test_last_extras_is_the_latest_finished() -> None:
    archive = _archive()
    assert archive.last_extras() is None
    archive.set_extras("a", {**EXTRAS, "finished": 500})
    archive.set_extras("b", {**EXTRAS, "finished": 900, "tray": "A4"})
    assert archive.last_extras()["tray"] == "A4"


def test_running_job_round_trips_through_the_store() -> None:
    archive = _archive()
    archive.set_running({"task_id": "T", "energy_start": 1.0})
    assert archive._data()["running"] == {"task_id": "T", "energy_start": 1.0}
    archive.set_running(None)
    assert archive.running is None


def test_load_restores_extras_and_running() -> None:
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={
            "jobs": {},
            "files": {},
            "extras": {"2": EXTRAS},
            "running": {"task_id": "T"},
        }
    )
    with patch.object(job_archive, "Store", return_value=store):
        archive = JobArchive(MagicMock(), "E")
    asyncio.run(archive.async_load())
    assert archive.extras(_task("2")) == EXTRAS
    assert archive.running == {"task_id": "T"}


def test_an_old_store_without_extras_still_loads() -> None:
    store = MagicMock()
    store.async_load = AsyncMock(return_value={"jobs": {}, "files": {}})
    with patch.object(job_archive, "Store", return_value=store):
        archive = JobArchive(MagicMock(), "E")
    asyncio.run(archive.async_load())
    assert archive.last_extras() is None
    assert archive.running is None


# --- job_extras / history / last print -----------------------------------------


def test_job_extras_maps_blanks_to_none() -> None:
    assert job_archive.job_extras(None) == dict.fromkeys(job_archive.JOB_EXTRA_FIELDS)
    assert job_archive.job_extras({**EXTRAS, "tray": ""})["tray"] is None
    assert job_archive.job_extras(EXTRAS)["energy_kwh"] == 0.42


def _coordinator() -> tuple[MagicMock, ElegooCC2Client]:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.config_entry.entry_id = "E"
    coordinator.hass.config.currency = "RON"
    coordinator.data = client.printer_data
    client.printer_data.print_tasks = parse_task_list(
        {
            "history_task_list": [
                {
                    "task_id": "2",
                    "task_name": "b.gcode",
                    "begin_time": 300,
                    "end_time": 700,
                    "task_status": 1,
                }
            ]
        }
    )
    return coordinator, client


@pytest.fixture
def archive_with_extras() -> Any:
    archive = _archive()
    archive.set_extras("2", EXTRAS)
    job_archive._ARCHIVES["E"] = archive
    yield archive
    job_archive._ARCHIVES.pop("E", None)


@pytest.mark.usefixtures("archive_with_extras")
def test_last_print_shows_the_extras() -> None:
    coordinator, _ = _coordinator()
    attrs = cc2_extras.ElegooLastPrintSensor(coordinator).extra_state_attributes
    assert attrs["energy_kwh"] == 0.42
    assert attrs["cost"] == 0.49
    assert attrs["currency"] == "RON"
    assert attrs["tray"] == "A3"
    assert attrs["filament_name"] == "PETG HF"


@pytest.mark.usefixtures("archive_with_extras")
def test_history_jobs_carry_minutes_and_extras() -> None:
    coordinator, _ = _coordinator()
    entity = SimpleNamespace(coordinator=coordinator)
    jobs = definitions._print_history_attributes(entity)["jobs"]
    assert jobs[0]["minutes"] == round(400 / 60, 1)
    assert jobs[0]["energy_kwh"] == 0.42
    assert jobs[0]["tray"] == "A3"


def test_history_jobs_without_extras_have_blank_fields() -> None:
    archive = _archive()
    job_archive._ARCHIVES["E"] = archive
    try:
        coordinator, _ = _coordinator()
        entity = SimpleNamespace(coordinator=coordinator)
        job = definitions._print_history_attributes(entity)["jobs"][0]
        assert job["energy_kwh"] is None
        assert job["tray"] is None
    finally:
        job_archive._ARCHIVES.pop("E", None)


# --- Print energy / Print cost sensors ------------------------------------------


class _Tracker:
    def __init__(self, energy: float | None, cost: float | None) -> None:
        self._energy, self._cost = energy, cost
        self.energy_configured = True

    def current_energy(self) -> float | None:
        return self._energy

    def current_cost(self) -> float | None:
        return self._cost


@pytest.fixture
def tracked(archive_with_extras: Any) -> Any:
    register_tracker("E", _Tracker(0.1, 0.12))
    yield archive_with_extras
    unregister_tracker("E")


def test_energy_and_cost_show_the_running_job(tracked: Any) -> None:
    tracked.set_running({"task_id": "T"})
    coordinator, _ = _coordinator()
    energy = cc2_extras.ElegooPrintEnergySensor(coordinator)
    cost = cc2_extras.ElegooPrintCostSensor(coordinator)
    assert energy.native_value == 0.1
    assert cost.native_value == 0.12
    assert energy.extra_state_attributes == {"printing": True}


@pytest.mark.usefixtures("tracked")
def test_energy_and_cost_show_the_last_job_when_idle() -> None:
    coordinator, _ = _coordinator()
    assert cc2_extras.ElegooPrintEnergySensor(coordinator).native_value == 0.42
    assert cc2_extras.ElegooPrintCostSensor(coordinator).native_value == 0.49
    attrs = cc2_extras.ElegooPrintEnergySensor(coordinator).extra_state_attributes
    assert attrs == {"printing": False}


@pytest.mark.usefixtures("tracked")
def test_cost_is_in_home_assistants_currency() -> None:
    coordinator, _ = _coordinator()
    sensor = cc2_extras.ElegooPrintCostSensor(coordinator)
    assert sensor.native_unit_of_measurement == "RON"


def test_energy_sensor_without_a_tracker_is_unknown() -> None:
    coordinator, _ = _coordinator()
    assert cc2_extras.ElegooPrintEnergySensor(coordinator).native_value is None


# --- options form -----------------------------------------------------------


def _options_flow() -> ElegooOptionsFlowHandler:
    entry = MagicMock()
    entry.data = {
        "name": "CC2 Unit Test",
        "ip_address": "192.0.2.1",
        "transport_type": "cc2_mqtt",
        "protocol_version": "CC2",
        "protocol": "CC2",
        "id": "board",
        "model": "Elegoo Centauri Carbon 2",
    }
    entry.options = {}
    flow = ElegooOptionsFlowHandler(entry)
    flow.hass = MagicMock()
    flow.flow_id = "options-test-flow"
    flow.handler = "elegoo_printer"
    return flow


def test_options_form_offers_energy_sensor_and_price() -> None:
    schema = dict(ElegooOptionsFlowHandler._cc2_options_schema())
    assert CONF_ENERGY_SENSOR in schema
    assert CONF_ENERGY_PRICE in schema


def test_energy_options_are_saved() -> None:
    async def run() -> dict:
        flow = _options_flow()
        return await flow.async_step_cc2_options(
            user_input={
                CONF_IP_ADDRESS: "192.0.2.1",
                CONF_ENERGY_SENSOR: "sensor.plug_energy",
                CONF_ENERGY_PRICE: 1.16,
            }
        )

    result = asyncio.run(run())
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENERGY_SENSOR] == "sensor.plug_energy"
    assert result["data"][CONF_ENERGY_PRICE] == 1.16


def test_cleared_energy_options_override_older_values() -> None:
    async def run() -> dict:
        flow = _options_flow()
        flow.config_entry.data = {
            **flow.config_entry.data,
            CONF_ENERGY_SENSOR: "sensor.old",
            CONF_ENERGY_PRICE: 2.0,
        }
        return await flow.async_step_cc2_options(
            user_input={CONF_IP_ADDRESS: "192.0.2.1"}
        )

    result = asyncio.run(run())
    assert result["data"][CONF_ENERGY_SENSOR] == ""
    assert result["data"][CONF_ENERGY_PRICE] == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.16, 1.16), ("0.5", 0.5), (0, None), (-1, None), (None, None), ("x", None)],
)
def test_price_option_parsing(value: Any, expected: float | None) -> None:
    assert integration._as_price(value) == expected


# --- coordinator feeds the tracker -------------------------------------------


def test_coordinator_feeds_each_replayed_status_then_the_live_one() -> None:
    seen: list[Any] = []
    tracker = MagicMock()
    tracker.update = lambda data: seen.append(data.status)
    register_tracker("E2", tracker)
    try:
        coordinator = object.__new__(ElegooDataUpdateCoordinator)
        coordinator.config_entry = MagicMock(entry_id="E2")
        api = coordinator.config_entry.runtime_data.api
        api.printer.transport_type = TransportType.CC2_MQTT
        live = SimpleNamespace(status="live")
        api.printer_data = live
        api.client.consume_print_status_transition_queue = lambda: ["s1", "s2"]
        coordinator.async_set_updated_data = MagicMock()
        coordinator._replay_cc2_print_status_transitions()
        coordinator._track_job(live)
    finally:
        unregister_tracker("E2")
    assert seen == ["s1", "s2", "live"]


def test_coordinator_without_a_tracker_is_fine() -> None:
    coordinator = object.__new__(ElegooDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace()  # no entry_id at all
    coordinator._track_job(SimpleNamespace())
