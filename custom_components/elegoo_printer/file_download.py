"""Download a job's G-code from a Centauri Carbon 2 through Home Assistant."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .cc2.download import open_download
from .cc2.timelapse import TimelapseDownloadError
from .const import LOGGER
from .job_previews import preview_key
from .timelapse_media import TimelapseError, cc2_client

if TYPE_CHECKING:
    from .cc2.download import PrinterDownload

VIEW_URL = "/api/elegoo_printer/gcode/{entry_id}/{key}"

# The timelapse lives at "video/<name>" under the server's root; where G-code
# sits relative to it is not documented, so the first path that answers is
# remembered and tried first afterwards.
_CANDIDATES = ["{name}", "local/{name}", "gcode/{name}", "/local/{name}"]
_working: list[str] = []


def gcode_url(entry_id: str, file_name: str) -> str:
    """Return the authenticated download URL of a file on the printer."""
    return VIEW_URL.format(entry_id=entry_id, key=preview_key(file_name))


async def _open(host: str, code: str, name: str) -> PrinterDownload:
    last: TimelapseDownloadError | None = None
    for pattern in [*_working, *(p for p in _CANDIDATES if p not in _working)]:
        try:
            download = await open_download(host, code, pattern.format(name=name))
        except TimelapseDownloadError as err:
            last = err
            continue
        if pattern not in _working:
            _working.insert(0, pattern)
            LOGGER.info("The printer serves G-code at path %r", pattern)
        return download
    raise last or TimelapseDownloadError("no path answered")


class ElegooGcodeView(HomeAssistantView):
    """Stream a G-code file to a signed-in user (or a signed URL)."""

    url = VIEW_URL
    name = "api:elegoo_printer:gcode"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, key: str
    ) -> web.StreamResponse:
        """Pass the file through from the printer, chunk by chunk."""
        hass = request.app["hass"]
        try:
            client = cc2_client(hass, entry_id)
        except TimelapseError as err:
            return web.Response(status=404, text=str(err))
        name = next(
            (n for n in client.printer_data.file_list if preview_key(n) == key), None
        )
        if name is None:
            return web.Response(status=404, text="That file is not on the printer")
        try:
            download = await _open(client.printer_ip, client.access_code or "", name)
        except TimelapseDownloadError as err:
            return web.Response(status=502, text=f"Printer: {err}")
        try:
            response = web.StreamResponse(
                headers={
                    "Content-Type": "text/x-gcode",
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
                    ),
                }
            )
            if download.length is not None:
                response.content_length = download.length
            await response.prepare(request)
            try:
                async for data in download.iter_body():
                    await response.write(data)
            except (TimelapseDownloadError, TimeoutError, OSError) as err:
                LOGGER.warning("G-code download of %s broke off: %s", name, err)
                return response
            await response.write_eof()
            return response
        finally:
            await download.close()
