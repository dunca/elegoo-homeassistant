"""Tests for fetching the preview of any file on the printer (method 1045)."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, patch

from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.const import CC2_CMD_GET_FILE_THUMBNAIL
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.printer import Printer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _client() -> ElegooCC2Client:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    return ElegooCC2Client("192.168.1.1", "TESTSN", printer=printer)


def _fetch(result: dict) -> tuple[bytes | None, AsyncMock]:
    client = _client()
    response = {"id": 1, "method": CC2_CMD_GET_FILE_THUMBNAIL, "result": result}
    with patch.object(
        client, "_send_command", new_callable=AsyncMock, return_value=response
    ) as mock_cmd:
        data = asyncio.run(client.get_file_thumbnail("a.gcode"))
    return data, mock_cmd


def test_returns_png_bytes() -> None:  # noqa: D103
    data, mock_cmd = _fetch(
        {"error_code": 0, "thumbnail": base64.b64encode(PNG).decode()}
    )
    assert data == PNG
    mock_cmd.assert_called_once_with(
        CC2_CMD_GET_FILE_THUMBNAIL, {"storage_media": "local", "file_name": "a.gcode"}
    )


def test_accepts_a_data_uri() -> None:  # noqa: D103
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    data, _ = _fetch({"error_code": 0, "thumbnail": uri})
    assert data == PNG


def test_none_when_missing_or_not_png() -> None:  # noqa: D103
    assert _fetch({"error_code": 1003})[0] is None
    assert _fetch({"error_code": 0})[0] is None
    assert _fetch({"error_code": 0, "thumbnail": "not base64!"})[0] is None
    not_png = base64.b64encode(b"GIF89a").decode()
    assert _fetch({"error_code": 0, "thumbnail": not_png})[0] is None
