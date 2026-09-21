"""Configurable BILRESA control using the existing HA Matter client."""

import logging
from dataclasses import asdict

from homeassistant.components.light import LightEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from matter_server.common.models import EventType

from .const import DOMAIN, Settings
from .controller import Controller, Light, OutputManager
from .decoder import PressDecoder, ScrollDecoder
from .discovery import matter_client

_LOGGER = logging.getLogger(__name__)


def read_lights(hass, entity_ids):
    """Expand light groups and deduplicate members; use the normal HA states."""
    result, visited = [], set()

    def visit(entity):
        if entity in visited:
            return
        visited.add(entity)
        state = hass.states.get(entity)
        if state is None:
            return
        attributes = state.attributes
        members = attributes.get("entity_id")
        if (
            isinstance(members, (list, tuple))
            and members
            and all(isinstance(member, str) and member.startswith("light.") for member in members)
        ):
            for member in members:
                visit(member)
            return
        result.append(
            Light(
                entity_id=entity,
                state=state.state,
                brightness=attributes.get("brightness"),
                modes=frozenset(attributes.get("supported_color_modes") or []),
                kelvin=attributes.get("color_temp_kelvin"),
                minimum_kelvin=attributes.get("min_color_temp_kelvin") or 2000,
                maximum_kelvin=attributes.get("max_color_temp_kelvin") or 6500,
                transition_supported=bool(attributes.get("supported_features", 0) & LightEntityFeature.TRANSITION),
            )
        )

    for entity in entity_ids:
        visit(entity)
    return sorted(result, key=lambda light: light.entity_id)


def get_outputs(hass):
    if DOMAIN not in hass.data:

        async def send(command):
            state = hass.states.get(command.entity_id)
            if state is None or state.state not in ("on", "off"):
                return False
            await hass.services.async_call(
                "light",
                command.service,
                {"entity_id": command.entity_id, **command.data},
                blocking=True,
                context=Context(),
            )
            return True

        def create_task(coroutine):
            # Shared between entries: reload must not cancel an in-flight device
            # call. HA will cancel background work when the whole server stops.
            return hass.async_create_background_task(coroutine, "bilresa_dial light output", eager_start=False)

        hass.data[DOMAIN] = OutputManager(send, create_task)
    return hass.data[DOMAIN]


class Runtime:
    def __init__(self, hass, entry, parent):
        self.hass = hass
        self.entry = entry
        self.parent = parent
        self.settings = Settings.from_options(entry.options)
        self.controller = Controller(self.settings, lambda: read_lights(hass, self.settings.lights), get_outputs(hass))
        self.client = self.unsubscribe = self.scroll = self.press = None
        self.scroll_events = self.press_events = 0

    @callback
    def receive(self, _event, data):
        if self.scroll is None:
            return
        event = data if isinstance(data, dict) else asdict(data)
        if steps := self.scroll.feed(event):
            self.scroll_events += 1
            self.controller.scroll(steps, event["endpoint_id"] == self.scroll.first_endpoint)
        if gesture := self.press.feed(event):
            self.press_events += 1
            self.controller.press(gesture)

    @callback
    def unbind(self):
        if self.unsubscribe:
            self.unsubscribe()
        self.client = self.unsubscribe = self.scroll = self.press = None

    @callback
    def bind(self):
        if self.parent.state is not ConfigEntryState.LOADED:
            self.unbind()
            return
        try:
            client = matter_client(self.parent)
            if client is self.client:
                return
            self.unbind()
            scroll = ScrollDecoder(self.entry.data["node_id"], self.entry.data["group"])
            press = PressDecoder(self.entry.data["node_id"], self.entry.data["group"])
            unsubscribe = client.subscribe_events(
                self.receive, event_filter=EventType.NODE_EVENT, node_filter=self.entry.data["node_id"]
            )
        except AttributeError, RuntimeError, TypeError:
            self.unbind()
            _LOGGER.exception("Cannot subscribe to Matter events; check HA compatibility and reload Matter")
            return
        self.client = client
        self.scroll = scroll
        self.press = press
        self.unsubscribe = unsubscribe

    @callback
    def close(self):
        self.unbind()
        self.controller.close()

    @callback
    def entity_renamed(self, event):
        old_id = event.data.get("old_entity_id")
        if event.data.get("action") != "update" or old_id not in self.entry.options["lights"]:
            return
        new_id = event.data["entity_id"]
        options = dict(self.entry.options)
        options["lights"] = list(dict.fromkeys(new_id if entity == old_id else entity for entity in options["lights"]))
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)


async def async_setup_entry(hass, entry):
    parent = hass.config_entries.async_get_entry(entry.data["matter_entry_id"])
    if parent is None or parent.state is not ConfigEntryState.LOADED:
        raise ConfigEntryNotReady("The selected Matter integration is not loaded")
    try:
        client = matter_client(parent)
        if entry.data["node_id"] not in {node.node_id for node in client.get_nodes()}:
            raise ConfigEntryNotReady("The selected remote is no longer present in Matter")
        runtime = Runtime(hass, entry, parent)
    except (AttributeError, RuntimeError, ValueError) as err:
        raise ConfigEntryNotReady(f"Cannot start BILRESA Dial: {err}") from err
    runtime.bind()
    if runtime.unsubscribe is None:
        runtime.close()
        raise ConfigEntryNotReady("Could not subscribe to the selected remote")
    entry.runtime_data = runtime
    entry.async_on_unload(parent.async_on_state_change(runtime.bind))
    entry.async_on_unload(hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED, runtime.entity_renamed))
    entry.async_on_unload(runtime.close)
    return True


async def async_unload_entry(hass, entry):
    entry.runtime_data.close()
    return True
