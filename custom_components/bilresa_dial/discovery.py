"""Locate already paired rotary remotes without depending on entity names."""

from dataclasses import dataclass

from homeassistant.helpers import device_registry as dr


@dataclass(frozen=True)
class Remote:
    key: str
    parent_id: str
    node_id: int
    label: str
    device_id: str | None
    groups: tuple[int, ...]


def matter_client(parent):
    """Keep the one version-sensitive HA access point in a single function."""
    client = parent.runtime_data.adapter.matter_client
    if not callable(getattr(client, "get_nodes", None)) or not callable(getattr(client, "subscribe_events", None)):
        raise RuntimeError("Unsupported Home Assistant Matter client API")
    return client


def list_remotes(hass):
    registry = dr.async_get(hass)
    remotes = {}
    for parent in hass.config_entries.async_loaded_entries("matter"):
        try:
            client = matter_client(parent)
            nodes = client.get_nodes()
            fabric = client.server_info.compressed_fabric_id
        except AttributeError, RuntimeError:
            continue
        for node in nodes:
            product = str(getattr(node.device_info, "productName", ""))
            groups = tuple(
                group
                for group in (1, 2, 3)
                if all(endpoint in node.endpoints for endpoint in range(group * 3 - 2, group * 3 + 1))
            )
            # The two-button BILRESA does not expose the three rotary triples.
            if "bilresa" not in product.casefold() or not (
                "scroll" in product.casefold() or "rotary" in product.casefold() or len(groups) == 3
            ):
                continue
            if not groups:
                continue
            identifier = ("matter", f"deviceid_{fabric:016X}-{node.node_id:016X}-MatterNodeDevice")
            device = registry.async_get_device_by_identifier(identifier, parent.entry_id)
            label = (device.name_by_user or device.name) if device else None
            label = label or getattr(node.device_info, "nodeLabel", None) or product
            key = f"{parent.entry_id}:{node.node_id}"
            remotes[key] = Remote(
                key,
                parent.entry_id,
                node.node_id,
                f"{label} · node {node.node_id}",
                device.id if device else None,
                groups,
            )
    return remotes
