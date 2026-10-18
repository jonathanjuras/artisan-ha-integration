"""Base entity for the Artisan Roaster Scope integration."""

from datetime import datetime
from typing import override

from homeassistant.core import CALLBACK_TYPE, HassJob, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .client import ArtisanBridgeClient, ArtisanConfigEntry
from .const import DEFAULT_SAMPLE_INTERVAL, DOMAIN, OPT_SAMPLE_INTERVAL, signal_update
from .models import RoastState


class ArtisanEntity(Entity):
    """An entity fed by the bridge client through the dispatcher.

    With `throttled=True`, writes caused by `sample` messages are limited to one
    per `options.sample_interval_s`; the latest value is written when the
    interval ends, so only intermediate samples are skipped. Everything else
    (snapshot, events, availability) is written immediately.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: ArtisanConfigEntry, key: str, *, throttled: bool = False
    ) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._client: ArtisanBridgeClient = entry.runtime_data.client
        self._attr_unique_id = f"{self._client.bridge_id}_{key}"
        # Identifiers only: the device is created (and named) in async_setup_entry.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._client.bridge_id)}
        )
        self._min_interval = (
            float(entry.options.get(OPT_SAMPLE_INTERVAL, DEFAULT_SAMPLE_INTERVAL))
            if throttled
            else 0.0
        )
        self._last_write = 0.0
        self._last_available: bool | None = None
        self._cancel_pending: CALLBACK_TYPE | None = None
        self._pending_job = HassJob(self._async_pending_write, cancel_on_shutdown=True)

    @property
    def roast(self) -> RoastState:
        """Return the client's current state."""
        return self._client.state

    @property
    @override
    def available(self) -> bool:
        """Return True while the bridge connection is up."""
        return self._client.connected

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to client updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )
        self.async_on_remove(self._cancel_pending_write)

    @callback
    def _handle_update(self, msg_type: str | None) -> None:
        if (
            not self._min_interval
            or msg_type != "sample"
            or self.available != self._last_available
        ):
            self._write_now()
            return
        if self._cancel_pending is not None:
            return  # a write at the end of the interval is already scheduled
        remaining = self._last_write + self._min_interval - self.hass.loop.time()
        if remaining <= 0:
            self._write_now()
        else:
            self._cancel_pending = async_call_later(
                self.hass, remaining, self._pending_job
            )

    @callback
    def _write_now(self) -> None:
        self._cancel_pending_write()
        self._last_write = self.hass.loop.time()
        self._last_available = self.available
        self.async_write_ha_state()

    @callback
    def _async_pending_write(self, _now: datetime) -> None:
        self._cancel_pending = None
        self._write_now()

    @callback
    def _cancel_pending_write(self) -> None:
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None
