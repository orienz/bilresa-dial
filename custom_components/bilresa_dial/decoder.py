"""Convert raw Matter switch events into wheel steps and presses."""

from enum import IntEnum

SWITCH_CLUSTER = 59


class SwitchEvent(IntEnum):
    INITIAL_PRESS = 1
    LONG_PRESS = 2
    LONG_RELEASE = 4
    ONGOING = 5
    COMPLETE = 6


def first_endpoint(group: int) -> int:
    if type(group) is not int or group not in (1, 2, 3):
        raise ValueError("group must be 1, 2, or 3")
    return (group - 1) * 3 + 1


def press_count(event: dict, maximum: int) -> int | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    key = "currentNumberOfPressesCounted" if event["event_id"] == SwitchEvent.ONGOING else "totalNumberOfPressesCounted"
    count = data.get(key)
    return count if type(count) is int and 0 <= count <= maximum else None


class ScrollDecoder:
    """Keep a step count and last event number for each rotation direction."""

    def __init__(self, node_id: int, group: int = 1) -> None:
        self.node_id = node_id
        self.first_endpoint = first_endpoint(group)
        self.counted_steps = {self.first_endpoint: 0, self.first_endpoint + 1: 0}
        self.last_number: dict[int, int] = {}

    def feed(self, event: dict) -> int:
        endpoint = event.get("endpoint_id")
        if (
            event.get("node_id") != self.node_id
            or event.get("cluster_id") != SWITCH_CLUSTER
            or endpoint not in self.counted_steps
        ):
            return 0
        number = event.get("event_number")
        if type(number) is not int or number <= self.last_number.get(endpoint, -1):
            return 0
        self.last_number[endpoint] = number
        kind = event.get("event_id")
        if type(kind) is not int:
            return 0
        previous = self.counted_steps[endpoint]
        if kind == SwitchEvent.INITIAL_PRESS:
            if previous:
                return 0
            self.counted_steps[endpoint] = 1
            return 1
        if kind not in (SwitchEvent.ONGOING, SwitchEvent.COMPLETE):
            return 0
        count = press_count(event, 255)
        if count is None:
            return 0
        if count == 0:
            if kind == SwitchEvent.COMPLETE:
                self.counted_steps[endpoint] = 0
            return 0
        # A smaller total can reveal a missing end event. Arrival gaps cannot:
        # the network may deliver part of a continuous turn several seconds late.
        if count < previous:
            previous = 0
        self.counted_steps[endpoint] = count if kind == SwitchEvent.ONGOING else 0
        return count - previous


class PressDecoder:
    """Send a double on its second press and a single on completion."""

    def __init__(self, node_id: int, group: int = 1) -> None:
        self.node_id = node_id
        self.endpoint = first_endpoint(group) + 2
        self.last_number = -1
        self.double_sent = False
        self.initials = 0

    def feed(self, event: dict) -> str | None:
        if (
            event.get("node_id") != self.node_id
            or event.get("cluster_id") != SWITCH_CLUSTER
            or event.get("endpoint_id") != self.endpoint
        ):
            return None
        number = event.get("event_number")
        if type(number) is not int or number <= self.last_number:
            return None
        self.last_number = number
        kind = event.get("event_id")
        if type(kind) is not int:
            return None
        if kind == SwitchEvent.INITIAL_PRESS:
            self.initials += 1
            if self.initials == 2 and not self.double_sent:
                self.double_sent = True
                return "multi_press_2"
        elif kind == SwitchEvent.ONGOING:
            if press_count(event, 3) == 2 and not self.double_sent:
                self.double_sent = True
                return "multi_press_2"
        elif kind == SwitchEvent.COMPLETE:
            count = press_count(event, 3)
            if count is None:
                return None
            already_sent = self.double_sent
            self.double_sent = False
            self.initials = 0
            if count in (1, 2) and not already_sent:
                return f"multi_press_{count}"
        elif kind in (SwitchEvent.LONG_PRESS, SwitchEvent.LONG_RELEASE):
            self.double_sent = False
            self.initials = 0
        return None
