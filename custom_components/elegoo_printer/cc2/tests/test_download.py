"""Tests for streaming a file from the printer's /download endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.elegoo_printer.cc2 import download
from custom_components.elegoo_printer.cc2.timelapse import TimelapseDownloadError

GCODE = b"; HEADER_BLOCK_START\n" + b"G1 X10 Y10\n" * 50000


async def _printer(*, chunks: list[bytes] | None, answer: bool = True) -> tuple:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        if answer and chunks is not None:
            total = sum(len(c) for c in chunks)
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {total}\r\n"
                "Transfer-Encoding: chunked\r\n\r\n".encode()
            )
            for c in chunks:
                writer.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        await asyncio.sleep(30)  # like the printer: never closes, never answers

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_streams_a_multi_chunk_file() -> None:  # noqa: D103
    async def run() -> bytes:
        server, port = await _printer(chunks=[GCODE[:100000], GCODE[100000:]])
        with patch.object(download, "DOWNLOAD_PORT", port):
            dl = await download.open_download("127.0.0.1", "code", "a.gcode")
            try:
                assert dl.length == len(GCODE)
                return b"".join([c async for c in dl.iter_body(chunk_size=4096)])
            finally:
                await dl.close()
                server.close()

    assert asyncio.run(run()) == GCODE


def test_missing_file_fails_fast() -> None:  # noqa: D103
    async def run() -> None:
        server, port = await _printer(chunks=None, answer=False)
        with (
            patch.object(download, "DOWNLOAD_PORT", port),
            patch.object(download, "HEADER_TIMEOUT", 0.2),
        ):
            try:
                await download.open_download("127.0.0.1", "code", "missing.gcode")
            finally:
                server.close()

    with pytest.raises(TimelapseDownloadError, match="did not answer"):
        asyncio.run(run())
