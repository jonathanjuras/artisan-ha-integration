"""Binary sensors for the Artisan Roaster Scope integration."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ArtisanBridgeClient, ArtisanConfigEntry
from .const import ROASTING_PHASES
from .entity import ArtisanEntity

PARALLEL_UPDATES = 0


def _roasting(client: ArtisanBridgeClient) -> bool | None:
    phase = client.state.phase
    return None if phase is None else phase in ROASTING_PHASES


@dataclass(frozen=True, kw_only=True)
class ArtisanBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes an Artisan binary sensor."""

    is_on_fn: Callable[[ArtisanBridgeClient], bool | None]
    # The connectivity sensor must report "off", not "unavailable", when down.
    always_available: bool = False


BINARY_SENSORS: tuple[ArtisanBinarySensorEntityDescription, ...] = (
    ArtisanBinarySensorEntityDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_available=True,
        is_on_fn=lambda client: client.connected,
    ),
    ArtisanBinarySensorEntityDescription(
        key="artisan_online",
        translation_key="artisan_online",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=lambda client: client.state.artisan_online,
    ),
    ArtisanBinarySensorEntityDescription(
        key="roasting",
        translation_key="roasting",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=_roasting,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArtisanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities(
        ArtisanBinarySensor(entry, description) for description in BINARY_SENSORS
    )


class ArtisanBinarySensor(ArtisanEntity, BinarySensorEntity):
    """A binary sensor described by an ArtisanBinarySensorEntityDescription."""

    entity_description: ArtisanBinarySensorEntityDescription

    def __init__(
        self,
        entry: ArtisanConfigEntry,
        description: ArtisanBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        """Return availability; the connectivity sensor is always available."""
        return self.entity_description.always_available or super().available

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        return self.entity_description.is_on_fn(self._client)
