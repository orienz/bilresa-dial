import pytest

from custom_components.bilresa_dial.decoder import PressDecoder, ScrollDecoder


def event(number, kind, count=None, node=73, endpoint=1):
    key = "currentNumberOfPressesCounted" if kind == 5 else "totalNumberOfPressesCounted"
    return dict(
        node_id=node,
        endpoint_id=endpoint,
        cluster_id=59,
        event_number=number,
        event_id=kind,
        data={} if count is None else {key: count},
    )


@pytest.mark.parametrize("node", [1, 73, 999])
@pytest.mark.parametrize("group", [1, 2, 3])
def test_rotary_groups_and_nodes(node, group):
    decoder = ScrollDecoder(node, group)
    result = []
    for number, kind, count in [(1, 1, None), (2, 1, None), (3, 5, 4), (4, 5, 10), (5, 6, 12)]:
        packet = event(number, kind, count, node, group * 3 - 2)
        result.append(decoder.feed(packet))
        assert decoder.feed(packet) == 0
    assert result == [1, 0, 3, 6, 2]
    assert decoder.feed(event(6, 1, node=node + 1, endpoint=group * 3 - 2)) == 0
    assert decoder.feed(event(7, 1, node=node, endpoint=group * 3)) == 0


def test_running_totals_missing_completion_and_out_of_order():
    decoder = ScrollDecoder(73)
    assert decoder.feed(event(1, 5, 15)) == 15
    assert decoder.feed(event(2, 1)) == 0
    assert decoder.feed(event(3, 5, 16)) == 1
    assert decoder.feed(event(4, 5, 2)) == 2
    assert decoder.feed(event(5, 6, 2)) == 0
    assert decoder.feed(event(4, 1)) == 0


def test_missing_start_and_independent_directions():
    decoder = ScrollDecoder(73)
    assert decoder.feed(event(1, 6, 18)) == 18
    assert decoder.feed(event(2, 1)) == 1
    assert decoder.feed(event(3, 1, endpoint=2)) == 1
    assert decoder.feed(event(4, 6, 3, endpoint=2)) == 2
    assert decoder.feed(event(5, 6, 5)) == 4


@pytest.mark.parametrize("bad", [-1, 256, "2", True, None])
def test_invalid_counters(bad):
    assert ScrollDecoder(73).feed(event(1, 5, bad)) == 0


@pytest.mark.parametrize("group", [1, 2, 3])
def test_double_and_contradictory_single(group):
    decoder = PressDecoder(73, group)
    assert decoder.feed(event(1, 1, endpoint=group * 3)) is None
    assert decoder.feed(event(2, 3, endpoint=group * 3)) is None
    assert decoder.feed(event(3, 1, endpoint=group * 3)) == "multi_press_2"
    assert decoder.feed(event(3, 1, endpoint=group * 3)) is None
    assert decoder.feed(event(4, 5, 2, endpoint=group * 3)) is None
    assert decoder.feed(event(5, 6, 1, endpoint=group * 3)) is None
    assert decoder.feed(event(6, 6, 1, endpoint=group * 3)) == "multi_press_1"


def test_double_fallback_and_hold():
    decoder = PressDecoder(73)
    assert decoder.feed(event(1, 6, 2, endpoint=3)) == "multi_press_2"
    assert decoder.feed(event(2, 1, endpoint=3)) is None
    assert decoder.feed(event(3, 2, endpoint=3)) is None
    assert decoder.feed(event(4, 4, endpoint=3)) is None
    assert decoder.feed(event(5, 1, endpoint=3)) is None


@pytest.mark.parametrize("bad", [True, 1.0, 2.0, -1, 4, "2", None])
def test_invalid_press_counters_do_not_toggle_or_break_next_press(bad):
    decoder = PressDecoder(73)
    assert decoder.feed(event(1, 6, bad, endpoint=3)) is None
    assert decoder.feed(event(2, 6, 1, endpoint=3)) == "multi_press_1"


@pytest.mark.parametrize("bad", [None, True, 4, "1"])
def test_unusable_completion_still_ends_the_press_gesture(bad):
    decoder = PressDecoder(73)
    assert decoder.feed(event(1, 1, endpoint=3)) is None
    assert decoder.feed(event(2, 1, endpoint=3)) == "multi_press_2"
    assert decoder.feed(event(3, 6, bad, endpoint=3)) is None
    assert decoder.feed(event(4, 1, endpoint=3)) is None
    assert decoder.feed(event(5, 6, 1, endpoint=3)) == "multi_press_1"


@pytest.mark.parametrize("decoder,endpoint", [(ScrollDecoder, 1), (PressDecoder, 3)])
def test_malformed_payload_is_ignored(decoder, endpoint):
    packet = event(1, 6, 2, endpoint=endpoint)
    packet["data"] = [2]
    assert not decoder(73).feed(packet)


def test_zero_completion_starts_a_new_turn():
    decoder = ScrollDecoder(73)
    assert decoder.feed(event(1, 1)) == 1
    assert decoder.feed(event(2, 6, 0)) == 0
    assert decoder.feed(event(3, 1)) == 1


@pytest.mark.parametrize("bad", [None, True, 256, "8"])
def test_unusable_completion_still_ends_the_turn(bad):
    decoder = ScrollDecoder(73)
    assert decoder.feed(event(1, 5, 7)) == 7
    assert decoder.feed(event(2, 6, bad)) == 0
    assert decoder.feed(event(3, 1)) == 1
    assert decoder.feed(event(4, 5, 8)) == 7


def test_recorded_turns():
    import json
    from pathlib import Path

    # Captures contain only event numbers, endpoints, kinds and counts.
    scenarios = json.loads((Path(__file__).parent / "fixtures/recorded_turns.json").read_text())
    for scenario in scenarios:
        decoder = ScrollDecoder(73)
        actual = [
            decoder.feed(event(number, kind, count, endpoint=endpoint))
            for number, endpoint, kind, count in scenario["events"]
        ]
        assert actual == scenario["steps"]
