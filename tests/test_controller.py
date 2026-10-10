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


@pytest.mark.parametrize("up,brightness,kelvin", [(False, 3, 2202), (True, 255, 6535)])
async def test_scroll_temperature_reaches_bulb_limits(up, brightness, kelvin):
    rig = Rig([light(minimum_kelvin=2202, maximum_kelvin=6535, transition_supported=True)])
    control = rig.controller(temperature_follows_brightness=True, brightness_transition=0.3)
    control.scroll(255, up)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"brightness": brightness, "color_temp_kelvin": kelvin, "transition": 0.3}


async def test_scroll_temperature_curve_and_mixed_capabilities():
    rig = Rig(
        [
            light(brightness=121, minimum_kelvin=2000, maximum_kelvin=6000),
            light("light.other", brightness=121, minimum_kelvin=2500, maximum_kelvin=6500),
            light("light.plain", brightness=121, modes=("brightness",)),
            light("light.off", state="off"),
        ]
    )
    control = rig.controller(temperature_follows_brightness=True)
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert {c.entity_id: c.data for c in rig.sent} == {
        "light.desk": {"brightness": 129, "color_temp_kelvin": 4000},
        "light.other": {"brightness": 129, "color_temp_kelvin": 4500},
        "light.plain": {"brightness": 129},
    }


async def test_scroll_temperature_pending_replacement_keeps_both_values():
    rig = Rig([light(state="off", brightness=None, minimum_kelvin=2000, maximum_kelvin=6000)])
    rig.blocked_entity = "light.desk"
    control = rig.controller(temperature_follows_brightness=True)
    control.scroll(1, True)
    await asyncio.sleep(0)
    control.scroll(10, True)
    control.scroll(255, True)
    rig.release.set()
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 2
    assert rig.sent[-1].data == {"brightness": 255, "color_temp_kelvin": 6000}


async def test_scroll_at_limit_restores_temperature_after_double_press_once():
    rig = Rig([light(brightness=255, minimum_kelvin=2000, maximum_kelvin=6000)])
    control = rig.controller(temperature_follows_brightness=True)
    control.press("multi_press_2")
    await rig.outputs.wait_idle()
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"brightness": 255, "color_temp_kelvin": 6000}
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 2


def test_scroll_temperature_is_opt_in_and_requires_boolean():
    assert not settings().temperature_follows_brightness
    with pytest.raises(ValueError, match="invalid_settings"):
        settings(temperature_follows_brightness="true")


async def test_palette_has_eight_colors_white_and_wraps_for_mixed_group():
    from custom_components.bilresa_dial.colors import PALETTE

    rig = Rig(
        [
            light(modes=("color_temp", "hs"), color_mode="color_temp"),
            light("light.white", minimum_kelvin=2500, maximum_kelvin=4000),
            light("light.off", state="off", modes=("hs",)),
        ]
    )
    control = rig.controller(double_press="color_cycle")
    for index in range(10):
        control.press("multi_press_2")
        await rig.outputs.wait_idle()
        _, hue, kelvin = PALETTE[index % 9]
        color, white = rig.sent[-2:]
        assert color.entity_id == "light.desk" and white.entity_id == "light.white"
        assert color.data == ({"color_temp_kelvin": 3200} if hue is None else {"hs_color": [hue, 100]})
        assert white.data == {"color_temp_kelvin": max(2500, min(4000, kelvin))}
    assert len(PALETTE) == 9 and sum(item[1] is None for item in PALETTE) == 1


async def test_white_spectrum_xy_does_not_enable_color_cycle():
    rig = Rig([light(modes=("color_temp", "xy"), white_only=True, color_mode="color_temp")])
    control = rig.controller(double_press="color_cycle")
    control.press("multi_press_2")
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"color_temp_kelvin": 3200}


async def test_scroll_preserves_current_color_with_temperature_follow_enabled():
    rig = Rig(
        [
            light(brightness=128, modes=("color_temp", "hs"), color_mode="hs", hs=(180, 100)),
            light("light.white", brightness=128),
        ]
    )
    control = rig.controller(temperature_follows_brightness=True, brightness_max_kelvin=3500)
    control.scroll(4, False)
    await rig.outputs.wait_idle()
    first = rig.sent[-2].data
    assert first["brightness"] == 96 and 170 < first["hs_color"][0] < 180
    assert first["hs_color"][1] == 100 and "color_temp_kelvin" not in first
    assert rig.sent[-1].data["color_temp_kelvin"] > 3000
    control.scroll(4, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-2].data["hs_color"] == pytest.approx([180, 100], abs=0.002)


async def test_rapid_palette_then_scroll_carries_pending_color_and_white():
    rig = Rig([light(brightness=128, modes=("color_temp", "hs"), color_mode="color_temp")])
    control = rig.controller(
        double_press="color_cycle", temperature_follows_brightness=True, brightness_max_kelvin=3500
    )
    # The bulb still reports white when the scroll replaces a pending red command.
    control.press("multi_press_2")
    control.scroll(1, False)
    await rig.outputs.wait_idle()
    assert len(rig.sent) == 1 and "hs_color" in rig.sent[-1].data
    for _ in range(3):
        control.press("multi_press_2")
    control.scroll(255, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"brightness": 255, "color_temp_kelvin": 3500}


async def test_rgb_only_white_choice_and_disabled_warmth_preserve_color():
    rig = Rig([light(modes=("xy",), color_mode="xy", hs=(270, 100))])
    control = rig.controller(double_press="color_cycle")
    control.scroll(1, False)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data["hs_color"] == [270, 100]
    for _ in range(4):
        control.press("multi_press_2")
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"color_temp_kelvin": 3200}


async def test_color_bulb_off_still_enables_palette_for_lit_white_bulbs():
    rig = Rig([light(), light("light.rgb", state="off", modes=("hs",))])
    control = rig.controller(double_press="color_cycle")
    control.press("multi_press_2")
    await rig.outputs.wait_idle()
    assert [(c.entity_id, c.data) for c in rig.sent] == [("light.desk", {"color_temp_kelvin": 2202})]


async def test_explicit_white_on_xy_only_light_keeps_white_curve_after_state_update():
    from dataclasses import replace

    rig = Rig([light(brightness=128, modes=("xy",), color_mode="xy", hs=(270, 100), minimum_kelvin=2202)])
    control = rig.controller(
        double_press="color_cycle", temperature_follows_brightness=True, brightness_max_kelvin=3500
    )
    for _ in range(4):
        control.press("multi_press_2")
    await rig.outputs.wait_idle()
    rig.now += 6
    rig.lights = [replace(rig.lights[0], hs=(28, 50))]
    control.scroll(255, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"brightness": 255, "color_temp_kelvin": 3500}
    rig.now += 6
    # A manually selected saturated color takes precedence over remembered white.
    rig.lights = [replace(rig.lights[0], hs=(225, 100))]
    control.scroll(1, False)
    await rig.outputs.wait_idle()
    assert "hs_color" in rig.sent[-1].data and "color_temp_kelvin" not in rig.sent[-1].data


async def test_dark_color_bulb_keeps_its_stored_color_until_report_arrives():
    rig = Rig([light(state="off", brightness=None, modes=("color_temp", "hs"), color_mode=None, hs=None)])
    control = rig.controller(temperature_follows_brightness=True)
    control.scroll(1, True)
    await rig.outputs.wait_idle()
    assert rig.sent[-1].data == {"brightness": 8}
