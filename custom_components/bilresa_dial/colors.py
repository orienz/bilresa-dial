"""Shared palette and a reversible, gentle color-warmth adjustment."""

from math import atan2, cos, exp, pi, sin

# Name, hue (None selects white), complementary white in kelvin.
PALETTE = (
    ("Red", 0, 2202),
    ("Orange", 30, 2400),
    ("Yellow", 60, 2700),
    ("White", None, 3200),
    ("Green", 135, 4000),
    ("Cyan", 180, 5500),
    ("Blue", 225, 6500),
    ("Violet", 270, 5000),
    ("Magenta", 315, 3200),
)
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


def shift_hue(hue, brightness_delta):
    """Keep the color family; move toward warm orange or cool blue."""
    angle = (hue - 30) * pi / 180
    factor = exp(0.18 * brightness_delta / 251)
    return round((2 * atan2(sin(angle / 2) * factor, cos(angle / 2)) * 180 / pi + 30) % 360, 3)


def matching_white(hue):
    """Choose a complementary white; saturated colors do not have a Kelvin value."""
    colors = [item for item in PALETTE if item[1] is not None]
    return min(colors, key=lambda item: abs((item[1] - hue + 180) % 360 - 180))[2]
