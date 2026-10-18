"""Roast event entity for the Artisan Roaster Scope integration."""

from typing import Any, override

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArtisanConfigEntry
from .const import EVENT_TYPES, signal_event
from .entity import ArtisanEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArtisanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the roast event entity."""
    async_add_entities([ArtisanRoastEvent(entry)])


class ArtisanRoastEvent(ArtisanEntity, EventEntity):
    """Fires for every roast event (CHARGE, FCs, DROP, …) and Artisan alert."""

    _attr_translation_key = "roast_event"

    def __init__(self, entry: ArtisanConfigEntry) -> None:
        """Initialize the event entity."""
        super().__init__(entry, "roast_event")
        self._attr_event_types = list(EVENT_TYPES)

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to roast events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry.entry_id), self._handle_event
            )
        )

    @override
    @callback
    def _handle_update(self, msg_type: str | None) -> None:
        # Only availability matters here; samples must not rewrite the state.
        if self.available != self._last_available:
            self._write_now()

    @callback
    def _handle_event(self, event_type: str, attributes: dict[str, Any]) -> None:
        self._trigger_event(event_type, attributes)
        self._write_now()
