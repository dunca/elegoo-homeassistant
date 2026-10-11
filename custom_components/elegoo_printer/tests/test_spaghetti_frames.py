"""Tests for keeping the camera frame behind every spaghetti check."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import KEY_AUTHENTICATED

from custom_components.elegoo_printer import spaghetti_frames
from custom_components.elegoo_printer.spaghetti_frames import (
    ElegooFrameFileView,
    ElegooFramesView,
    Limits,
    async_capture,
)

ENTRY = "01M3H1QPHWZ1AY5SV86KJTNXC2"
OTHER_ENTRY = "01M3H1QPHWZ1AY5SV86KJTNXC3"
HOST = "192.0.2.15"
JPEG = b"\xff\xd8" + b"\0" * 98  # 100 bytes
T0 = 1_791_000_000.0  # a fixed instant in 2026


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    spaghetti_frames._PRINTS.clear()
    spaghetti_frames._CAPTURING.clear()
    spaghetti_frames._BUSY.clear()
    spaghetti_frames._STATE.update({"loaded": False, "viewer": None, "unsub": None})


def _hass(tmp_path: Path) -> MagicMock:
    hass = MagicMock()
    hass.config.path = lambda *parts: str(Path(tmp_path, *parts))

    async def run(func, *args: object):
        return func(*args)

    hass.async_add_executor_job = run
    return hass


def _root(tmp_path: Path) -> Path:
    return tmp_path / "elegoo_printer" / "spaghetti_frames"


def _capture(  # noqa: PLR0913
    hass, *, now, task_id="", score=0.0, limits=None, entry=ENTRY
):
    async def fake_fetch(*_args: object) -> bytes:
        return JPEG

    async def run():
        with patch.object(spaghetti_frames, "_fetch_snapshot", fake_fetch):
            return await async_capture(
                hass,
                entry_id=entry,
                host=HOST,
                task_id=task_id,
                file_name="ECC2_0.4_Cube_PLA_0.2_1h2m.gcode",
                score=score,
                detections=3 if score else 0,
                limits=limits or Limits(),
                now=now,
            )

    return asyncio.run(run())


def _index(tmp_path: Path, print_id: str) -> dict:
    return json.loads((_root(tmp_path) / print_id / "index.json").read_text())


# --- capture ---------------------------------------------------------------


def test_capture_files_the_frame_under_the_current_job(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    result = _capture(hass, now=T0, task_id="job-1", score=0.1234)
    assert result is not None
    assert result.print_id == "job-1"
    stamp = time.strftime("%H%M%S", time.localtime(T0))
    assert result.frame == f"{stamp}_01234.jpg"
    folder = _root(tmp_path) / "job-1"
    assert (folder / result.frame).read_bytes() == JPEG
    assert not list(folder.glob("*.part"))  # written atomically
    meta = _index(tmp_path, "job-1")
    assert meta["entry_id"] == ENTRY
    assert meta["task"] == "job-1"
    assert meta["name"] == "ECC2_0.4_Cube_PLA_0.2_1h2m.gcode"
    assert meta["frames"] == [{"t": T0, "f": result.frame, "s": 0.1234, "n": 3}]
    summary = spaghetti_frames._PRINTS["job-1"]
    assert (summary.count, summary.max_score, summary.size) == (1, 0.1234, 100)


def test_frames_of_one_job_stay_together_whatever_the_gap(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1")
    _capture(hass, now=T0 + 3600, task_id="job-1")
    assert list(spaghetti_frames._PRINTS) == ["job-1"]
    assert spaghetti_frames._PRINTS["job-1"].count == 2


def test_capture_without_a_job_follows_the_gap_rule(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    first = _capture(hass, now=T0)
    assert first is not None
    assert first.print_id.startswith("print-")
    # 10 min later: still the same print
    second = _capture(hass, now=T0 + 600)
    assert second is not None
    assert second.print_id == first.print_id
    # quiet for more than 15 min: a new print
    third = _capture(hass, now=T0 + 600 + spaghetti_frames.NEW_PRINT_GAP + 1)
    assert third is not None
    assert third.print_id != first.print_id
    assert spaghetti_frames._PRINTS[first.print_id].count == 2
    assert spaghetti_frames._PRINTS[third.print_id].count == 1


def test_a_job_less_print_is_renamed_when_the_job_id_turns_up(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    first = _capture(hass, now=T0)
    assert first is not None
    _capture(hass, now=T0 + 10, task_id="job-2")
    assert list(spaghetti_frames._PRINTS) == ["job-2"]
    assert not (_root(tmp_path) / first.print_id).exists()
    meta = _index(tmp_path, "job-2")
    assert len(meta["frames"]) == 2
    assert meta["task"] == "job-2"
    assert meta["start"] == T0


def test_a_new_job_inside_the_gap_starts_its_own_print(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1")
    _capture(hass, now=T0 + 10, task_id="job-2")
    assert sorted(spaghetti_frames._PRINTS) == ["job-1", "job-2"]


def test_a_frame_without_a_job_joins_the_latest_job_inside_the_gap(
    tmp_path: Path,
) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1")
    result = _capture(hass, now=T0 + 10)
    assert result is not None
    assert result.print_id == "job-1"


def test_an_unsafe_job_id_is_not_used_as_a_folder(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    result = _capture(hass, now=T0, task_id="../../secrets")
    assert result is not None
    assert result.print_id.startswith("print-")
    assert not (tmp_path / "secrets").exists()


def test_long_prints_drop_every_other_low_score_frame(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(max_frames=4)
    scores = [0.01, 0.02, 0.6, 0.03, 0.04]
    for i, score in enumerate(scores):
        _capture(hass, now=T0 + 10 * i, task_id="job-1", score=score, limits=limits)
    kept = [fr["s"] for fr in _index(tmp_path, "job-1")["frames"]]
    # low scores were 0.01, 0.02, 0.03, 0.04: every other one goes
    assert kept == [0.01, 0.6, 0.03]
    jpgs = sorted(p.name for p in (_root(tmp_path) / "job-1").glob("*.jpg"))
    assert len(jpgs) == 3
    summary = spaghetti_frames._PRINTS["job-1"]
    assert (summary.count, summary.max_score, summary.size) == (3, 0.6, 300)


def test_thinning_never_drops_a_scored_frame(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(max_frames=2)
    for i, score in enumerate([0.3, 0.5, 0.2, 0.9]):
        _capture(hass, now=T0 + 10 * i, task_id="job-1", score=score, limits=limits)
    assert [fr["s"] for fr in _index(tmp_path, "job-1")["frames"]] == [
        0.3,
        0.5,
        0.2,
        0.9,
    ]


def test_only_the_last_prints_are_kept(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(keep_prints=2)
    for i in range(3):
        _capture(hass, now=T0 + 3600 * i, task_id=f"job-{i}", limits=limits)
    assert sorted(spaghetti_frames._PRINTS) == ["job-1", "job-2"]
    assert not (_root(tmp_path) / "job-0").exists()


def test_the_total_size_cap_drops_the_oldest_prints(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(max_bytes=250)  # room for two 100-byte frames
    for i in range(3):
        _capture(hass, now=T0 + 3600 * i, task_id=f"job-{i}", limits=limits)
    assert sorted(spaghetti_frames._PRINTS) == ["job-1", "job-2"]
    assert not (_root(tmp_path) / "job-0").exists()


def test_the_size_cap_never_removes_the_print_being_written(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(max_bytes=150)
    _capture(hass, now=T0, task_id="old", limits=limits)
    for i in range(3):
        _capture(hass, now=T0 + 3600 + 10 * i, task_id="live", limits=limits)
    assert list(spaghetti_frames._PRINTS) == ["live"]
    assert spaghetti_frames._PRINTS["live"].count == 3


def test_limits_are_per_printer(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    limits = Limits(keep_prints=1)
    _capture(hass, now=T0, task_id="a", limits=limits)
    _capture(hass, now=T0 + 10, task_id="b", limits=limits, entry=OTHER_ENTRY)
    assert sorted(spaghetti_frames._PRINTS) == ["a", "b"]


def test_stored_prints_are_found_again_after_a_restart(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1", score=0.4)
    before = dict(spaghetti_frames._PRINTS)
    spaghetti_frames._PRINTS.clear()
    asyncio.run(spaghetti_frames.async_load(hass))
    assert before == spaghetti_frames._PRINTS


def test_delete_print_removes_the_frames(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1")
    assert asyncio.run(spaghetti_frames.async_delete_print(hass, "job-1"))
    assert "job-1" not in spaghetti_frames._PRINTS
    assert not (_root(tmp_path) / "job-1").exists()
    assert not asyncio.run(spaghetti_frames.async_delete_print(hass, "../x"))


# --- the snapshot request --------------------------------------------------


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.content = self

    async def iter_chunked(self, _size: int):
        yield self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, object]] = []

    def get(self, url: str, *, timeout: aiohttp.ClientTimeout) -> _Response:
        self.calls.append((url, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


def _fetch(outcome: object) -> tuple[bytes | None, _Session]:
    session = _Session(outcome)
    with patch.object(
        spaghetti_frames, "async_get_clientsession", return_value=session
    ):
        body = asyncio.run(spaghetti_frames._fetch_snapshot(MagicMock(), HOST))
    return body, session


def test_one_get_with_a_ten_second_timeout() -> None:
    body, session = _fetch(_Response(200, JPEG))
    assert body == JPEG
    assert len(session.calls) == 1
    url, timeout = session.calls[0]
    assert url == f"http://{HOST}:8080/snapshot"
    assert timeout.total == spaghetti_frames.FETCH_TIMEOUT == 10


@pytest.mark.parametrize(
    "outcome",
    [
        _Response(503, b""),
        _Response(200, b"<html>not a picture</html>"),
        TimeoutError(),
        aiohttp.ClientConnectionError("refused"),
    ],
)
def test_a_failed_snapshot_is_not_retried(outcome: object) -> None:
    body, session = _fetch(outcome)
    assert body is None
    assert len(session.calls) == 1


def test_an_oversized_snapshot_is_dropped() -> None:
    big = JPEG + b"\0" * spaghetti_frames.MAX_JPEG_BYTES
    body, _ = _fetch(_Response(200, big))
    assert body is None


def test_nothing_is_saved_when_the_snapshot_fails(tmp_path: Path) -> None:
    async def no_frame(*_args: object) -> None:
        return None

    async def run():
        with patch.object(spaghetti_frames, "_fetch_snapshot", no_frame):
            return await async_capture(
                _hass(tmp_path),
                entry_id=ENTRY,
                host=HOST,
                task_id="job-1",
                file_name="",
                score=0.0,
                detections=0,
                limits=Limits(),
                now=T0,
            )

    assert asyncio.run(run()) is None
    assert not spaghetti_frames._PRINTS
    assert not _root(tmp_path).exists()


# --- the detector's events -------------------------------------------------


def _event(**data: object) -> SimpleNamespace:
    base = {
        "manual": False,
        "raw_score": 0.42,
        "confidence": 0.42,
        "detections": 2,
        "camera": "camera.printer",
        "image_url": f"http://{HOST}:8080/snapshot",
    }
    return SimpleNamespace(data={**base, **data})


def _capturing(entry: str = ENTRY, host: str = HOST) -> None:
    spaghetti_frames._CAPTURING[entry] = spaghetti_frames._Capture(
        entry_id=entry,
        host=host,
        limits=Limits(),
        current_job=lambda: ("job-1", "cube.gcode"),
    )


def _event_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_create_background_task.side_effect = lambda coro, **_: coro.close()
    return hass


def test_manual_checks_are_not_kept() -> None:
    _capturing()
    hass = _event_hass()
    spaghetti_frames.async_handle_result(hass, _event(manual=True))
    spaghetti_frames.async_handle_result(hass, _event(manual=None))
    hass.async_create_background_task.assert_not_called()


def test_a_scheduled_check_starts_one_capture_and_skips_while_busy() -> None:
    _capturing()
    hass = _event_hass()
    spaghetti_frames.async_handle_result(hass, _event())
    assert hass.async_create_background_task.call_count == 1
    # the previous fetch has not finished: this check is skipped, not queued
    spaghetti_frames.async_handle_result(hass, _event())
    assert hass.async_create_background_task.call_count == 1
    spaghetti_frames._BUSY.clear()
    spaghetti_frames.async_handle_result(hass, _event())
    assert hass.async_create_background_task.call_count == 2


def test_the_capture_uses_the_coordinators_job_and_the_event_score() -> None:
    _capturing()
    seen: dict = {}

    async def fake_capture(_hass: object, **kwargs: object) -> None:
        seen.update(kwargs)

    async def run() -> None:
        with patch.object(spaghetti_frames, "async_capture", fake_capture):
            await spaghetti_frames._async_capture_event(
                MagicMock(), spaghetti_frames._CAPTURING[ENTRY], _event().data
            )

    spaghetti_frames._BUSY.add(ENTRY)
    asyncio.run(run())
    assert seen["task_id"] == "job-1"
    assert seen["file_name"] == "cube.gcode"
    assert seen["score"] == 0.42
    assert seen["detections"] == 2
    assert seen["host"] == HOST
    assert ENTRY not in spaghetti_frames._BUSY


def test_events_go_to_the_printer_they_are_for() -> None:
    _capturing(ENTRY, HOST)
    _capturing(OTHER_ENTRY, "192.0.2.16")
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(config_entry_id=OTHER_ENTRY)
    with patch.object(spaghetti_frames.er, "async_get", return_value=registry):
        # by the host in the detector's image URL
        assert spaghetti_frames.owner_of(hass, _event().data) == ENTRY
        # by the camera entity's config entry
        data = _event(image_url="http://homeassistant.local:8123/api/x").data
        assert spaghetti_frames.owner_of(hass, data) == OTHER_ENTRY
    registry.async_get.return_value = None
    with patch.object(spaghetti_frames.er, "async_get", return_value=registry):
        # two printers and no clue: nobody
        assert spaghetti_frames.owner_of(hass, data) is None
        # one printer: that one
        del spaghetti_frames._CAPTURING[OTHER_ENTRY]
        assert spaghetti_frames.owner_of(hass, data) == ENTRY


def _entry(options: dict) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = ENTRY
    entry.data = {}
    entry.options = options
    entry.runtime_data.api.printer.connection_host = HOST
    return entry


def test_setup_listens_once_and_undo_stops_listening() -> None:
    hass = MagicMock()
    undo = spaghetti_frames.async_setup_capture(hass, _entry({}))
    assert ENTRY in spaghetti_frames._CAPTURING
    hass.bus.async_listen.assert_called_once()
    assert hass.bus.async_listen.call_args.args[0] == (
        "elegoo_spaghetti_detection_result"
    )
    undo()
    assert not spaghetti_frames._CAPTURING
    hass.bus.async_listen.return_value.assert_called_once()


def test_switching_frames_off_stops_capture() -> None:
    hass = MagicMock()
    spaghetti_frames.async_setup_capture(
        hass, _entry({"spaghetti_frames_enabled": False})
    )
    assert not spaghetti_frames._CAPTURING
    hass.bus.async_listen.assert_not_called()


def test_limits_come_from_the_options() -> None:
    assert spaghetti_frames.limits_from_options({}) == Limits(
        keep_prints=10, max_frames=1500, max_bytes=1500 * 1024 * 1024
    )
    assert spaghetti_frames.limits_from_options(
        {
            "spaghetti_frames_keep_prints": 3,
            "spaghetti_frames_max_per_print": 200,
            "spaghetti_frames_max_mb": 50,
        }
    ) == Limits(keep_prints=3, max_frames=200, max_bytes=50 * 1024 * 1024)


# --- views -----------------------------------------------------------------


def _request(path: str, hass: MagicMock, *, signed_in: bool = False) -> web.Request:
    request = make_mocked_request("GET", path, app={"hass": hass})
    request[KEY_AUTHENTICATED] = signed_in
    return request


def _token() -> str:
    return spaghetti_frames._TOKEN


def test_views_refuse_requests_without_auth_or_token(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    result = _capture(hass, now=T0, task_id="job-1")
    assert result is not None

    async def run() -> list[int]:
        base = f"/api/elegoo_printer/frames/{ENTRY}"
        return [
            (await ElegooFramesView().get(_request(p, hass), ENTRY, n)).status
            for p, n in [
                (f"{base}/viewer", "viewer"),
                (f"{base}/index.json?token=wrong", "index.json"),
            ]
        ] + [
            (
                await ElegooFrameFileView().get(
                    _request(f"{base}/job-1/{name}", hass), ENTRY, "job-1", name
                )
            ).status
            for name in ("index.json", result.frame)
        ]

    assert asyncio.run(run()) == [401, 401, 401, 401]
    assert not ElegooFramesView.requires_auth  # checked in the handler instead


def test_views_serve_signed_in_requests_and_the_token(tmp_path: Path) -> None:
    hass = _hass(tmp_path)
    result = _capture(hass, now=T0, task_id="job-1", score=0.25)
    assert result is not None
    base = f"/api/elegoo_printer/frames/{ENTRY}"

    async def run() -> None:
        index = await ElegooFramesView().get(
            _request(f"{base}/index.json?token={_token()}", hass), ENTRY, "index.json"
        )
        assert index.status == 200
        assert index.headers["Cache-Control"] == "no-store"
        rows = json.loads(index.text)["prints"]
        assert [(r["id"], r["count"], r["max"]) for r in rows] == [("job-1", 1, 0.25)]

        meta = await ElegooFrameFileView().get(
            _request(f"{base}/job-1/index.json", hass, signed_in=True),
            ENTRY,
            "job-1",
            "index.json",
        )
        assert meta.status == 200
        assert json.loads(meta.text)["frames"][0]["f"] == result.frame

        frame = await ElegooFrameFileView().get(
            _request(f"{base}/job-1/{result.frame}?token={_token()}", hass),
            ENTRY,
            "job-1",
            result.frame,
        )
        assert isinstance(frame, web.FileResponse)
        assert frame.headers["Content-Type"] == "image/jpeg"

        viewer = await ElegooFramesView().get(
            _request(f"{base}/viewer?token={_token()}", hass), ENTRY, "viewer"
        )
        assert viewer.status == 200
        assert viewer.content_type == "text/html"
        assert "frame-ancestors 'self'" in viewer.headers["Content-Security-Policy"]
        assert "Spaghetti frames" in viewer.text

    asyncio.run(run())


@pytest.mark.parametrize(
    ("entry", "print_id", "name"),
    [
        (ENTRY, "job-1", "../../../secrets.yaml"),
        (ENTRY, "job-1", "notes.txt"),
        (ENTRY, "..", "index.json"),
        (ENTRY, "job-9", "index.json"),
        (OTHER_ENTRY, "job-1", "index.json"),  # someone else's print
    ],
)
def test_frame_view_only_serves_stored_files(
    tmp_path: Path, entry: str, print_id: str, name: str
) -> None:
    hass = _hass(tmp_path)
    _capture(hass, now=T0, task_id="job-1")

    async def run() -> int:
        request = _request("/x", hass, signed_in=True)
        return (await ElegooFrameFileView().get(request, entry, print_id, name)).status

    assert asyncio.run(run()) == 404


# --- the Media panel -------------------------------------------------------


def test_media_source_lists_prints_and_frames(tmp_path: Path) -> None:
    from homeassistant.components.media_source import MediaSourceItem  # noqa: PLC0415

    from custom_components.elegoo_printer.media_source import (  # noqa: PLC0415
        ElegooTimelapseSource,
    )

    hass = _hass(tmp_path)
    result = _capture(hass, now=T0, task_id="job-1", score=0.5)
    assert result is not None
    source = ElegooTimelapseSource(hass)

    def item(identifier: str) -> MediaSourceItem:
        return MediaSourceItem(hass, "elegoo_printer", identifier, None)

    async def run() -> None:
        prints = await source.async_browse_media(item(f"frames/{ENTRY}"))
        assert [c.identifier for c in prints.children] == [f"frames/{ENTRY}/job-1"]
        assert "Cube" in prints.children[0].title
        frames = await source.async_browse_media(item(f"frames/{ENTRY}/job-1"))
        (frame,) = frames.children
        assert frame.identifier == f"frames/{ENTRY}/job-1/{result.frame}"
        assert frame.media_content_type == "image/jpeg"
        assert f"token={_token()}" in frame.thumbnail
        played = await source.async_resolve_media(item(frame.identifier))
        # no token: Home Assistant signs a resolved URL itself
        assert played.url == (
            f"/api/elegoo_printer/frames/{ENTRY}/job-1/{result.frame}"
        )
        assert played.mime_type == "image/jpeg"

    asyncio.run(run())
