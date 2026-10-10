"""Light policy and bounded per-light output; independent of HA internals."""

import asyncio
import logging
from dataclasses import dataclass, replace
from time import monotonic

from .colors import COLOR_MODES, PALETTE, matching_white, shift_hue
from .const import DIMMABLE_MODES, REQUEST_MAX_AGE_SECONDS, TARGET_MEMORY_SECONDS, Settings

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Light:
    entity_id: str
    state: str
    brightness: int | None = None
    modes: frozenset[str] = frozenset()
    kelvin: int | None = None
    minimum_kelvin: int = 2000
    maximum_kelvin: int = 6500
    transition_supported: bool = False

    color_mode: str | None = None
    hs: tuple[float, float] | None = None
    white_only: bool = False

    @property
    def supports_color(self):
        return not self.white_only and bool(self.modes & COLOR_MODES)

    @property
    def colored(self):
        return self.supports_color and self.color_mode not in (None, "color_temp", "white") and self.hs is not None

    @property
    def available(self):
        return self.state in ("on", "off")

    @property
    def dimmable(self):
        return bool(self.modes & DIMMABLE_MODES) or (not self.modes and self.brightness is not None)


@dataclass(frozen=True)
class Command:
    owner: object
    entity_id: str
    service: str
    data: dict
    created: float
    sequence: int


class OutputManager:
    """One in-flight call and one replaceable pending command per light.

    Shared across config entries, so two remotes do not create parallel calls
    to the same light. Unload drops pending work, without cancelling an already
    submitted device command. HA owns cancellation at shutdown.
    """

    def __init__(self, send, create_task=asyncio.create_task, clock=monotonic):
        self.send = send
        self.create_task = create_task
        self.clock = clock
        self.pending = {}
        self.tasks = {}
        self.latest = {}
        self.owners = set()
        self.sequence = 0
        self.completed = self.failed = self.superseded = self.expired = self.skipped = 0

    def add_owner(self, owner):
        self.owners.add(owner)

    def remove_owner(self, owner):
        self.owners.discard(owner)
        self.discard_pending(owner)
        for entity, command in list(self.latest.items()):
            if command.owner is owner:
                del self.latest[entity]

    def discard_pending(self, owner):
        for entity, command in list(self.pending.items()):
            if command.owner is owner:
                self.superseded += 1
                del self.pending[entity]

    def submit(self, owner, entity, service, data):
        if owner not in self.owners:
            return None
        self.sequence += 1
        command = Command(owner, entity, service, dict(data), self.clock(), self.sequence)
        if entity in self.pending:
            self.superseded += 1
        self.pending[entity] = self.latest[entity] = command
        if entity not in self.tasks:
            # create_task must not eagerly execute: registration precedes drain.
            self.tasks[entity] = self.create_task(self._drain(entity))
        return command.sequence

    async def _drain(self, entity):
        try:
            while command := self.pending.pop(entity, None):
                if command.owner not in self.owners:
                    continue
                if not 0 <= self.clock() - command.created < REQUEST_MAX_AGE_SECONDS:
                    self.expired += 1
                    continue
                try:
                    sent = await self.send(command)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failed += 1
                    _LOGGER.warning("Light command failed for %s; later input can retry", entity, exc_info=True)
                else:
                    if sent:
                        self.completed += 1
                    else:
                        self.skipped += 1
        finally:
            self.tasks.pop(entity, None)

    async def wait_idle(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))


@dataclass
class Target:
    time: float
    operation: str
    entities: tuple[str, ...]
    value: int
    sequences: dict[str, int]


class Controller:
    def __init__(self, settings: Settings, read_lights, outputs: OutputManager, clock=monotonic):
        self.settings = settings
        self.read_lights = read_lights
        self.outputs = outputs
        self.clock = clock
        self.owner = object()
        self.outputs.add_owner(self.owner)
        self.target = None
        self.palette_index = -1

    def close(self):
        self.outputs.remove_owner(self.owner)
        self.target = None

    def _fresh(self, operation, entities):
        target = self.target
        return bool(
            target
            and target.operation == operation
            and target.entities == tuple(sorted(entities))
            and 0 <= self.clock() - target.time < TARGET_MEMORY_SECONDS
            and all(
                (command := self.outputs.latest.get(entity)) is not None
                and command.owner is self.owner
                and command.sequence == sequence
                for entity, sequence in target.sequences.items()
            )
        )

    def _effective(self, light):
        """Include our recent target while the bulb's state report catches up."""
        command = self.outputs.latest.get(light.entity_id)
        if (
            command is None
            or command.owner is not self.owner
            or command.service != "turn_on"
            or not 0 <= self.clock() - command.created < TARGET_MEMORY_SECONDS
        ):
            return light
        data = command.data
        changes = {}
        if "brightness" in data:
            changes["brightness"] = data["brightness"]
        if "hs_color" in data:
            changes.update(hs=tuple(data["hs_color"]), color_mode="hs", kelvin=None)
        elif "color_temp_kelvin" in data:
            changes.update(kelvin=data["color_temp_kelvin"], color_mode="color_temp")
        return replace(light, **changes)

    def _submit(self, operation, lights, value=0):
        lights = [self._effective(light) for light in lights]
        # XY-only bulbs emulate the explicit white choice. Keep that choice
        # while their reported color still looks like the warm white we sent.
        lights = [
            replace(light, color_mode="color_temp")
            if (
                self.palette_index == 3
                and light.supports_color
                and "color_temp" not in light.modes
                and light.hs is not None
                and 15 < light.hs[0] < 50
                and light.hs[1] < 98
            )
            else light
            for light in lights
        ]
        self.outputs.discard_pending(self.owner)
        sequences = {}
        mood = next((light.hs[0] for light in lights if light.colored), None)
        for light in lights:
            service, data = ("turn_off", {}) if operation == "off" else ("turn_on", {})
            transition = 0.0
            if operation == "brightness":
                data["brightness"] = value
                if light.colored:
                    hue, saturation = light.hs
                    if self.settings.temperature_follows_brightness:
                        hue = shift_hue(hue, value - (light.brightness if light.brightness is not None else value))
                    # Carry color into the newest pending command, including when
                    # a double press has not reached the bulb yet.
                    data["hs_color"] = [hue, saturation]
                elif self.settings.temperature_follows_brightness and (
                    ("color_temp" in light.modes and not light.supports_color) or light.color_mode == "color_temp"
                ):
                    fraction = max(0.0, min(1.0, (value - 3) / (255 - 3)))
                    ceiling = (
                        matching_white(mood)
                        if mood is not None and not light.supports_color
                        else self.settings.brightness_max_kelvin
                    )
                    ceiling = max(light.minimum_kelvin, min(light.maximum_kelvin, ceiling))
                    data["color_temp_kelvin"] = round(
                        light.minimum_kelvin + fraction * (ceiling - light.minimum_kelvin)
                    )
                elif light.color_mode == "color_temp" and light.kelvin and light.supports_color:
                    data["color_temp_kelvin"] = light.kelvin
                transition = self.settings.brightness_transition
            elif operation == "palette":
                _, hue, kelvin = PALETTE[value]
                if light.supports_color and hue is not None:
                    data["hs_color"] = [hue, 100]
                else:
                    data["color_temp_kelvin"] = max(light.minimum_kelvin, min(light.maximum_kelvin, kelvin))
                transition = self.settings.temperature_transition
            elif operation == "temperature":
                data["color_temp_kelvin"] = max(light.minimum_kelvin, min(light.maximum_kelvin, value))
                transition = self.settings.temperature_transition
            if light.transition_supported:
                data["transition"] = transition
            sequence = self.outputs.submit(self.owner, light.entity_id, service, data)
            if sequence is not None:
                sequences[light.entity_id] = sequence
        if sequences:
            self.target = Target(self.clock(), operation, tuple(sorted(sequences)), value, sequences)

    def scroll(self, steps: int, up: bool):
        if type(steps) is not int or not 1 <= steps <= 255:
            return
        if self.settings.reverse_direction:
            up = not up
        available = [light for light in self.read_lights() if light.available and light.dimmable]
        lit = [light for light in available if light.state == "on"]
        pending_from_dark = self._fresh("brightness", [light.entity_id for light in available]) and any(
            light.entity_id in self.outputs.tasks for light in available
        )
        if self.settings.lit_only and lit:
            selected = lit
        elif lit or (up and self.settings.brighten_from_off) or pending_from_dark:
            selected = available
        else:
            return
        if not selected:
            return
        if self._fresh("brightness", [light.entity_id for light in selected]):
            base = self.target.value
        else:
            levels = [light.brightness for light in selected if light.state == "on" and light.brightness is not None]
            base = round(sum(levels) / len(levels)) if levels else 0
        value = max(
            self.settings.minimum, min(self.settings.maximum, base + (steps if up else -steps) * self.settings.step)
        )
        if value != base or (
            self.settings.temperature_follows_brightness
            and not self._fresh("brightness", [light.entity_id for light in selected])
        ):
            self._submit("brightness", selected, value)

    def press(self, gesture):
        available = [light for light in self.read_lights() if light.available]
        if gesture == "multi_press_1" and self.settings.single_press == "toggle":
            if not available:
                return
            target = self.target
            if (
                target
                and target.operation in ("on", "off", "brightness")
                and self._fresh(target.operation, target.entities)
            ):
                is_on = target.operation != "off"
            else:
                is_on = any(light.state == "on" for light in available)
            self._submit("off" if is_on else "on", available)
        elif gesture == "multi_press_2" and self.settings.double_press in ("temperature_cycle", "color_cycle"):
            all_lights = self.read_lights()
            if self.settings.double_press == "color_cycle" and any(light.supports_color for light in all_lights):
                selected = [
                    light
                    for light in available
                    if light.state == "on" and (light.supports_color or "color_temp" in light.modes)
                ]
                if selected:
                    self.palette_index = (self.palette_index + 1) % len(PALETTE)
                    self._submit("palette", selected, self.palette_index)
                return
            selected = [light for light in available if light.state == "on" and "color_temp" in light.modes]
            if not selected:
                return
            if self._fresh("temperature", [light.entity_id for light in selected]):
                current = self.target.value
            else:
                current = next((light.kelvin for light in selected if light.kelvin and light.kelvin > 0), None)
            steps = self.settings.temperature_steps
            if current:
                nearest = min(range(len(steps)), key=lambda index: abs(1_000_000 / current - 1_000_000 / steps[index]))
                kelvin = steps[(nearest + 1) % len(steps)]
            else:
                kelvin = steps[0]
            self._submit("temperature", selected, kelvin)
