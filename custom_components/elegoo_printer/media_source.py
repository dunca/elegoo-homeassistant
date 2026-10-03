"""Browse and play Centauri Carbon 2 timelapses in the Media panel."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from .cc2.client import ElegooCC2Client
from .const import DOMAIN
from .timelapse_media import (
    TimelapseError,
    async_get_timelapse_file,
    cc2_client,
    timelapse_url,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cc2.timelapse import CC2PrintTask

# ECC2_0.4_<model>_<filament>_<layer height>_<slicer estimate>.gcode
_SLICER_NAME = re.compile(r"^ECC2_[\d.]+_(?P<model>.+?)_[^_]*_[\d.]+_[\dhms]+$")


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the timelapse media source."""
    return ElegooTimelapseSource(hass)


def job_title(task: CC2PrintTask) -> str:
    """Return a readable job name with the time it finished."""
    stem = task.file_name.removesuffix(".gcode")
    match = _SLICER_NAME.match(stem)
    name = (match.group("model") if match else stem).replace("+", " ")
    finished = dt_util.as_local(dt_util.utc_from_timestamp(task.end_time))
    return f"{name} · {finished:%d %b %H:%M}"


class ElegooTimelapseSource(MediaSource):
    """Timelapses recorded by Centauri Carbon 2 printers."""

    name = "Printer timelapses"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Make sure the video is local, then hand out its URL."""
        entry_id, _, task_id = item.identifier.partition("/")
        if not task_id:
            msg = "Not a timelapse"
            raise Unresolvable(msg)
        try:
            await async_get_timelapse_file(self.hass, entry_id, task_id)
        except TimelapseError as err:
            raise Unresolvable(str(err)) from err
        return PlayMedia(timelapse_url(entry_id, task_id), "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """List printers, then the jobs that have a timelapse, newest first."""
        if not item.identifier:
            return self._printers()
        entry_id, _, task_id = item.identifier.partition("/")
        if task_id:
            msg = "A timelapse cannot be browsed into"
            raise BrowseError(msg)
        try:
            client = cc2_client(self.hass, entry_id)
        except TimelapseError as err:
            raise BrowseError(str(err)) from err
        return self._jobs(entry_id, client)

    def _printers(self) -> BrowseMediaSource:
        entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and isinstance(entry.runtime_data.api.client, ElegooCC2Client)
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=self.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=entry.entry_id,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=entry.title,
                    can_play=False,
                    can_expand=True,
                )
                for entry in entries
            ],
        )

    def _jobs(self, entry_id: str, client: ElegooCC2Client) -> BrowseMediaSource:
        tasks = [t for t in client.printer_data.print_tasks if t.has_timelapse]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=entry_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=self.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{entry_id}/{task.task_id}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=job_title(task),
                    can_play=True,
                    can_expand=False,
                )
                for task in reversed(tasks)
            ],
        )
