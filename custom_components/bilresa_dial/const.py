"""Configuration shared by the UI and controller."""

import math
import re
from dataclasses import dataclass

DOMAIN = "bilresa_dial"
DEFAULTS = {
    "lights": [],
    "reverse_direction": False,
    "step_percent": 3.0,
    "minimum_percent": 1.0,
    "maximum_percent": 100.0,
    "lit_only": True,
    "brighten_from_off": True,
    "brightness_transition": 0.0,
    "single_press": "toggle",
    "double_press": "temperature_cycle",
    "temperature_steps": "2202,2400,2700,3200,4500",
    "temperature_transition": 0.7,
}
DIMMABLE_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
TARGET_MEMORY_SECONDS = 5.0
REQUEST_MAX_AGE_SECONDS = 5.0


def validate_options(values: dict) -> dict:
    """Validate again on save/load, including calls that bypass the form."""
    result = {**DEFAULTS, **values}
    lights = result["lights"]
    if (
        not isinstance(lights, list)
        or not lights
        or any(not isinstance(e, str) or not re.fullmatch(r"light\.[a-z0-9_]+", e) for e in lights)
    ):
        raise ValueError("lights_required")
    result["lights"] = list(dict.fromkeys(lights))
    for key, low, high in [
        ("step_percent", 0.1, 25),
        ("minimum_percent", 0.1, 100),
        ("maximum_percent", 0.1, 100),
        ("brightness_transition", 0, 5),
        ("temperature_transition", 0, 10),
    ]:
        value = result[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError("invalid_settings")
    if result["minimum_percent"] > result["maximum_percent"]:
        raise ValueError("brightness_range")
    for key in ("reverse_direction", "lit_only", "brighten_from_off"):
        if not isinstance(result[key], bool):
            raise ValueError("invalid_settings")
    if result["single_press"] not in ("toggle", "none") or result["double_press"] not in ("temperature_cycle", "none"):
        raise ValueError("invalid_settings")
    try:
        steps = [int(x.strip()) for x in result["temperature_steps"].split(",")]
        if not 2 <= len(steps) <= 16 or steps != sorted(set(steps)) or not all(1000 <= x <= 12000 for x in steps):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as err:
        raise ValueError("temperature_steps") from err
    result["temperature_steps"] = ",".join(map(str, steps))
    return {key: result[key] for key in DEFAULTS}


@dataclass(frozen=True)
class Settings:
    lights: tuple[str, ...]
    reverse_direction: bool
    step: int
    minimum: int
    maximum: int
    lit_only: bool
    brighten_from_off: bool
    brightness_transition: float
    single_press: str
    double_press: str
    temperature_steps: tuple[int, ...]
    temperature_transition: float

    @classmethod
    def from_options(cls, options: dict) -> "Settings":
        values = validate_options(options)
        return cls(
            lights=tuple(values["lights"]),
            reverse_direction=values["reverse_direction"],
            step=max(1, round(values["step_percent"] * 255 / 100)),
            minimum=max(1, round(values["minimum_percent"] * 255 / 100)),
            maximum=max(1, round(values["maximum_percent"] * 255 / 100)),
            lit_only=values["lit_only"],
            brighten_from_off=values["brighten_from_off"],
            brightness_transition=values["brightness_transition"],
            single_press=values["single_press"],
            double_press=values["double_press"],
            temperature_steps=tuple(map(int, values["temperature_steps"].split(","))),
            temperature_transition=values["temperature_transition"],
        )
