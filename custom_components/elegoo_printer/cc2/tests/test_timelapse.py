"""Tests for CC2 job history and timelapse download (methods 1036 and 1051)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.elegoo_printer.cc2.client import ElegooCC2Client
from custom_components.elegoo_printer.cc2.const import (
    CC2_CMD_GET_TIME_LAPSE_VIDEO,
    CC2_CMD_PRINT_TASK_LIST,
)
from custom_components.elegoo_printer.cc2.timelapse import (
    TimelapseDownloadError,
    dechunk,
    download_video,
    parse_task_list,
)
from custom_components.elegoo_printer.sdcp.exceptions import (
    ElegooPrinterConnectionError,
)
from custom_components.elegoo_printer.sdcp.models.enums import PrinterType
from custom_components.elegoo_printer.sdcp.models.printer import Printer

# Rows as the printer sends them (fw 02.01.00.00).
FRAMES = {
    "begin_time": 1790959909,
    "end_time": 1790960643,
    "task_id": "896c3f5a-80d1-413c-95ed-1e9812d912b8",
    "task_name": "ECC2_0.4_ring_Elegoo PLA _0.2_11m2s.gcode",
    "task_status": 1,
    "time_lapse_video_duration": 1,
    "time_lapse_video_size": 0,
    "time_lapse_video_status": 1,
    "time_lapse_video_url": (
        "picture/ECC2_0.4_ring_Elegoo PLA _0.2_11m2s.gcode20261002195149"
    ),
}
READY = {
    **FRAMES,
    "time_lapse_video_size": 580152,
    "time_lapse_video_status": 2,
    "time_lapse_video_url": (
        "video/ECC2_0.4_ring_Elegoo PLA _0.2_11m2s.gcode20261002195149.mp4"
    ),
}
NONE = {
    "begin_time": 1789844918,
    "end_time": 1789846512,
    "task_id": "99e0badf-e41f-43fa-8a2b-6f6c359e6a05",
    "task_name": "ECC2_0.4_Doorholder_Elegoo PLA _0.2_22m2s.gcode",
    "task_status": 2,
    "time_lapse_video_duration": 0,
    "time_lapse_video_size": 0,
    "time_lapse_video_status": 0,
    "time_lapse_video_url": "",
}

MP4 = b"\x00\x00\x00\x20ftypisom" + bytes(range(256)) * 40


def _client() -> ElegooCC2Client:
    printer = Printer()
    printer.printer_type = PrinterType.FDM
    return ElegooCC2Client("127.0.0.1", "TESTSN", printer=printer)


def test_parse_task_list_reads_rows_and_skips_bad_ones() -> None:  # noqa: D103
    tasks = parse_task_list(
        {"error_code": 0, "history_task_list": [NONE, {"task_name": "x"}, FRAMES]}
    )
    assert [t.task_id for t in tasks] == [NONE["task_id"], FRAMES["task_id"]]
    stopped, frames = tasks
    assert stopped.result == "stopped"
    assert not stopped.has_timelapse
    assert stopped.timelapse == "none"
    assert frames.result == "complete"
    assert frames.has_timelapse
    assert not frames.video_ready
    assert frames.timelapse == "frames"
    ready = parse_task_list({"history_task_list": [READY]})[0]
    assert ready.video_ready
    assert ready.timelapse == "ready"


def test_parse_task_list_without_list() -> None:  # noqa: D103
    assert parse_task_list(None) == []
    assert parse_task_list({"error_code": 0}) == []


def test_get_print_task_list_stores_tasks() -> None:  # noqa: D103
    client = _client()
    response = {
        "id": 1,
        "method": CC2_CMD_PRINT_TASK_LIST,
        "result": {"error_code": 0, "history_task_list": [NONE, FRAMES]},
    }
    with patch.object(
        client, "_send_command", new_callable=AsyncMock, return_value=response
    ) as mock_cmd:
        tasks = asyncio.run(client.get_print_task_list())
    mock_cmd.assert_called_once_with(CC2_CMD_PRINT_TASK_LIST)
    assert [t.task_id for t in tasks] == [NONE["task_id"], FRAMES["task_id"]]
    assert client.printer_data.print_tasks == tasks


def test_get_print_task_list_raises_on_error_code() -> None:  # noqa: D103
    client = _client()
    response = {"id": 1, "method": CC2_CMD_PRINT_TASK_LIST, "result": {"error_code": 1}}
    with (
        patch.object(
            client, "_send_command", new_callable=AsyncMock, return_value=response
        ),
        pytest.raises(ElegooPrinterConnectionError),
    ):
        asyncio.run(client.get_print_task_list())


def test_video_url_composes_frames_with_1051() -> None:  # noqa: D103
    client = _client()
    task = parse_task_list({"history_task_list": [FRAMES]})[0]
    response = {
        "id": 2,
        "method": CC2_CMD_GET_TIME_LAPSE_VIDEO,
        "result": {"error_code": 0, "url": READY["time_lapse_video_url"]},
    }
    with patch.object(
        client, "_send_command", new_callable=AsyncMock, return_value=response
    ) as mock_cmd:
        url = asyncio.run(client.get_timelapse_video_url(task))
    mock_cmd.assert_called_once_with(
        CC2_CMD_GET_TIME_LAPSE_VIDEO, {"url": FRAMES["time_lapse_video_url"]}
    )
    assert url == READY["time_lapse_video_url"]


def test_video_url_skips_1051_when_ready() -> None:  # noqa: D103
    client = _client()
    task = parse_task_list({"history_task_list": [READY]})[0]
    with patch.object(client, "_send_command", new_callable=AsyncMock) as mock_cmd:
        url = asyncio.run(client.get_timelapse_video_url(task))
    mock_cmd.assert_not_called()
    assert url == READY["time_lapse_video_url"]


def test_dechunk() -> None:  # noqa: D103
    assert dechunk(b"3\r\nabc\r\n2;x=1\r\nde\r\n0\r\n\r\n") == b"abcde"
    with pytest.raises(ValueError, match="shorter"):
        dechunk(b"5\r\nabc\r\n0\r\n\r\n")


async def _serve_like_the_printer(
    reply_body: bytes, *, status: str = "200 OK"
) -> tuple[asyncio.Server, int, list[bytes]]:
    """Answer like libhv: both length headers, one chunk, socket left open."""
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            f"HTTP/1.1 {status}\r\nConnection: close\r\n"
            f"Content-Length: {len(reply_body)}\r\nServer: libhv/1.3.4\r\n"
            "Transfer-Encoding: chunked\r\n\r\n".encode()
            + f"{len(reply_body):x}\r\n".encode()
            + reply_body
            + b"\r\n0\r\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(30)  # the printer does not close the socket

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, requests


def test_download_video_handles_the_printers_reply() -> None:  # noqa: D103
    async def run() -> tuple[bytes, list[bytes]]:
        server, port, requests = await _serve_like_the_printer(MP4)
        with patch(
            "custom_components.elegoo_printer.cc2.timelapse.DOWNLOAD_PORT", port
        ):
            data = await asyncio.wait_for(
                download_video("127.0.0.1", "s3cret", "video/a b.mp4"), timeout=5
            )
        server.close()
        return data, requests

    data, requests = asyncio.run(run())
    assert data == MP4
    assert b"GET /download?X-Token=s3cret&file_name=video%2Fa+b.mp4 " in requests[0]


def test_download_video_hides_the_token_in_errors() -> None:  # noqa: D103
    async def run() -> None:
        server, port, _ = await _serve_like_the_printer(b"nope", status="403 Forbidden")
        with patch(
            "custom_components.elegoo_printer.cc2.timelapse.DOWNLOAD_PORT", port
        ):
            try:
                await download_video("127.0.0.1", "s3cret", "video/a.mp4")
            finally:
                server.close()

    with pytest.raises(TimelapseDownloadError) as err:
        asyncio.run(run())
    assert "403" in str(err.value)
    assert "s3cret" not in str(err.value)


def test_download_video_rejects_non_mp4() -> None:  # noqa: D103
    async def run() -> None:
        server, port, _ = await _serve_like_the_printer(b"<html>not a video</html>")
        with patch(
            "custom_components.elegoo_printer.cc2.timelapse.DOWNLOAD_PORT", port
        ):
            try:
                await download_video("127.0.0.1", "s3cret", "video/a.mp4")
            finally:
                server.close()

    with pytest.raises(TimelapseDownloadError, match="not an MP4"):
        asyncio.run(run())


def test_delete_print_tasks_sends_list_and_reports_leftovers() -> None:  # noqa: D103
    client = _client()
    calls: list = []
    listed = {"rows": [NONE]}  # what 1036 answers after the delete

    async def fake_send(method: int, params: dict | None = None) -> dict:
        calls.append((method, params))
        if method == CC2_CMD_PRINT_TASK_LIST:
            result = {"error_code": 0, "history_task_list": listed["rows"]}
        else:
            result = {"error_code": 0}
        return {"id": 1, "method": method, "result": result}

    with patch.object(client, "_send_command", side_effect=fake_send):
        gone = asyncio.run(client.delete_print_tasks([FRAMES["task_id"]]))
        kept = asyncio.run(client.delete_print_tasks([NONE["task_id"]]))
    assert calls[0][1] == {"list": [FRAMES["task_id"]]}
    assert gone == []
    assert kept == [NONE["task_id"]]
