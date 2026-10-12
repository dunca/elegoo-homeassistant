"""
A CC2 that is off when Home Assistant starts sets up offline instead of failing.

Failing setup (``ConfigEntryNotReady``) left the entry to Home Assistant's setup
retries, which back off to 10 minutes: a printer switched on in between stayed
unavailable for up to 10 minutes (12 Oct 2026). Now the entry loads with its
entities unavailable and the coordinator's 30 s reconnect connects it once the
printer's MQTT broker answers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.elegoo_printer as integration
import custom_components.elegoo_printer.api as api_module
from custom_components.elegoo_printer.api import ElegooPrinterApiClient
from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.coordinator import ElegooDataUpdateCoordinator
from custom_components.elegoo_printer.sdcp.exceptions import (
    ElegooPrinterNotConnectedError,
)
from custom_components.elegoo_printer.sdcp.models.printer import PrinterData

if TYPE_CHECKING:
    from types import SimpleNamespace

CC2_CONFIG: dict[str, Any] = {
    "name": "Centauri Carbon 2",
    "id": "SERIAL123",
    "connection": "SERIAL123",
    "model": "Centauri Carbon 2",
    "ip_address": "10.0.0.9",
    "protocol_version": "CC2",
    "transport_type": "cc2_mqtt",
}


async def _create(config: dict[str, Any], *, reachable: bool) -> tuple[Any, AsyncMock]:
    """Run ``async_create`` with the broker (un)reachable; return (api, connect)."""

    class _Writer:
        def close(self) -> None:
            """No-op."""

        async def wait_closed(self) -> None:
            """No-op."""

    async def fake_open_connection(*_a: Any, **_k: Any) -> tuple[MagicMock, _Writer]:
        if not reachable:
            raise OSError(113, "No route to host")
        return MagicMock(), _Writer()

    connect = AsyncMock(return_value=True)
    with (
        patch.object(api_module, "async_get_clientsession", return_value=MagicMock()),
        patch.object(api_module, "get_async_client", return_value=MagicMock()),
        patch("asyncio.open_connection", side_effect=fake_open_connection),
        patch.object(ElegooCC2Client, "connect_printer", new=connect),
        patch.object(
            ElegooPrinterApiClient, "_update_config_entry_if_needed", new=AsyncMock()
        ),
    ):
        api = await ElegooPrinterApiClient.async_create(
            config, MagicMock(), MagicMock(), None
        )
    return api, connect


async def test_unreachable_cc2_creates_an_offline_client() -> None:
    """The client is returned (not None), marked offline, with empty data."""
    api, connect = await _create(CC2_CONFIG, reachable=False)

    assert api is not None
    assert api.started_offline is True
    assert api.printer_data is api.client.printer_data
    connect.assert_not_awaited()


async def test_reachable_cc2_connects_as_before() -> None:
    """A printer that answers still connects during setup."""
    api, connect = await _create(CC2_CONFIG, reachable=True)

    assert api is not None
    assert api.started_offline is False
    connect.assert_awaited_once()


async def test_setup_entry_loads_offline_instead_of_raising() -> None:
    """An offline CC2 entry finishes setup and forwards its platforms."""
    data = PrinterData()
    api = MagicMock(started_offline=True, printer_data=data)
    coordinator = MagicMock(data=None)
    coordinator.async_refresh = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = MagicMock(data=CC2_CONFIG, options={}, entry_id="E1")

    with (
        patch.object(
            integration, "ElegooDataUpdateCoordinator", return_value=coordinator
        ),
        patch.object(
            integration.ElegooPrinterApiClient,
            "async_create",
            new=AsyncMock(return_value=api),
        ),
        patch.object(integration, "async_get_loaded_integration"),
        patch.object(integration, "async_load_saved", new=AsyncMock()),
        patch.object(integration, "async_load_saved_previews", new=AsyncMock()),
        patch.object(integration, "async_load_saved_gcode", new=AsyncMock()),
        patch.object(integration, "async_setup_archive", new=AsyncMock()),
    ):
        assert await integration.async_setup_entry(hass, entry) is True

    coordinator.async_refresh.assert_awaited_once()
    coordinator.async_config_entry_first_refresh.assert_not_awaited()
    assert coordinator.data is data
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()


def _coordinator(
    hass: MagicMock, entry: SimpleNamespace
) -> ElegooDataUpdateCoordinator:
    entry.async_on_unload = MagicMock()  # type: ignore[attr-defined]
    entry.pref_disable_polling = False  # type: ignore[attr-defined]
    return ElegooDataUpdateCoordinator(hass, entry=entry)


async def test_successful_reconnect_polls_again_in_2s(
    hass: MagicMock, entry: SimpleNamespace
) -> None:
    """Once reconnect succeeds, the next poll comes in 2 s, not 30 s."""
    entry.runtime_data.api.async_get_printer_data.side_effect = (
        ElegooPrinterNotConnectedError()
    )
    entry.runtime_data.api.reconnect = AsyncMock(return_value=True)

    coordinator = _coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.update_interval == timedelta(seconds=2)


async def test_failed_reconnect_keeps_the_30s_interval(
    hass: MagicMock, entry: SimpleNamespace
) -> None:
    """While the printer stays unreachable, polling stays at 30 s."""
    entry.runtime_data.api.async_get_printer_data.side_effect = (
        ElegooPrinterNotConnectedError()
    )
    entry.runtime_data.api.reconnect = AsyncMock(return_value=False)

    coordinator = _coordinator(hass, entry)
    await coordinator.async_refresh()

    assert coordinator.update_interval == timedelta(seconds=30)
