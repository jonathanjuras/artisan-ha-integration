"""The Artisan Roaster Scope integration."""

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .client import ArtisanBridgeClient, ArtisanConfigEntry, ArtisanRuntimeData
from .const import CONF_HOSTNAME, DOMAIN, MANUFACTURER, MODEL, device_name

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT]


async def async_setup_entry(hass: HomeAssistant, entry: ArtisanConfigEntry) -> bool:
    """Set up a bridge from a config entry.

    Setup never waits for the bridge: entities start unavailable and the client
    keeps reconnecting in the background.
    """
    bridge_id = entry.unique_id or entry.entry_id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, bridge_id)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=device_name(entry.data.get(CONF_HOSTNAME) or entry.data[CONF_HOST]),
    )
    client = ArtisanBridgeClient(hass, entry, device.id)
    entry.runtime_data = ArtisanRuntimeData(client)

    # Forward first so entity listeners exist before the first message arrives.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, client.async_run(), f"{DOMAIN} bridge client {bridge_id}"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ArtisanConfigEntry) -> bool:
    """Unload a config entry; the client task is canceled with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
