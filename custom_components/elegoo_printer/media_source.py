"""
Browse and play Centauri Carbon 2 timelapses in the Media panel.

Also lists the spaghetti check frames kept per print (``spaghetti_frames``):
``frames/<entry_id>`` lists a printer's prints, ``frames/<entry_id>/<print>``
its frames, and ``frames/<entry_id>/<print>/<file>`` is one frame.
"""

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

from . import spaghetti_frames
from .cc2.client import ElegooCC2Client
from .const import DOMAIN
from .timelapse_media import (
    TimelapseError,
    async_get_timelapse_file,
    cc2_client,
    is_playable,
    timelapse_url,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cc2.timelapse import CC2PrintTask
    from .spaghetti_frames import PrintSummary

# ECC2_0.4_<model>_<filament>_<layer height>_<slicer estimate>.gcode
_SLICER_NAME = re.compile(r"^ECC2_[\d.]+_(?P<model>.+?)_[^_]*_[\d.]+_[\dhms]+$")
FRAMES = "frames"
FRAMES_TITLE = "Spaghetti check frames"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the timelapse media source."""
    return ElegooTimelapseSource(hass)


def model_name(file_name: str) -> str:
    """Return the model's name from a sliced file's name."""
    stem = file_name.removesuffix(".gcode")
    match = _SLICER_NAME.match(stem)
    return (match.group("model") if match else stem).replace("+", " ")


def job_title(task: CC2PrintTask) -> str:
    """Return a readable job name with the time it finished."""
    finished = dt_util.as_local(dt_util.utc_from_timestamp(task.end_time))
    return f"{model_name(task.file_name)} · {finished:%d %b %H:%M}"


def frames_print_title(summary: PrintSummary) -> str:
    """Return a stored print's name, start time, frames and highest score."""
    started = dt_util.as_local(dt_util.utc_from_timestamp(summary.start))
    name = model_name(summary.name) if summary.name else "Print"
    return (
        f"{name} · {started:%d %b %H:%M} · {summary.count} frames"
        f" · max {round(summary.max_score * 100)} %"
    )


def frame_title(frame: dict) -> str:
    """Return a frame's time and score."""
    taken = dt_util.as_local(dt_util.utc_from_timestamp(float(frame.get("t") or 0)))
    return f"{taken:%H:%M:%S} · {round(float(frame.get('s') or 0) * 100)} %"


class ElegooTimelapseSource(MediaSource):
    """Timelapses recorded by Centauri Carbon 2 printers."""

    name = "Printer timelapses"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Make sure the video is local, then hand out its URL."""
        if item.identifier.startswith(f"{FRAMES}/"):
            return await self._resolve_frame(item.identifier)
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
        if item.identifier.startswith(f"{FRAMES}/"):
            return await self._browse_frames(item.identifier)
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
        tasks = [t for t in client.printer_data.print_tasks if is_playable(t)]
        folders = []
        if spaghetti_frames.prints_of(entry_id):
            folders.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{FRAMES}/{entry_id}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.IMAGE,
                    title=FRAMES_TITLE,
                    can_play=False,
                    can_expand=True,
                )
            )
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
                *folders,
                *(
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
                ),
            ],
        )

    async def _browse_frames(self, identifier: str) -> BrowseMediaSource:
        """List a printer's stored prints, or one print's frames."""
        parts = identifier.split("/")
        if len(parts) == 2:  # noqa: PLR2004 - frames/<entry_id>
            entry_id = parts[1]
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=identifier,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.IMAGE,
                title=FRAMES_TITLE,
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
                children=[
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{identifier}/{summary.print_id}",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type=MediaType.IMAGE,
                        title=frames_print_title(summary),
                        can_play=False,
                        can_expand=True,
                    )
                    for summary in spaghetti_frames.prints_of(entry_id)
                ],
            )
        if len(parts) != 3:  # noqa: PLR2004 - frames/<entry_id>/<print>
            msg = "A frame cannot be browsed into"
            raise BrowseError(msg)
        _, entry_id, print_id = parts
        meta = await spaghetti_frames.async_read_print(self.hass, entry_id, print_id)
        if meta is None:
            msg = "No frames are kept for that print"
            raise BrowseError(msg)
        title = next(
            (
                frames_print_title(p)
                for p in spaghetti_frames.prints_of(entry_id)
                if p.print_id == print_id
            ),
            print_id,
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.IMAGE,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{identifier}/{frame['f']}",
                    media_class=MediaClass.IMAGE,
                    media_content_type="image/jpeg",
                    title=frame_title(frame),
                    can_play=True,
                    can_expand=False,
                    thumbnail=spaghetti_frames.frame_url(
                        entry_id, print_id, frame["f"], token=True
                    ),
                )
                for frame in meta.get("frames") or []
                if isinstance(frame, dict) and frame.get("f")
            ],
        )

    async def _resolve_frame(self, identifier: str) -> PlayMedia:
        """Hand out the URL of one stored frame (signed by Home Assistant)."""
        parts = identifier.split("/")
        if len(parts) != 4:  # noqa: PLR2004 - frames/<entry_id>/<print>/<file>
            msg = "Not a frame"
            raise Unresolvable(msg)
        _, entry_id, print_id, name = parts
        meta = await spaghetti_frames.async_read_print(self.hass, entry_id, print_id)
        if meta is None or not any(
            isinstance(fr, dict) and fr.get("f") == name
            for fr in meta.get("frames") or []
        ):
            msg = "That frame is gone"
            raise Unresolvable(msg)
        return PlayMedia(
            spaghetti_frames.frame_url(entry_id, print_id, name, token=False),
            "image/jpeg",
        )
