"""Light policy and bounded per-light output; independent of HA internals."""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

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

    def _submit(self, operation, lights, value=0):
        self.outputs.discard_pending(self.owner)
        sequences = {}
        for light in lights:
            service, data = ("turn_off", {}) if operation == "off" else ("turn_on", {})
            transition = 0.0
            if operation == "brightness":
                data["brightness"] = value
                transition = self.settings.brightness_transition
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
        if value != base:
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
        elif gesture == "multi_press_2" and self.settings.double_press == "temperature_cycle":
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
