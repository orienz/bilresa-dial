"""Remote/group selection and editable light behavior."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DEFAULTS, DOMAIN, validate_options
from .discovery import list_remotes


def settings_schema(defaults):
    values = {**DEFAULTS, **defaults}
    fields = {
        vol.Required("lights", default=values["lights"]): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="light", multiple=True)
        )
    }
    for key in ("reverse_direction", "lit_only", "brighten_from_off"):
        fields[vol.Required(key, default=values[key])] = selector.BooleanSelector()
    for key, minimum, maximum, step, unit in [
        ("step_percent", 0.1, 25, 0.1, "%"),
        ("minimum_percent", 0.1, 100, 0.1, "%"),
        ("maximum_percent", 0.1, 100, 0.1, "%"),
        ("brightness_transition", 0, 5, 0.1, "s"),
        ("temperature_transition", 0, 10, 0.1, "s"),
    ]:
        fields[vol.Required(key, default=values[key])] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX
            )
        )
    for key, choices in [("single_press", ["toggle", "none"]), ("double_press", ["temperature_cycle", "none"])]:
        fields[vol.Required(key, default=values[key])] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=choices, translation_key=key, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
    fields[vol.Required("temperature_steps", default=values["temperature_steps"])] = selector.TextSelector()
    return vol.Schema(fields)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def async_step_user(self, user_input=None):
        remotes = list_remotes(self.hass)
        if not remotes:
            return self.async_abort(reason="no_remotes")
        errors = {}
        if user_input is not None:
            remote = remotes.get(user_input.get("remote"))
            if remote is None:
                errors["base"] = "remote_missing"
            else:
                try:
                    group = int(user_input.get("group", 0))
                except ValueError, TypeError:
                    group = 0
                if group not in remote.groups:
                    errors["base"] = "group_missing"
                else:
                    await self.async_set_unique_id(f"{remote.key}:{group}")
                    self._abort_if_unique_id_configured()
                    self._remote, self._group = remote, group
                    return await self.async_step_settings()
        schema = vol.Schema(
            {
                vol.Required("remote"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": remote.key, "label": remote.label} for remote in remotes.values()],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required("group", default="1"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["1", "2", "3"], mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                options = validate_options(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                remote = self._remote
                return self.async_create_entry(
                    title=f"{remote.label} · group {self._group}",
                    data={
                        "matter_entry_id": remote.parent_id,
                        "node_id": remote.node_id,
                        "device_id": remote.device_id,
                        "group": self._group,
                    },
                    options=options,
                )
        return self.async_show_form(step_id="settings", data_schema=settings_schema(user_input or {}), errors=errors)


class OptionsFlow(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                options = validate_options(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="init", data_schema=settings_schema(user_input or self.config_entry.options), errors=errors
        )
