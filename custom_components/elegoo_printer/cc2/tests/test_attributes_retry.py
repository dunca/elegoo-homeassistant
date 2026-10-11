"""
The CC2 attributes request (1001) is retried until the printer answers.

The printer sometimes ignores the attributes request sent right after
registering. Before this fix the client never asked again, so
max_video_stream_allowed stayed 0 and the camera refused every image until
the integration was reloaded.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.const import (
    CC2_CMD_GET_ATTRIBUTES,
    CC2_CMD_GET_STATUS,
)
from custom_components.elegoo_printer.sdcp.exceptions import (
    ElegooPrinterNotConnectedError,
    ElegooPrinterTimeoutError,
)


def _client() -> ElegooCC2Client:
    return ElegooCC2Client(
        printer_ip="127.0.0.1",
        serial_number="test_serial",
        access_code="x",
        logger=MagicMock(),
    )


def _methods(send: AsyncMock) -> list[int]:
    return [c.args[0] for c in send.call_args_list]


def test_status_still_requested_when_attributes_time_out() -> None:
    """A missed attributes reply no longer skips the initial status request."""

    async def run() -> None:
        client = _client()

        async def send(method: int, *_a: Any, **_k: Any) -> None:
            if method == CC2_CMD_GET_ATTRIBUTES:
                raise ElegooPrinterTimeoutError

        mock = AsyncMock(side_effect=send)
        with patch.object(client, "_send_command", mock):
            await client._request_initial_data()
        assert _methods(mock) == [CC2_CMD_GET_ATTRIBUTES, CC2_CMD_GET_STATUS]
        assert client._attributes_received is False

    asyncio.run(run())


def test_retry_requests_attributes_until_received() -> None:
    """The heartbeat hook re-asks while attributes are missing."""

    async def run() -> None:
        client = _client()
        mock = AsyncMock(side_effect=ElegooPrinterTimeoutError)
        with patch.object(client, "_send_command", mock):
            client._retry_attributes_if_missing()
            await client._attributes_retry_task
            client._retry_attributes_if_missing()
            await client._attributes_retry_task
        assert _methods(mock) == [CC2_CMD_GET_ATTRIBUTES, CC2_CMD_GET_ATTRIBUTES]

    asyncio.run(run())


def test_no_retry_once_attributes_received() -> None:
    """Once the printer has answered, the heartbeat stops asking."""

    async def run() -> None:
        client = _client()
        client._handle_attributes({})  # an empty reply maps to default attributes
        assert client._attributes_received is True
        mock = AsyncMock()
        with patch.object(client, "_send_command", mock):
            client._retry_attributes_if_missing()
        assert client._attributes_retry_task is None
        mock.assert_not_called()

    asyncio.run(run())


def test_no_duplicate_retry_while_one_is_in_flight() -> None:
    """A slow retry is not stacked with another one."""

    async def run() -> None:
        client = _client()
        gate = asyncio.Event()

        async def slow(*_a: Any, **_k: Any) -> None:
            await gate.wait()

        mock = AsyncMock(side_effect=slow)
        with patch.object(client, "_send_command", mock):
            client._retry_attributes_if_missing()
            first = client._attributes_retry_task
            await asyncio.sleep(0)
            client._retry_attributes_if_missing()
            assert client._attributes_retry_task is first
            gate.set()
            await first
        assert mock.await_count == 1

    asyncio.run(run())


def test_retry_tolerates_a_dropped_connection() -> None:
    """A retry that finds the client disconnected just ends quietly."""

    async def run() -> None:
        client = _client()
        mock = AsyncMock(side_effect=ElegooPrinterNotConnectedError)
        with patch.object(client, "_send_command", mock):
            client._retry_attributes_if_missing()
            await client._attributes_retry_task
        assert client._attributes_received is False

    asyncio.run(run())
