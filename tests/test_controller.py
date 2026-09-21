import asyncio

import pytest

from custom_components.bilresa_dial.const import Settings, validate_options
from custom_components.bilresa_dial.controller import Controller, Light, OutputManager


def light(entity="light.desk", state="on", brightness=100, modes=("color_temp",), kelvin=2700, **extra):
    return Light(entity, state, brightness, frozenset(modes), kelvin, **extra)


def settings(**values):
    return Settings.from_options({"lights": ["light.desk"], **values})


class Rig:
    def __init__(self, lights=None):
        self.now, self.lights, self.sent = 100.0, lights or [light()], []
        self.blocked_entity = None
        self.release = asyncio.Event()
        self.outputs = OutputManager(self.send, clock=lambda: self.now)

    async def send(self, command):
        self.sent.append(command)
        if command.entity_id == self.blocked_entity and not self.release.is_set():
            await self.release.wait()
        return True

    def controller(self, **options):
        return Controller(settings(**options), lambda: self.lights, self.outputs, lambda: self.now)


async def test_saved_brightness_and_expiry():
    rig = Rig()
    control = rig.controller()
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    control.scroll(2, True)
    await rig.outputs.wait_idle()
    assert [call.data["brightness"] for call in rig.sent] == [108, 124]
    rig.now += 6
    control.scroll(1, False)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data["brightness"] == 92


async def test_lit_only_bounds_reverse_and_simple_lights():
    rig = Rig(
        [light(brightness=253), light("light.off", "off"), light("light.simple", modes=("onoff",), brightness=None)]
    )
    control = rig.controller(reverse_direction=True)
    control.scroll(1, False)
    await rig.outputs.wait_idle()
    assert [(c.entity_id, c.data["brightness"]) for c in rig.sent] == [("light.desk", 255)]
    control.scroll(10, False)
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1
    control.scroll(255, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data["brightness"] == 3


async def test_dark_start_and_pending_reversal():
    rig = Rig([light(state="off", brightness=None)])
    rig.blocked_entity = "light.desk"
    control = rig.controller()
    control.scroll(1, False)
    assert not rig.outputs.tasks
    control.scroll(2, True)
    await asyncio.sleep(0)
    control.scroll(1, False)
    rig.release.set()
    await rig.outputs.wait_idle()
    assert [c.data["brightness"] for c in rig.sent] == [16, 8]


async def test_disabled_dark_start_and_all_lights_mode():
    rig = Rig([light(state="off")])
    control = rig.controller(brighten_from_off=False)
    control.scroll(2, True)
    await rig.outputs.wait_idle()
    assert not rig.sent
    rig.lights = [light(), light("light.off", "off")]
    control = rig.controller(lit_only=False)
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert {c.entity_id for c in rig.sent} == {"light.desk", "light.off"}


async def test_slow_bulb_does_not_block_other_and_pending_is_bounded():
    rig = Rig([light("light.slow"), light("light.fast")])
    rig.blocked_entity = "light.slow"
    control = rig.controller()
    control.scroll(1, True)
    await asyncio.sleep(0)
    for _ in range(4):
        control.scroll(1, True)
    assert len(rig.outputs.pending) <= 2
    await asyncio.sleep(0)
    assert len([c for c in rig.sent if c.entity_id == "light.fast"]) == 2
    rig.release.set()
    await rig.outputs.wait_idle()
    assert [c.data["brightness"] for c in rig.sent if c.entity_id == "light.slow"] == [108, 140]


async def test_two_remotes_share_output_and_invalidate_old_cache():
    rig = Rig()
    rig.blocked_entity = "light.desk"
    first, second = rig.controller(), rig.controller()
    first.scroll(1, True)
    await asyncio.sleep(0)
    first.scroll(1, True)
    second.scroll(1, False)
    first.scroll(1, True)
    rig.release.set()
    await rig.outputs.wait_idle()
    assert [c.data["brightness"] for c in rig.sent] == [108, 108]


async def test_unload_keeps_inflight_and_discards_waiting():
    rig = Rig()
    rig.blocked_entity = "light.desk"
    control = rig.controller()
    control.scroll(1, True)
    await asyncio.sleep(0)
    control.scroll(1, True)
    control.close()
    rig.release.set()
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1


async def test_expired_queue_is_skipped():
    rig = Rig()
    rig.blocked_entity = "light.desk"
    control = rig.controller()
    control.scroll(1, True)
    await asyncio.sleep(0)
    control.scroll(1, True)
    rig.now += 6
    rig.release.set()
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1 and rig.outputs.expired == 1


async def test_failed_call_does_not_wedge_worker():
    sent = []

    async def send(command):
        sent.append(command)
        if len(sent) == 1:
            raise RuntimeError("test connection failure")
        return True

    output = OutputManager(send)
    control = Controller(settings(), lambda: [light()], output)
    control.scroll(1, True)
    await output.wait_idle()
    control.scroll(1, True)
    await output.wait_idle()
    assert output.failed == 1 and output.completed == 1


async def test_toggle_uses_pending_intent_with_onoff_lights():
    rig = Rig([light(modes=("onoff",), brightness=None)])
    control = rig.controller()
    control.press("multi_press_1")
    await rig.outputs.wait_idle()
    control.press("multi_press_1")
    await rig.outputs.wait_idle()
    assert [c.service for c in rig.sent] == ["turn_off", "turn_on"]


async def test_temperature_capability_range_fade_and_saved_target():
    rig = Rig(
        [light(transition_supported=True, minimum_kelvin=2500, maximum_kelvin=4000), light("light.rgb", modes=("rgb",))]
    )
    control = rig.controller(temperature_steps="2202,2700,4500")
    control.press("multi_press_2")
    await rig.outputs.wait_idle()
    control.press("multi_press_2")
    await rig.outputs.wait_idle()
    assert [c.data for c in rig.sent] == [
        {"color_temp_kelvin": 4000, "transition": 0.7},
        {"color_temp_kelvin": 2500, "transition": 0.7},
    ]
    assert all(c.entity_id == "light.desk" for c in rig.sent)


async def test_disabled_presses_and_unavailable_lights():
    rig = Rig([light(), light("light.missing", "unavailable")])
    control = rig.controller(single_press="none", double_press="none")
    control.press("multi_press_1")
    control.press("multi_press_2")
    assert not rig.sent
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1 and "transition" not in rig.sent[0].data


@pytest.mark.parametrize(
    "values,error",
    [
        ({"lights": []}, "lights_required"),
        ({"lights": ["switch.bad"]}, "lights_required"),
        ({"lights": ["light."]}, "lights_required"),
        ({"lights": ["light.bad name"]}, "lights_required"),
        ({"minimum_percent": 70, "maximum_percent": 30}, "brightness_range"),
        ({"temperature_steps": "2700,2200"}, "temperature_steps"),
        ({"step_percent": float("nan")}, "invalid_settings"),
        ({"reverse_direction": "true"}, "invalid_settings"),
    ],
)
def test_validation(values, error):
    with pytest.raises(ValueError, match=error):
        validate_options({"lights": ["light.desk"], **values})


async def test_new_action_drops_old_waiting_work_for_different_light_selection():
    rig = Rig([light("light.color"), light("light.slow", modes=("brightness",))])
    rig.blocked_entity = "light.slow"
    control = rig.controller()
    control.scroll(1, True)
    await asyncio.sleep(0)
    control.scroll(1, True)
    assert "light.slow" in rig.outputs.pending
    # Temperature only applies to the color bulb. The earlier queued dimming
    # command must not remain and surprise the user after the newer action.
    control.press("multi_press_2")
    assert "light.slow" not in rig.outputs.pending
    rig.release.set()
    await rig.outputs.wait_idle()
    assert [c.data for c in rig.sent if c.entity_id == "light.slow"] == [{"brightness": 108}]
    assert rig.sent[-1].data["color_temp_kelvin"] == 3200
