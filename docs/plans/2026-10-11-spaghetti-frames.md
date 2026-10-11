# Spaghetti Check Frames Plan

**Goal:** Keep the camera frame behind every scheduled spaghetti-detection check, filed under the Centauri Carbon 2 job it belongs to, so that after a failed print the user can step through exactly what the detector saw. Replaces a stopgap (a shell script run by an automation, writing under `www/`) with a proper part of the integration.

**Architecture:** A new module `spaghetti_frames.py` listens for the `elegoo_spaghetti_detection_result` event fired by the third-party [ha-elegoo-spaghetti-detection](https://github.com/hepter/ha-elegoo-spaghetti-detection) integration (not modified), fetches one JPEG from the printer's snapshot endpoint per scheduled check and stores it under `<config>/elegoo_printer/spaghetti_frames/<print id>/` with an `index.json`. Authenticated views serve a viewer page, the indexes and the frames; a media-source branch lists them in the Media panel; the Print History sensor's `jobs` gain per-job frame info and a viewer link.

**Tech Stack:** Python, Home Assistant event bus, aiohttp (HA's shared session), `HomeAssistantView`, media source, plain HTML/JS for the viewer.

---

## Behaviour carried over from the stopgap

The user already approved the stopgap's behaviour; it is kept as is:

| What | Value |
|------|-------|
| One frame per check | only scheduled checks (`manual: false`) |
| Frame name | `HHMMSS_<score × 10000, 5 digits>.jpg` (local time) |
| Per-print index | `index.json` with `frames: [{t, f, s, n}]` (time, file, score, detection count) plus `start`, `end`, `task`, `name` |
| Prints kept | last 10 |
| Frames per print | 1,500; past that every other frame scored below 0.1 is dropped, so a long print keeps its whole timeline at a coarser step |
| Total size | 1.5 GB; the oldest prints go first, never the one being written |
| New print without a job id | no frame for 15 min starts a new print |
| Viewer | print picker, big frame with time + score tag, clickable score strip with dashed 30 % / 50 % lines, scrub slider, prev/play/next, "Highest", ←/→/space, auto-follow of the live print, HA theme colours when embedded |

## Event contract (third party, read-only)

Read from the detector's `runtime.py`: `elegoo_spaghetti_detection_result` fires after every *successful* check (a failed Obico call sets `last_error` and fires nothing). Data: `config_entry, detector, name, camera, manual, printer_state, confidence, raw_score, detected, detections, image_url, last_error, last_run, next_run, status`. `raw_score` and `confidence` are equal, 0..1; `detections` is a count; `camera` is the detector's camera entity id; `image_url` is the URL the detector sent to Obico (the snapshot URL when one is configured).

## Decisions

### Capture

- Listener per CC2 config entry, registered in `async_setup_entry` and removed on unload.
- **Which printer an event is for** (only matters with more than one CC2): the printer whose host is the host of `image_url`, else the printer that owns the `camera` entity, else the only CC2 that is capturing. An event that matches none is ignored.
- **One GET per check, no more.** `http://<host>:8080/snapshot`, where `<host>` is the entry's `connection_host` (the printer IP, or the forward proxy when `proxy_host` is set, which forwards 8080 like the camera). HA's shared aiohttp session, a 10 s total timeout, no retries, body capped at 5 MB, must start with the JPEG SOI marker. If the previous capture of that printer is still running when the next event arrives, the event is skipped rather than queued, so the printer (108 MB RAM) never sees two requests from us at once.
- **Which print:** the coordinator's current `print_info.task_id` (the CC2 job uuid, the same id the job history uses). Without one, the frame joins the printer's latest print if that ended less than 15 min ago, else starts a new one named `print-<UTC yyyymmdd-hhmmss>`. When a job id turns up for a print that started without one (inside the gap), the folder is renamed to the job id so it lines up with the history.
- All file I/O runs in the executor. Captures are serialised by one lock, and the executor works on a snapshot of the in-memory print summaries and hands back the changes, which are applied on the event loop. Entity attributes read only the in-memory summaries (no I/O every 2 s).

### Storage

- `<config>/elegoo_printer/spaghetti_frames/<print id>/` (outside `www`, never served without auth). Print ids are validated against `^[A-Za-z0-9_-]{1,64}$`, frame names against `^\d{6}_\d{5}\.jpg$`.
- JPEGs and indexes are written to a `.part` file and `os.replace`d into place.
- Limits apply per printer (each print's `index.json` records its `entry_id`); with one printer this is the stopgap exactly.
- Deleting a job with `elegoo_printer.delete_history_job` also deletes its frames, the way it already deletes its timelapse. The job archive itself is not used for pruning: it keeps every job forever, while frames are capped by count and size.

### Options (CC2 options flow)

| Key | Default | Range |
|-----|---------|-------|
| `spaghetti_frames_enabled` | on when an `elegoo_spaghetti_detection` entry exists | bool |
| `spaghetti_frames_keep_prints` | 10 | 1–100 |
| `spaghetti_frames_max_per_print` | 1500 | 100–20000 |
| `spaghetti_frames_max_mb` | 1500 | 50–100000 |

When the switch has never been set the listener runs anyway: without the detector no event ever arrives, so "on when present" and "on" behave the same, and a detector added later is picked up without revisiting the options. Turning it off explicitly stops capture; stored frames stay until they age out or the job is deleted.

### Serving and auth

Home Assistant has no session cookie. A signed-in browser proves itself only with an `Authorization` header (sent by the frontend's own `fetch`/websocket) or a signed path (`authSig`, a short-lived JWT bound to one exact path). Neither reaches a page opened by a dashboard:

- A button-card `url` action (new tab) or an `iframe` card is a plain browser navigation: no header, so a `requires_auth = True` view answers 401.
- A signed path covers only that one URL and expires (HA uses 30 s for media). The viewer then loads `index.json` and hundreds of frames by other URLs, which would each need their own signature, and a long-lived signature in the sensor attributes would have to be rotated, rewriting the attribute every time.

So the frames follow the pattern the integration already uses for job previews (`job_previews.py`), which is how Home Assistant itself serves `entity_picture` for camera and image entities: an **access token** carried in the URL. The token is random per Home Assistant run (`secrets.token_urlsafe`), compared in constant time, and published only in the Print History sensor's attributes, which only signed-in users can read. The views accept either a request HA's auth middleware already authenticated (header or a valid `authSig`) or the token, and return 401 otherwise. The viewer reads the token from its own URL and adds it to every request it makes, so it works the same in an iframe, a new tab and the companion app's external browser. A token stops working when Home Assistant restarts; the viewer then says to reopen it from the dashboard, which will have the new link.

The timelapse view keeps `requires_auth = True` because its URLs only ever reach the dashboard as media-source ids, which the frontend resolves into a signed URL for one video. The new media-source branch does the same for single frames: a resolved frame is the plain view URL, which HA signs.

Hardening on the viewer page: `Content-Security-Policy` with `frame-ancestors 'self'` (only HA's own dashboards can frame it), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and `Cache-Control: no-store` on the HTML and indexes; frames are `private`.

URLs:

| URL | Serves |
|-----|--------|
| `/api/elegoo_printer/frames/{entry_id}/viewer?token=…&print=<id>` | viewer page |
| `/api/elegoo_printer/frames/{entry_id}/index.json` | that printer's prints, newest first |
| `/api/elegoo_printer/frames/{entry_id}/{print_id}/index.json` | one print's frames |
| `/api/elegoo_printer/frames/{entry_id}/{print_id}/{HHMMSS_SSSSS}.jpg` | one frame |

### Media panel

Under each printer, a "Spaghetti check frames" folder (`frames/<entry_id>`) lists prints newest first (`frames/<entry_id>/<print_id>`), each listing its frames as images titled with time and score (`frames/<entry_id>/<print_id>/<file>`). Existing timelapse ids (`<entry_id>/<task_id>`) are unchanged.

### History sensor

Each job in `sensor.<printer>_print_history`'s `jobs` gains `frames` (count, 0 when none), `frames_max` (highest score, 0..1, or null) and `frames_url` (viewer link for that print, or null).

---

## Tasks

1. **Plan** (this file) and the plans index.
2. **Frame store + capture** — `spaghetti_frames.py`: store (executor), capture (event loop), event matching, limits; wiring in `__init__.py`; constants in `const.py`. Tests: capture with and without a task id, the gap rule and adoption, thinning, per-print and total caps, skip while busy, ignore manual checks, timeout/non-JPEG.
3. **Serving** — views, viewer page (`static/spaghetti_frames.html`, ported from the stopgap with the token added and nothing personal), registered in `async_setup`. Tests: 401 without auth, token and authenticated requests, path validation, unknown prints.
4. **Media source branch.** Tests: browse and resolve.
5. **History enrichment + delete** — `frames`, `frames_max`, `frames_url` per job; `delete_history_job` removes frames. Tests: enrichment.
6. **Options** — four fields in the CC2 options flow, translations for all 18 locales. Tests: options persisted, defaults.
7. **Docs** — README section with the dashboard snippet, CHANGELOG entry.

## Known limitations

- The snapshot path is fixed at `/snapshot` on port 8080, which a custom camera service on the printer provides; a printer without one keeps no frames (the request fails once per check and is logged at debug level). A configurable path was left out to keep to one request per check.
- The frame is fetched about 0.1 s after the detector's own fetch, so it is the next frame from the camera, not byte-for-byte the one Obico scored (as with the stopgap).
- A token link stops working after a Home Assistant restart (by design); reopen it from the dashboard.
- Prints recorded without a job id (no CC2 job was active) appear in the viewer's picker and the Media panel but not on a History row.
- Frames written by the stopgap under `www/` are not migrated.
