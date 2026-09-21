"""Small diagnostics with no tokens, network addresses or device identifiers."""

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    outputs = hass.data[DOMAIN]
    return {
        "group": entry.data["group"],
        "light_count": len(entry.options.get("lights", [])),
        "settings": {key: value for key, value in entry.options.items() if key != "lights"},
        "matter_listener_attached": runtime.unsubscribe is not None,
        "scroll_events": runtime.scroll_events,
        "press_events": runtime.press_events,
        "shared_output_totals": {
            "completed": outputs.completed,
            "failed": outputs.failed,
            "skipped": outputs.skipped,
            "superseded": outputs.superseded,
            "expired": outputs.expired,
            "active_lights": len(outputs.tasks),
            "pending_lights": len(outputs.pending),
        },
    }
