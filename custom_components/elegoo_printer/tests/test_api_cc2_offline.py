"""
A poll of a disconnected CC2 must fail instead of returning cached data.

The CC2 client answers status and attributes from its cache, so with the
printer switched off a poll used to succeed now and then (whenever none of
the coordinator's network checks was due) and the entities flicked back to
"idle" for a few seconds every 6-10 minutes.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.elegoo_printer.api import ElegooPrinterApiClient
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.sdcp.exceptions import (
    ElegooPrinterNotConnectedError,
)
from custom_components.elegoo_printer.sdcp.models.printer import PrinterData


def _api(*, connected: bool) -> tuple[ElegooPrinterApiClient, MagicMock]:
    """Build an API client around a mocked CC2 client."""
    data = PrinterData()
    client = MagicMock(spec=ElegooCC2Client)
    client.is_connected = connected
    client.get_printer_attributes = AsyncMock(return_value=data)
    client.get_printer_status = AsyncMock(return_value=data)
    client.async_get_printer_historical_tasks = AsyncMock(return_value={})
    client.async_get_printer_current_task = AsyncMock(return_value=None)
    api = object.__new__(ElegooPrinterApiClient)
    api.client = client
    api.printer_data = data
    api._logger = logging.getLogger(__name__)
    return api, client


def test_disconnected_cc2_poll_raises() -> None:
    """Offline: the poll fails and the cache is not read."""
    api, client = _api(connected=False)
    with pytest.raises(ElegooPrinterNotConnectedError):
        asyncio.run(api.async_get_printer_data())
    client.get_printer_status.assert_not_called()


def test_connected_cc2_poll_returns_data() -> None:
    """Online: the poll returns the client's data as before."""
    api, client = _api(connected=True)
    result = asyncio.run(api.async_get_printer_data())
    assert result is client.get_printer_status.return_value
