"""Diagnostics for the Artisan Roaster Scope integration."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .client import ArtisanConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ArtisanConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    client = entry.runtime_data.client
    return {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "connection": {
            "connected": client.connected,
            "host": client.host,
            "port": client.port,
            "failed_attempts": client.failed_attempts,
            "messages_received": client.messages_received,
        },
        "state": asdict(client.state),
        # Bridge messages never carry the token; redact anyway as defense in depth.
        "messages": async_redact_data(list(client.messages), TO_REDACT),
    }
