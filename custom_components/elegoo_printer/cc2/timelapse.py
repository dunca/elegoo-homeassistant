"""
Print history and timelapse videos on a Centauri Carbon 2.

Measured on firmware 02.01.00.00:

* ``1036`` answers with every job the printer remembers, oldest first, in
  ``history_task_list``. It ignores paging parameters.
* A job printed with timelapse on is stored as frames first:
  ``time_lapse_video_status`` 1 with a ``picture/<name><stamp>`` url and a
  size of 0. ``1051`` with that url turns the frames into an MP4 and answers
  with the ``video/<name><stamp>.mp4`` path straight away; the job reads
  status 2 with the real size once the video is written (under 30 s for an
  11 minute print). Status 3 is a timelapse still being recorded.
* The MP4 is served by the printer's libhv server on port 80 at
  ``/download?X-Token=<access code>&file_name=<video path>``. The reply
  carries ``Content-Length`` *and* ``Transfer-Encoding: chunked``, which
  aiohttp rejects, and the printer keeps the socket open after the last
  chunk, so the body is read and de-chunked here by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
import urllib.parse
from dataclasses import dataclass
from typing import Any

TIMELAPSE_NONE = 0
TIMELAPSE_FRAMES = 1
TIMELAPSE_READY = 2
TIMELAPSE_RECORDING = 3

TASK_STATUS_COMPLETE = 1
TASK_STATUS_STOPPED = 2

DOWNLOAD_PORT = 80
CONNECT_TIMEOUT = 10  # seconds
READ_IDLE_TIMEOUT = 10  # seconds without a byte before giving up
MAX_VIDEO_BYTES = 512 * 1024 * 1024


class TimelapseDownloadError(Exception):
    """The printer did not hand over a complete video."""


@dataclass(frozen=True, slots=True)
class CC2PrintTask:
    """One job from the printer's own history (method 1036)."""

    task_id: str
    file_name: str
    begin_time: int
    end_time: int
    task_status: int
    timelapse_status: int
    timelapse_url: str
    timelapse_size: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CC2PrintTask | None:
        """Build a task from a 1036 entry; None if it has no task id."""
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        return cls(
            task_id=task_id,
            file_name=str(data.get("task_name") or ""),
            begin_time=_int(data.get("begin_time")),
            end_time=_int(data.get("end_time")),
            task_status=_int(data.get("task_status")),
            timelapse_status=_int(data.get("time_lapse_video_status")),
            timelapse_url=str(data.get("time_lapse_video_url") or ""),
            timelapse_size=_int(data.get("time_lapse_video_size")),
        )

    @property
    def has_timelapse(self) -> bool:
        """Whether frames or a finished video exist for this job."""
        return self.timelapse_status in (TIMELAPSE_FRAMES, TIMELAPSE_READY)

    @property
    def video_ready(self) -> bool:
        """Whether the MP4 is already written and can be downloaded."""
        ready = self.timelapse_status == TIMELAPSE_READY
        return ready and self.timelapse_url.startswith("video/")

    @property
    def result(self) -> str:
        """``complete``, ``stopped`` or ``unknown``."""
        return {
            TASK_STATUS_COMPLETE: "complete",
            TASK_STATUS_STOPPED: "stopped",
        }.get(self.task_status, "unknown")

    @property
    def timelapse(self) -> str:
        """``none``, ``frames``, ``ready`` or ``recording``."""
        return {
            TIMELAPSE_FRAMES: "frames",
            TIMELAPSE_READY: "ready",
            TIMELAPSE_RECORDING: "recording",
        }.get(self.timelapse_status, "none")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_task_list(result: dict[str, Any] | None) -> list[CC2PrintTask]:
    """Parse a 1036 result into tasks, oldest first, skipping malformed rows."""
    rows = (result or {}).get("history_task_list")
    if not isinstance(rows, list):
        return []
    tasks = (CC2PrintTask.from_dict(row) for row in rows if isinstance(row, dict))
    return [task for task in tasks if task is not None]


def dechunk(body: bytes) -> bytes:
    """Decode a chunked HTTP body; raises ValueError if it is not complete."""
    out = bytearray()
    pos = 0
    while True:
        line_end = body.find(b"\r\n", pos)
        if line_end < 0:
            msg = "chunk size line missing"
            raise ValueError(msg)
        size = int(body[pos:line_end].split(b";", 1)[0], 16)
        start = line_end + 2
        if size == 0:
            return bytes(out)
        end = start + size
        if body[end : end + 2] != b"\r\n":
            msg = "chunk shorter than its declared size"
            raise ValueError(msg)
        out += body[start:end]
        pos = end + 2


def _body_complete(headers: dict[str, str], body: bytes) -> bool:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return body.endswith(b"\r\n0\r\n\r\n")
    length = headers.get("content-length")
    return length is not None and len(body) >= int(length)


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers


async def _read_response(
    reader: asyncio.StreamReader,
) -> tuple[dict[str, str], bytes]:
    """Read one HTTP reply; stop at the end of the body, not at socket close."""
    raw = bytearray()
    headers: dict[str, str] = {}
    body_start = -1
    while True:
        try:
            chunk = await asyncio.wait_for(
                reader.read(256 * 1024), timeout=READ_IDLE_TIMEOUT
            )
        except TimeoutError as err:
            msg = "the printer stopped sending before the video was complete"
            raise TimelapseDownloadError(msg) from err
        raw += chunk
        if len(raw) > MAX_VIDEO_BYTES:
            msg = "video larger than expected"
            raise TimelapseDownloadError(msg)
        if body_start < 0 and (sep := raw.find(b"\r\n\r\n")) >= 0:
            body_start = sep + 4
            status, headers = _parse_head(bytes(raw[:sep]))
            if status != 200:  # noqa: PLR2004
                msg = f"the printer answered HTTP {status}"
                raise TimelapseDownloadError(msg)
        body = bytes(raw[body_start:]) if body_start >= 0 else b""
        if body_start >= 0 and _body_complete(headers, body):
            return headers, body
        if not chunk:
            break
    if body_start < 0:
        msg = "the printer sent no HTTP reply"
        raise TimelapseDownloadError(msg)
    return headers, bytes(raw[body_start:])


def _decode_body(headers: dict[str, str], body: bytes) -> bytes:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        try:
            body = dechunk(body)
        except ValueError as err:
            msg = f"bad chunked body: {err}"
            raise TimelapseDownloadError(msg) from err
    elif (length := headers.get("content-length")) is not None:
        body = body[: int(length)]
    if body[4:8] != b"ftyp":
        msg = "the download is not an MP4 file"
        raise TimelapseDownloadError(msg)
    return body


async def download_video(host: str, access_code: str, video_url: str) -> bytes:
    """
    Fetch a finished timelapse MP4 from the printer.

    Never put the request in an exception or log line: the query string holds
    the printer's access code.
    """
    query = urllib.parse.urlencode({"X-Token": access_code, "file_name": video_url})
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, DOWNLOAD_PORT), timeout=CONNECT_TIMEOUT
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
        headers, body = await _read_response(reader)
    except OSError as err:
        msg = "connection to the printer failed during the download"
        raise TimelapseDownloadError(msg) from err
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return _decode_body(headers, body)
