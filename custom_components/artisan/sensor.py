"""Sensors for the Artisan Roaster Scope integration."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, override

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .client import ArtisanConfigEntry
from .const import (
    DEFAULT_ROR_PRECISION,
    OPT_ROR_PRECISION,
    PHASES,
    signal_update,
)
from .entity import ArtisanEntity
from .models import JsonDict, RoastState

# Entities only read client state; nothing to parallelize.
PARALLEL_UPDATES = 0

# Channel names the bridge accepts (spec 3.1); anything else is not turned into
# an entity.
_CHANNEL_RE = re.compile(r"[a-z0-9]{1,16}")
_CONTROL_KEY_PREFIX = "control_"


def _temperature_unit(unit: str) -> str:
    return UnitOfTemperature.FAHRENHEIT if unit == "F" else UnitOfTemperature.CELSIUS


def _ror_unit(unit: str) -> str:
    return f"°{'F' if unit == 'F' else 'C'}/min"


def _summary_value(key: str) -> Callable[[RoastState], StateType]:
    def value(state: RoastState) -> StateType:
        summary = state.last_summary or {}
        result = summary.get(key)
        return result if isinstance(result, int | float) else None

    return value


def _last_roast(state: RoastState) -> datetime | None:
    summary = state.last_final_summary
    if summary is None or not isinstance(ts := summary.get("ts"), int | float):
        return None
    return dt_util.utc_from_timestamp(ts / 1000)


def _last_roast_attributes(state: RoastState) -> JsonDict | None:
    if (summary := state.last_final_summary) is None:
        return None
    return {key: value for key, value in summary.items() if key != "ts"}


@dataclass(frozen=True, kw_only=True)
class ArtisanSensorEntityDescription(SensorEntityDescription):
    """Describes an Artisan sensor."""

    value_fn: Callable[[RoastState], StateType | datetime]
    # Maps the bridge's unit ("C"/"F") to the native unit, for unit-bearing values.
    unit_fn: Callable[[str], str] | None = None
    attributes_fn: Callable[[RoastState], JsonDict | None] | None = None
    # High-rate values follow the sample_interval_s throttle.
    throttled: bool = False
    # Rate-of-rise values honor the ror_precision option.
    ror: bool = False


SENSORS: tuple[ArtisanSensorEntityDescription, ...] = (
    ArtisanSensorEntityDescription(
        key="bt",
        translation_key="bt",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        unit_fn=_temperature_unit,
        throttled=True,
        value_fn=lambda s: s.bt,
    ),
    ArtisanSensorEntityDescription(
        key="et",
        translation_key="et",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        unit_fn=_temperature_unit,
        throttled=True,
        value_fn=lambda s: s.et,
    ),
    ArtisanSensorEntityDescription(
        key="bt_ror",
        translation_key="bt_ror",
        state_class=SensorStateClass.MEASUREMENT,
        unit_fn=_ror_unit,
        throttled=True,
        ror=True,
        value_fn=lambda s: s.bt_ror,
    ),
    ArtisanSensorEntityDescription(
        key="et_ror",
        translation_key="et_ror",
        state_class=SensorStateClass.MEASUREMENT,
        unit_fn=_ror_unit,
        throttled=True,
        ror=True,
        value_fn=lambda s: s.et_ror,
    ),
    ArtisanSensorEntityDescription(
        key="roast_time",
        translation_key="roast_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        throttled=True,
        value_fn=lambda s: s.roast_time,
    ),
    ArtisanSensorEntityDescription(
        key="dev_time",
        translation_key="dev_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        throttled=True,
        value_fn=lambda s: s.dev_time,
    ),
    ArtisanSensorEntityDescription(
        key="dtr",
        translation_key="dtr",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        throttled=True,
        value_fn=lambda s: s.dtr,
    ),
    ArtisanSensorEntityDescription(
        key="phase",
        translation_key="phase",
        device_class=SensorDeviceClass.ENUM,
        options=list(PHASES),
        value_fn=lambda s: s.phase,
    ),
    ArtisanSensorEntityDescription(
        key="charge_bt",
        translation_key="charge_bt",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=1,
        unit_fn=_temperature_unit,
        value_fn=_summary_value("charge_bt"),
    ),
    ArtisanSensorEntityDescription(
        key="fcs_bt",
        translation_key="fcs_bt",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=1,
        unit_fn=_temperature_unit,
        value_fn=_summary_value("fcs_bt"),
    ),
    ArtisanSensorEntityDescription(
        key="drop_bt",
        translation_key="drop_bt",
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=1,
        unit_fn=_temperature_unit,
        value_fn=_summary_value("drop_bt"),
    ),
    ArtisanSensorEntityDescription(
        key="last_roast",
        translation_key="last_roast",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_roast,
        attributes_fn=_last_roast_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArtisanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, including control sensors that appear at runtime."""
    client = entry.runtime_data.client
    async_add_entities(ArtisanSensor(entry, description) for description in SENSORS)

    known: set[str] = set()

    @callback
    def add_controls(channels: Iterable[str]) -> None:
        new = sorted(ch for ch in set(channels) - known if _CHANNEL_RE.fullmatch(ch))
        if new:
            known.update(new)
            async_add_entities(ArtisanControlSensor(entry, ch) for ch in new)

    # Recreate channels seen before a restart so they exist while the bridge is
    # offline and keep their entity IDs.
    prefix = f"{client.bridge_id}_{_CONTROL_KEY_PREFIX}"
    add_controls(
        reg.unique_id.removeprefix(prefix)
        for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if reg.domain == SENSOR_DOMAIN and reg.unique_id.startswith(prefix)
    )
    add_controls(client.state.controls)

    @callback
    def handle_update(_msg_type: str | None) -> None:
        add_controls(client.state.controls)

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_update(entry.entry_id), handle_update)
    )


class ArtisanSensor(ArtisanEntity, SensorEntity):
    """A sensor described by an ArtisanSensorEntityDescription."""

    entity_description: ArtisanSensorEntityDescription

    def __init__(
        self, entry: ArtisanConfigEntry, description: ArtisanSensorEntityDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(entry, description.key, throttled=description.throttled)
        self.entity_description = description
        self._ror_precision: int | None = None
        if description.ror:
            self._ror_precision = int(
                entry.options.get(OPT_ROR_PRECISION, DEFAULT_ROR_PRECISION)
            )
            self._attr_suggested_display_precision = self._ror_precision

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """Return the value from the roast state."""
        value = self.entity_description.value_fn(self.roast)
        # Round the state itself too, so the recorder stores what is shown.
        if self._ror_precision is not None and isinstance(value, float):
            return round(value, self._ror_precision)
        return value

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, following the bridge's configured unit."""
        if (unit_fn := self.entity_description.unit_fn) is not None:
            return unit_fn(self.roast.unit)
        return self.entity_description.native_unit_of_measurement

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes (the last roast summary)."""
        if (attributes_fn := self.entity_description.attributes_fn) is not None:
            return attributes_fn(self.roast)
        return None


class ArtisanControlSensor(ArtisanEntity, SensorEntity):
    """The last value of an Artisan slider/control channel."""

    _attr_translation_key = "control"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ArtisanConfigEntry, channel: str) -> None:
        """Initialize the control sensor."""
        super().__init__(entry, f"{_CONTROL_KEY_PREFIX}{channel}")
        self._channel = channel
        self._attr_translation_placeholders = {"channel": channel.capitalize()}

    @property
    @override
    def native_value(self) -> float | None:
        """Return the channel's last value."""
        return self.roast.controls.get(self._channel)
