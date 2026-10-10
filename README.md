# BILRESA Dial

Control Home Assistant lights with an IKEA BILRESA rotary remote. Choose lights for each of its three groups, then set scrolling and press behavior in HA.

Requires **Home Assistant 2026.9.3+**, its **Matter integration**, and a working Thread network. The remote must be paired with HA. This is an early release; changes to HA's internal Matter API may require an update.

## Install

**HACS:** Open **HACS → ⋮ → Custom repositories**, add `https://github.com/orienz/bilresa-dial`, and choose **Integration**. Download BILRESA Dial and restart HA.

**Manual:** Download the ZIP from [Releases](https://github.com/orienz/bilresa-dial/releases), copy its `custom_components/bilresa_dial` folder into your HA configuration directory, and restart HA.

Disable any older automation that controls the same remote group. Then:

1. Open **Settings → Devices & services → Add integration → BILRESA Dial**.
2. Choose the remote, group, lights, and settings.

Add another entry for each group or remote. The gear icon beside an entry opens its settings. Lights can come from any HA integration; light groups are expanded into their members.

For updates, install the new version through HACS and restart HA. Manual installations need one download through HACS before HACS can track them. Keep the existing integration entry to preserve its settings.

## Controls

| Control | Default | Settings |
|---|---|---|
| Wheel | Change brightness by 3% per step | Direction, step size, limits, fade, white temperature |
| Single press | Toggle selected lights | Enable or disable |
| Double press | Cycle white temperature | White presets or eight colors and white, fade |

Scrolling normally adjusts only lit bulbs. Turning up from darkness starts them gently; turning down leaves them off. Both choices are configurable. Brightness stops at 1% and 100%, with no fade. Selected bulbs receive the same level, so relative brightness differences are not preserved.

Enable **Adjust color warmth with brightness** to make white light warmer when dimmed and cooler when brightened, up to the chosen Kelvin limit. Colored lights keep their saturation and shift hue gently within the same color family. This hue shift is a visual warmth effect, not a Kelvin setting for saturated color. The option is off by default.

Choose **Cycle eight colors and white** for red, orange, yellow, white (3200 K), green, cyan, blue, violet, and magenta. White uses native temperature control where available; RGB/XY-only bulbs emulate it. White-only bulbs use a complementary white within their supported range. IKEA WS models that advertise XY are still treated as white-only. Only lit lights change; the presence of any configured color bulb enables the palette. Without one, the dial cycles white presets instead. Palette order starts again after an integration reload.

White presets default to **2202, 2400, 2700, 3200, and 4500 K**, with a **0.7-second fade**. The original white-only cycle remains available as a separate double-press option. Fades require transition support. On/off-only lights support toggling.

A double acts on its second click. A third click adds nothing; holds have no assigned action. The group button stays local to the remote.

## How scrolling works

The decoder reads raw Matter events, sends one step as movement starts, then subtracts steps already counted from later totals. Event numbers remove duplicates. Each direction has its own count; delivery pauses do not reset it.

The controller remembers the requested brightness for five seconds while bulb reports catch up. Each bulb has one command running and one replaceable waiting request. New input replaces waiting work, so a slow bulb does not hold up the others. Requests older than five seconds are dropped; commands already sent finish normally.

Network delays still matter. Missing clicks or turn boundaries cannot always be recovered. Other controls can briefly compete with the saved target. Native group broadcasts and perfectly synchronized bulbs are not provided.

## Testing and troubleshooting

Tested with HA 2026.9.3 and Matter Python client 1.4.0. Physical testing covered one rotary remote, group 1, and four Matter lights. Other groups and multiple remotes have automated coverage. Tests also cover recorded turns, duplicate events, light capabilities, slow calls, reloads, and configuration.

Download diagnostics from the entry menu for input and output counts. Integration diagnostics omit device IDs, light names, and credentials. Report problems in [Issues](https://github.com/orienz/bilresa-dial/issues).

## Development

Use Python 3.14:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python tools/build_release.py
```

The build writes an installation ZIP and checksum to `dist/`. To release, update the manifest version, run the checks, and publish a matching GitHub release with those files. HACS reads the integration from the release tag.
