import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from test_decoder import event

from custom_components.bilresa_dial import async_setup_entry, async_unload_entry, read_lights
from custom_components.bilresa_dial.config_flow import ConfigFlow, OptionsFlow
from custom_components.bilresa_dial.const import DOMAIN, validate_options
from custom_components.bilresa_dial.discovery import list_remotes


class Client:
    def __init__(self, nodes):
        self.nodes = nodes
        self.server_info = SimpleNamespace(compressed_fabric_id=123)
        self.callbacks = []

    def get_nodes(self):
        return self.nodes

    def subscribe_events(self, callback, **filters):
        item = (callback, filters)
        self.callbacks.append(item)
        return lambda: self.callbacks.remove(item)


def node(number, product="BILRESA scroll wheel", endpoints=range(10)):
    return SimpleNamespace(
        node_id=number,
        device_info=SimpleNamespace(productName=product, nodeLabel="Desk dial"),
        endpoints={endpoint: object() for endpoint in endpoints},
    )


def entry(domain, data=None, options=None, unique_id=None, state=ConfigEntryState.NOT_LOADED):
    return ConfigEntry(
        domain=domain,
        data=data or {},
        options=options or {},
        title="Test",
        unique_id=unique_id,
        version=1,
        minor_version=1,
        source="user",
        discovery_keys={},
        subentries_data=[],
        state=state,
    )


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    instance.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(instance)
    await dr.async_load(instance, load_empty=True)
    yield instance
    for task in list(instance._background_tasks):
        task.cancel()
    await asyncio.gather(*list(instance._background_tasks), return_exceptions=True)
    await instance.async_stop(force=True)


def add_parent(hass, nodes=None):
    parent = entry("matter", state=ConfigEntryState.LOADED)
    parent.runtime_data = SimpleNamespace(adapter=SimpleNamespace(matter_client=Client(nodes or [node(73)])))
    hass.config_entries._entries[parent.entry_id] = parent
    return parent


def configure_flow(hass):
    flow = ConfigFlow()
    flow.hass, flow.handler, flow.flow_id = hass, DOMAIN, "test-flow"
    flow.context = {"source": "user"}
    return flow


async def test_discovery_does_not_use_entity_names_and_filters_other_devices(hass):
    first = add_parent(hass, [node(73), node(74, "BILRESA dual button", range(3)), node(75, "Other dial")])
    add_parent(hass, [node(901)])
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=first.entry_id,
        identifiers={("matter", "deviceid_000000000000007B-0000000000000049-MatterNodeDevice")},
        name="Custom name",
    )
    remotes = list_remotes(hass)
    assert {remote.node_id for remote in remotes.values()} == {73, 901}
    assert remotes[f"{first.entry_id}:73"].device_id == device.id
    assert "Custom name" in remotes[f"{first.entry_id}:73"].label


async def test_config_flow_and_duplicate_group(hass):
    parent = add_parent(hass)
    flow = configure_flow(hass)
    form = await flow.async_step_user()
    assert form["type"] == "form"
    selected = {"remote": f"{parent.entry_id}:73", "group": "3"}
    settings = await flow.async_step_user(form["data_schema"](selected))
    assert settings["step_id"] == "settings"
    bad = await flow.async_step_settings({"lights": []})
    assert bad["errors"]["base"] == "lights_required"
    result = await flow.async_step_settings(settings["data_schema"]({"lights": ["light.test"]}))
    assert result["type"] == "create_entry" and result["data"]["node_id"] == 73 and result["data"]["group"] == 3
    configured = entry(DOMAIN, result["data"], result["options"], f"{parent.entry_id}:73:3")
    hass.config_entries._entries[configured.entry_id] = configured
    from homeassistant.data_entry_flow import AbortFlow

    with pytest.raises(AbortFlow):
        await configure_flow(hass).async_step_user(selected)
    assert (await configure_flow(hass).async_step_user({**selected, "group": "2"}))["step_id"] == "settings"


async def test_no_remote_and_options_change(hass):
    assert (await configure_flow(hass).async_step_user())["reason"] == "no_remotes"
    configured = entry(DOMAIN, options=validate_options({"lights": ["light.old"]}))
    hass.config_entries._entries[configured.entry_id] = configured
    flow = OptionsFlow()
    flow.hass, flow.handler, flow.flow_id = hass, configured.entry_id, "options"
    form = await flow.async_step_init()
    values = form["data_schema"]({"lights": ["light.new"], "reverse_direction": True})
    result = await flow.async_step_init(values)
    assert result["data"]["lights"] == ["light.new"] and result["data"]["reverse_direction"] is True


async def test_group_expansion_deduplication_and_capabilities(hass):
    hass.states.async_set("light.a", "on", {"brightness": 80, "supported_color_modes": ["brightness"]})
    hass.states.async_set("light.b", "off", {"supported_color_modes": ["color_temp"], "supported_features": 32})
    hass.states.async_set("light.group", "on", {"entity_id": ["light.a", "light.b", "light.group"]})
    snapshots = read_lights(hass, ["light.group", "light.a"])
    assert [light.entity_id for light in snapshots] == ["light.a", "light.b"]
    assert snapshots[1].transition_supported


async def test_runtime_real_ha_light_service_and_reload(hass):
    parent = add_parent(hass)
    configured = entry(
        DOMAIN,
        {"matter_entry_id": parent.entry_id, "node_id": 73, "group": 2},
        validate_options({"lights": ["light.a"], "reverse_direction": True}),
    )
    hass.config_entries._entries[configured.entry_id] = configured
    hass.states.async_set(
        "light.a", "on", {"brightness": 100, "supported_color_modes": ["brightness"], "supported_features": 32}
    )
    calls = []

    async def light_service(call):
        calls.append(dict(call.data))

    hass.services.async_register("light", "turn_on", light_service)
    assert await async_setup_entry(hass, configured)
    runtime = configured.runtime_data
    first_client = parent.runtime_data.adapter.matter_client
    assert len(first_client.callbacks) == 1
    runtime.bind()
    assert len(first_client.callbacks) == 1
    runtime.receive(None, event(1, 1, endpoint=4))
    await runtime.controller.outputs.wait_idle()
    assert calls == [{"entity_id": "light.a", "brightness": 92, "transition": 0.0}]
    second_client = Client([node(73)])
    parent.runtime_data.adapter.matter_client = second_client
    runtime.bind()
    assert not first_client.callbacks and len(second_client.callbacks) == 1
    assert await async_unload_entry(hass, configured)
    assert not second_client.callbacks
    runtime.close()


async def test_invalid_group_is_reported_in_form(hass):
    parent = add_parent(hass)
    result = await configure_flow(hass).async_step_user({"remote": f"{parent.entry_id}:73", "group": "invalid"})
    assert result["errors"] == {"base": "group_missing"}


async def test_setup_waits_for_selected_matter_node(hass):
    from homeassistant.exceptions import ConfigEntryNotReady

    parent = add_parent(hass)
    configured = entry(
        DOMAIN,
        {"matter_entry_id": parent.entry_id, "node_id": 999, "group": 1},
        validate_options({"lights": ["light.a"]}),
    )
    with pytest.raises(ConfigEntryNotReady, match="no longer present"):
        await async_setup_entry(hass, configured)
    assert not parent.runtime_data.adapter.matter_client.callbacks


async def test_entity_rename_updates_selection_and_requests_reload(hass, monkeypatch):
    from homeassistant.core import Event

    from custom_components.bilresa_dial import Runtime

    parent = add_parent(hass)
    configured = entry(
        DOMAIN,
        {"matter_entry_id": parent.entry_id, "node_id": 73, "group": 1},
        validate_options({"lights": ["light.old", "light.kept"]}),
    )
    runtime = Runtime(hass, configured, parent)
    updated, reloaded = [], []
    monkeypatch.setattr(hass.config_entries, "async_update_entry", lambda entry, **values: updated.append(values))
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reloaded.append)
    runtime.entity_renamed(Event("entity_registry_updated", {"action": "update", "entity_id": "light.ignored"}))
    assert not updated
    runtime.entity_renamed(
        Event("entity_registry_updated", {"action": "update", "old_entity_id": "light.old", "entity_id": "light.new"})
    )
    assert updated[0]["options"]["lights"] == ["light.new", "light.kept"]
    assert updated[0]["options"]["step_percent"] == configured.options["step_percent"]
    assert reloaded == [configured.entry_id]
    runtime.close()


async def test_diagnostics_omit_device_and_light_identifiers(hass):
    from custom_components.bilresa_dial.diagnostics import async_get_config_entry_diagnostics

    parent = add_parent(hass)
    configured = entry(
        DOMAIN,
        {"matter_entry_id": parent.entry_id, "node_id": 73, "group": 1},
        validate_options({"lights": ["light.private_name"]}),
    )
    assert await async_setup_entry(hass, configured)
    result = await async_get_config_entry_diagnostics(hass, configured)
    assert result["matter_listener_attached"] and result["light_count"] == 1
    assert "light.private_name" not in str(result)
    assert "node_id" not in result and "matter_entry_id" not in result
    assert result["shared_output_totals"]["pending_lights"] == 0
    await async_unload_entry(hass, configured)


async def test_light_becoming_unavailable_before_dispatch_is_not_counted_as_completed(hass):
    from custom_components.bilresa_dial import get_outputs

    hass.states.async_set("light.a", "on", {"brightness": 100})
    outputs = get_outputs(hass)
    owner = object()
    outputs.add_owner(owner)
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    outputs.submit(owner, "light.a", "turn_on", {"brightness": 108})
    hass.states.async_set("light.a", "unavailable")
    await outputs.wait_idle()
    assert not calls and outputs.completed == 0 and outputs.skipped == 1


async def test_subscription_failure_leaves_no_output_owner_and_can_retry(hass, monkeypatch):
    from homeassistant.exceptions import ConfigEntryNotReady

    parent = add_parent(hass)
    configured = entry(
        DOMAIN,
        {"matter_entry_id": parent.entry_id, "node_id": 73, "group": 1},
        validate_options({"lights": ["light.a"]}),
    )
    client = parent.runtime_data.adapter.matter_client
    subscribe = client.subscribe_events

    def broken_subscribe(*args, **kwargs):
        raise RuntimeError("test subscription failure")

    monkeypatch.setattr(client, "subscribe_events", broken_subscribe)
    with pytest.raises(ConfigEntryNotReady, match="Could not subscribe"):
        await async_setup_entry(hass, configured)
    assert not hass.data[DOMAIN].owners
    monkeypatch.setattr(client, "subscribe_events", subscribe)
    assert await async_setup_entry(hass, configured)
    assert len(client.callbacks) == 1
    await async_unload_entry(hass, configured)
