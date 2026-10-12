"""Plain colour names for filament hex colours ("#FF8800" -> "Orange")."""

from __future__ import annotations

import colorsys
import re

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
BLACK_BELOW = 0.12  # lightness
GREY_BELOW = 0.15  # saturation
WHITE_ABOVE = 0.88  # lightness of a coloured (saturated) shade
# grey shades by lightness, darkest first: (lightness at or below, name)
_GREYS = ((0.3, "Dark grey"), (0.6, "Grey"), (0.85, "Light grey"), (1.0, "White"))
# hue bands, in degrees: (hue below, darker name, lighter name, lightness split)
_HUES = (
    (15, "Red", "Pink", 0.7),
    (40, "Brown", "Orange", 0.35),
    (65, "Gold", "Yellow", 0.4),
    (160, "Green", "Green", 0.0),
    (200, "Cyan", "Cyan", 0.0),
    (255, "Blue", "Blue", 0.0),
    (310, "Purple", "Purple", 0.0),
    (345, "Magenta", "Pink", 0.6),
    (360, "Red", "Pink", 0.7),
)


def color_name(hex_color: str | None) -> str | None:
    """Return a plain name for a hex colour, or None if it isn't one."""
    match = _HEX.match((hex_color or "").strip())
    if not match:
        return None
    raw = match.group(1)
    r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if light < BLACK_BELOW:
        return "Black"
    if sat < GREY_BELOW:
        return next(name for limit, name in _GREYS if light <= limit)
    if light > WHITE_ABOVE:
        return "White"
    degrees = hue * 360
    band = next((b for b in _HUES if degrees < b[0]), _HUES[-1])
    _, darker, lighter, split = band
    return lighter if light > split else darker
