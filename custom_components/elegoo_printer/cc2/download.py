"""
Stream a file from a Centauri Carbon 2's ``/download`` endpoint.

Same server and quirks as the timelapse download (see ``timelapse.py``): the
access code goes in ``X-Token``, the reply carries ``Content-Length`` and
chunked ``Transfer-Encoding`` at once, and the socket stays open after the
last chunk. A path the printer does not have is not answered at all, so the
header wait is short. This streams instead of buffering, since G-code files run
to tens of megabytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import urllib.parse
from typing import TYPE_CHECKING

from .timelapse import DOWNLOAD_PORT, TimelapseDownloadError, _parse_head

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HEADER_TIMEOUT = 6  # seconds; a missing file gets no reply at all
READ_TIMEOUT = 15  # seconds without a byte mid-transfer


class PrinterDownload:
    """An open download: ``headers``, then ``iter_body()`` once, then ``close()``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
    ) -> None:
        """Wrap a connection whose status line and headers were read."""
        self._reader = reader
        self._writer = writer
        self.headers = headers

    @property
    def length(self) -> int | None:
        """The file size the printer announced, if any."""
        value = self.headers.get("content-length", "")
        return int(value) if value.isdigit() else None

    async def _read(self, n: int) -> bytes:
        data = await asyncio.wait_for(self._reader.read(n), timeout=READ_TIMEOUT)
        if not data:
            msg = "the printer closed the connection mid-file"
            raise TimelapseDownloadError(msg)
        return data

    async def _read_line(self) -> bytes:
        return await asyncio.wait_for(
            self._reader.readuntil(b"\r\n"), timeout=READ_TIMEOUT
        )

    async def iter_body(self, chunk_size: int = 256 * 1024) -> AsyncIterator[bytes]:
        """Yield the file's bytes, de-chunked, stopping at its end."""
        if "chunked" not in self.headers.get("transfer-encoding", "").lower():
            remaining = self.length or 0
            while remaining > 0:
                data = await self._read(min(chunk_size, remaining))
                remaining -= len(data)
                yield data
            return
        while True:
            size = int((await self._read_line()).split(b";", 1)[0], 16)
            if size == 0:
                return
            remaining = size
            while remaining > 0:
                data = await self._read(min(chunk_size, remaining))
                remaining -= len(data)
                yield data
            await self._reader.readexactly(2)  # CRLF after each chunk

    async def close(self) -> None:
        """Close the connection."""
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()


async def open_download(host: str, access_code: str, file_name: str) -> PrinterDownload:
    """
    Request a file and return once the printer has answered with HTTP 200.

    Never put the request in an exception or log line: the query string holds
    the printer's access code.
    """
    query = urllib.parse.urlencode({"X-Token": access_code, "file_name": file_name})
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, DOWNLOAD_PORT), timeout=HEADER_TIMEOUT
        )
    except (OSError, TimeoutError) as err:
        msg = f"cannot reach the printer on port {DOWNLOAD_PORT}"
        raise TimelapseDownloadError(msg) from err
    try:
        writer.write(
            f"GET /download?{query} HTTP/1.1\r\n"
            f"Host: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=HEADER_TIMEOUT
        )
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
        writer.close()
        msg = "the printer did not answer for that file"
        raise TimelapseDownloadError(msg) from err
    status, headers = _parse_head(head[:-4])
    if status != 200:  # noqa: PLR2004
        writer.close()
        msg = f"the printer answered HTTP {status}"
        raise TimelapseDownloadError(msg)
    return PrinterDownload(reader, writer, headers)
