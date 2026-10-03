"""
Diagnostics for Elegoo printers.

For a Centauri Carbon 2 this also asks the printer, fresh, for its read-only
data (attributes, full status, disk info, CANVAS status, and one sample each of
the file list and the job history) and includes the raw replies, so what the
firmware actually reports can be compared with what the integration maps.
Serial numbers, addresses, host names and the access code are redacted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .cc2.client import ElegooCC2Client
from .cc2.const import (
    CC2_CMD_GET_ATTRIBUTES,
    CC2_CMD_GET_CANVAS_STATUS,
    CC2_CMD_GET_DISK_INFO,
    CC2_CMD_GET_FILE_LIST,
    CC2_CMD_GET_STATUS,
    CC2_CMD_PRINT_TASK_LIST,
)
from .sdcp.exceptions import PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ElegooPrinterConfigEntry

TO_REDACT = {
    "access_code",
    "password",
    "token",
    "sn",
    "serial_number",
    "SerialNumber",
    "id",
    "ip",
    "ip_address",
    "IpAddress",
    "MainboardIP",
    "MainboardID",
    "MainboardMAC",
    "mac",
    "hostname",
    "external_ip",
    "proxy_host",
    "url",
    "video_url",
}

QUERIES: tuple[tuple[str, int, dict[str, Any] | None], ...] = (
    ("attributes_1001", CC2_CMD_GET_ATTRIBUTES, None),
    ("status_1002", CC2_CMD_GET_STATUS, None),
    ("disk_info_1048", CC2_CMD_GET_DISK_INFO, {"storage_media": "local"}),
    ("canvas_status_2005", CC2_CMD_GET_CANVAS_STATUS, None),
    ("file_list_1044", CC2_CMD_GET_FILE_LIST, {"storage_media": "local", "path": "/"}),
    ("task_list_1036", CC2_CMD_PRINT_TASK_LIST, None),
)
# Lists that can run to hundreds of entries; one sample shows their shape.
_SAMPLED = {"file_list", "history_task_list"}


def _sample(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in _SAMPLED & out.keys():
        items = out[key]
        if isinstance(items, list):
            out[key] = items[:1]
            out[f"{key}_count"] = len(items)
    return out


def _scrub(value: Any, secrets: list[str]) -> Any:
    """Replace any remaining occurrence of a secret value inside strings."""
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "**REDACTED**")
    return value


async def _query_printer(client: ElegooCC2Client) -> dict[str, Any]:
    replies: dict[str, Any] = {}
    for name, method, params in QUERIES:
        try:
            response = await client._send_command(method, params)  # noqa: SLF001
        except (*PRINT_TRANSPORT_ERRORS, ElegooPrinterTimeoutError) as err:
            replies[name] = {"error": type(err).__name__}
            continue
        replies[name] = _sample((response or {}).get("result"))
    return replies


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ElegooPrinterConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    api = entry.runtime_data.api
    client = api.client
    data: dict[str, Any] = {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options or {}), TO_REDACT),
        "printer": {
            "model": getattr(api.printer, "model", None),
            "transport": str(getattr(api.printer, "transport_type", None)),
            "protocol": str(getattr(api.printer, "protocol_version", None)),
        },
    }
    if isinstance(client, ElegooCC2Client):
        secrets = [
            str(client.printer_ip or ""),
            str(client.serial_number or ""),
            str(client.access_code or ""),
        ]
        raw = {
            "connected": client.is_connected,
            "cached_status": client._cached_status,  # noqa: SLF001
            "fresh_replies": await _query_printer(client)
            if client.is_connected
            else {},
        }
        data["cc2"] = _scrub(async_redact_data(raw, TO_REDACT), secrets)
    return data
