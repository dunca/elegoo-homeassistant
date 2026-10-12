"""
Job history sensors stay readable with the printer switched off.

Finished jobs, their names, energy and cost live in Home Assistant's job
archive, so the Print History, Last print, Print name, Print energy and Print
cost sensors are available whenever the archive is loaded, not only while the
printer answers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.elegoo_printer import cc2_extras, job_archive
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.job_archive import JobArchive
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.printer import Printer
from custom_components.elegoo_printer.sensor import ElegooPrintHistorySensor


def _coordinator(*, online: bool) -> MagicMock:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    client = ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)
    coordinator = MagicMock()
    coordinator.generate_unique_id = lambda key: f"x_{key}"
    coordinator.config_entry.runtime_data.api.client = client
    coordinator.config_entry.entry_id = "OFF"
    coordinator.data = client.printer_data
    coordinator.last_update_success = online
    return coordinator


@pytest.fixture
def archive() -> Any:
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    with patch.object(job_archive, "Store", return_value=store):
        loaded = JobArchive(MagicMock(), "OFF")
    job_archive._ARCHIVES["OFF"] = loaded
    yield loaded
    job_archive._ARCHIVES.pop("OFF", None)


ARCHIVE_SENSORS = (
    cc2_extras.ElegooLastPrintSensor,
    cc2_extras.ElegooPrintNameSensor,
    cc2_extras.ElegooPrintEnergySensor,
    cc2_extras.ElegooPrintCostSensor,
)


@pytest.mark.usefixtures("archive")
@pytest.mark.parametrize("cls", ARCHIVE_SENSORS)
def test_archive_sensors_stay_available_with_the_printer_off(cls: type) -> None:
    coordinator = _coordinator(online=False)
    coordinator.hass.config.currency = "RON"
    assert cls(coordinator).available is True


@pytest.mark.parametrize("cls", ARCHIVE_SENSORS)
def test_archive_sensors_unavailable_without_an_archive(cls: type) -> None:
    coordinator = _coordinator(online=True)
    coordinator.hass.config.currency = "RON"
    assert cls(coordinator).available is False


@pytest.mark.usefixtures("archive")
def test_history_sensor_stays_available_with_the_printer_off() -> None:
    from custom_components.elegoo_printer.definitions import (  # noqa: PLC0415
        PRINTER_STATUS_CC2_PRINT_HISTORY,
    )

    sensor = ElegooPrintHistorySensor(
        coordinator=_coordinator(online=False),
        entity_description=PRINTER_STATUS_CC2_PRINT_HISTORY[0],
    )
    assert sensor.available is True


def test_live_printer_sensors_still_follow_the_connection() -> None:
    coordinator = _coordinator(online=False)
    stage = cc2_extras.ElegooStageSensor(coordinator)
    assert stage.available is False
