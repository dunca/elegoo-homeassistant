"""Tests for the CC2 faults, storage, runout and auto-refill entities."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from custom_components.elegoo_printer import cc2_extras
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.timelapse import parse_task_list
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.file_info import PrinterFile
from custom_components.elegoo_printer.sdcp.models.printer import Printer

FRAME = {
    "machine_status": {
        "exception_status": [1201, 1305],
        "sub_status": 2501,
        "sub_status_reason_code": 7,
    },
    "extruder": {"filament_detected": 0, "filament_detect_enable": 1},
    "external_device": {"u_disk": True},
}


def _coordinator() -> tuple[MagicMock, ElegooCC2Client]:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    client._cached_status = FRAME
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.data = client.printer_data
    return coordinator, client


def test_faults_sensor_reads_the_exception_list() -> None:
    coordinator, _ = _coordinator()
    sensor = cc2_extras.ElegooFaultsSensor(coordinator)
    assert sensor.native_value == 2
    assert sensor.extra_state_attributes == {
        "codes": [1201, 1305],
        "sub_status": 2501,
        "reason_code": 7,
    }


def test_storage_sensor_reports_percent_and_sizes() -> None:
    coordinator, client = _coordinator()
    client.printer_data.disk_info = {
        "internal": {"total_bytes": 511647744, "used_bytes": 97677312},
        "usb": {"total_bytes": 8036270080, "used_bytes": 1410953216},
    }
    sensor = cc2_extras.ElegooStorageSensor(coordinator)
    assert sensor.native_value == 19.1
    attrs = sensor.extra_state_attributes
    assert attrs["internal_total_mb"] == 488
    assert attrs["internal_free_mb"] == 395
    assert attrs["usb_inserted"] is True


def test_storage_sensor_unknown_without_disk_info() -> None:
    coordinator, _ = _coordinator()
    assert cc2_extras.ElegooStorageSensor(coordinator).native_value is None


def test_filament_sensor() -> None:
    coordinator, _ = _coordinator()
    sensor = cc2_extras.ElegooFilamentBinarySensor(coordinator)
    assert sensor.is_on is False
    assert sensor.extra_state_attributes == {"detection_enabled": True}


def test_auto_refill_switch_sends_2004() -> None:
    coordinator, client = _coordinator()
    client.printer_data.ams_status = SimpleNamespace(auto_refill=True)
    switch = cc2_extras.ElegooAutoRefillSwitch(coordinator)
    switch.async_write_ha_state = MagicMock()
    assert switch.is_on is True
    client._send_command = AsyncMock(return_value={"result": {"error_code": 0}})
    asyncio.run(switch.async_turn_off())
    first = client._send_command.call_args_list[0]
    assert first.args == (2004, {"auto_refill": False})
    # the printer still reports the old value for a while; the switch shows
    # what was asked for until it agrees
    assert switch.is_on is False
    client.printer_data.ams_status = SimpleNamespace(auto_refill=False)
    assert switch.is_on is False
    client.printer_data.ams_status = SimpleNamespace(auto_refill=True)
    assert switch.is_on is True  # pending cleared once the printer agreed


def test_connected_sensor_is_off_not_unavailable_when_down() -> None:
    coordinator, client = _coordinator()
    sensor = cc2_extras.ElegooConnectedBinarySensor(coordinator)
    client._is_connected = False
    assert sensor.available is True
    assert sensor.is_on is False


def test_last_print_reads_the_newest_finished_job() -> None:
    coordinator, client = _coordinator()
    coordinator.config_entry.entry_id = "E"
    client.printer_data.print_tasks = parse_task_list(
        {
            "history_task_list": [
                {
                    "task_id": "1",
                    "task_name": "a.gcode",
                    "begin_time": 100,
                    "end_time": 200,
                    "task_status": 1,
                },
                {
                    "task_id": "2",
                    "task_name": "b.gcode",
                    "begin_time": 300,
                    "end_time": 700,
                    "task_status": 2,
                },
            ]
        }
    )
    client.printer_data.file_list = {
        "b.gcode": PrinterFile(
            {
                "filename": "b.gcode",
                "total_filament_used": 5.5,
                "color_map": [{"t": 0, "color": "#FFFFFF", "name": "PLA"}],
            }
        )
    }
    sensor = cc2_extras.ElegooLastPrintSensor(coordinator)
    assert sensor.native_value.timestamp() == 700
    attrs = sensor.extra_state_attributes
    assert attrs["file"] == "b.gcode"
    assert attrs["result"] == "stopped"
    assert attrs["duration_seconds"] == 400
    assert attrs["filament_grams"] == 5.5
    assert attrs["filament_colors"] == ["#FFFFFF"]


def test_online_since_ignores_blips_and_resets_after_an_outage() -> None:
    coordinator, _client = _coordinator()
    coordinator.last_update_success = True
    sensor = cc2_extras.ElegooOnlineSinceSensor(coordinator)
    sensor.async_write_ha_state = MagicMock()
    start = datetime(2026, 10, 3, 5, 13, 52, tzinfo=UTC)
    sensor._since = start
    clock = {"t": 1000.0}

    def tick(connected: bool, seconds: float) -> None:  # noqa: FBT001
        clock["t"] += seconds
        with (
            patch.object(cc2_extras.time, "monotonic", return_value=clock["t"]),
            patch.object(
                ElegooCC2Client,
                "is_connected",
                new_callable=PropertyMock,
                return_value=connected,
            ),
        ):
            sensor._handle_coordinator_update()

    tick(connected=False, seconds=0)
    tick(connected=True, seconds=30)  # a 30 s blip: same uptime
    assert sensor.native_value == start
    tick(connected=False, seconds=60)
    tick(connected=True, seconds=600)  # off for 10 min: powered on again
    assert sensor.native_value > start
    assert sensor.available is True


def _stage(frame: dict) -> str | None:
    coordinator, client = _coordinator()
    client._cached_status = frame
    return cc2_extras.ElegooStageSensor(coordinator).native_value


def test_stage_names_the_preparing_steps() -> None:
    def frame(code: int, layer: int = 0) -> dict:
        return {
            "machine_status": {"status": 2, "sub_status": code},
            "print_status": {"current_layer": layer},
        }

    assert _stage(frame(1405)) == "Heating bed"
    assert _stage(frame(2801)) == "Homing"
    assert _stage(frame(2901)) == "Auto-leveling"
    assert _stage(frame(1045)) == "Heating nozzle"
    assert _stage(frame(1066)) == "Preparing"
    assert _stage(frame(1066, layer=3)) == "Printing"
    assert _stage(frame(2075, layer=3)) == "Printing"


def test_stage_is_idle_or_unknown_outside_a_job() -> None:
    assert _stage({"machine_status": {"status": 1, "sub_status": 0}}) == "Idle"
    assert _stage({}) is None


def test_stage_attributes_keep_the_codes() -> None:
    coordinator, client = _coordinator()
    client._cached_status = {"machine_status": {"status": 2, "sub_status": 2901}}
    sensor = cc2_extras.ElegooStageSensor(coordinator)
    attrs = sensor.extra_state_attributes
    assert attrs["code"] == 2901
    assert attrs["machine_status"] == 2


def test_the_client_keeps_the_frame_from_each_leveling_pass() -> None:
    _, client = _coordinator()

    def push(code: int, extra: int) -> None:
        client._cached_status = {
            "machine_status": {"status": 2, "sub_status": code},
            "probe": extra,
        }
        client._update_printer_status()

    push(1045, 0)
    assert client.leveling_frames == {}
    push(2901, 1)
    push(2901, 2)
    assert client.leveling_frames["first"]["probe"] == 1
    assert client.leveling_frames["latest"]["probe"] == 2
    push(1045, 3)
    push(2901, 9)
    assert client.leveling_frames["first"]["probe"] == 9


def test_timelapse_storage_sensor_reads_the_folder_usage() -> None:
    coordinator, _ = _coordinator()
    sensor = cc2_extras.ElegooTimelapseStorageSensor(coordinator)
    with patch.object(
        cc2_extras.timelapse_media,
        "usage",
        return_value={"used_gb": 1.5, "limit_gb": 20, "percent": 7.5, "count": 2},
    ):
        assert sensor.native_value == 1.5
        assert sensor.extra_state_attributes == {
            "limit_gb": 20,
            "percent": 7.5,
            "count": 2,
        }
    assert sensor.available
