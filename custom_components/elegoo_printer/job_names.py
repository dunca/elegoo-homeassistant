"""
Readable names for Centauri Carbon 2 jobs, parsed from their G-code file names.

ElegooSlicer names a CC2 job ``ECC2_{nozzle}_{model}_{filament}_{layer}_{time}.gcode``
(e.g. ``ECC2_0.4_DHT22-A-case_Elegoo PLA _0.2_1h43m.gcode``). Projects sliced
from a CLI-flattened preset carry ``resolved(<project>.3mf)`` after the filament,
and files sent from the Elegoo app or uploaded without a project are named by a
download id (a UUID) or a cloud id (a long number).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_EXTENSION = re.compile(r"\.gcode$", re.IGNORECASE)
_PREFIX = re.compile(r"^ECC2_\d+(?:\.\d+)?_")
_RESOLVED = re.compile(r" ?resolved\([^)]*\)")
_TAIL = re.compile(
    r"_(?P<filament>[^_]*)_(?P<layer>\d+(?:\.\d+)?)_"
    r"(?P<time>(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?)$"
)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CLOUD_ID = re.compile(r"^\d{12,}$")
MAX_STATE = 255


@dataclass(frozen=True)
class JobName:
    """A job's readable parts."""

    model: str
    filament: str
    layer_height: float | None
    estimate: str

    @property
    def display(self) -> str:
        """Return ``model (filament)``, or the model alone."""
        text = f"{self.model} ({self.filament})" if self.filament else self.model
        return text[:MAX_STATE]


def display_model(raw: str) -> str:
    """Return a readable model name for the name part of a file name."""
    raw = raw.strip()
    if _UUID.match(raw):
        return f"Upload {raw[:8]}"
    if _CLOUD_ID.match(raw):
        return "Cloud upload"
    readable = re.sub(r"\s+", " ", raw.replace("_", " ").replace("+", " ")).strip()
    return readable or raw


def parse_job_name(file_name: str | None) -> JobName | None:
    """Parse a CC2 G-code file name; None for an empty name."""
    if not file_name or not file_name.strip():
        return None
    base = _RESOLVED.sub("", _EXTENSION.sub("", file_name.strip()))
    prefix = _PREFIX.match(base)
    body = base[prefix.end() :] if prefix else base
    tail = _TAIL.search(body)
    if tail is None or not prefix:
        return JobName(display_model(body), "", None, "")
    return JobName(
        model=display_model(body[: tail.start()]),
        filament=tail["filament"].strip(),
        layer_height=float(tail["layer"]),
        estimate=tail["time"],
    )
