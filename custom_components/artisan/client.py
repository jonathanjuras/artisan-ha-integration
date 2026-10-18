"""WebSocket client for the Artisan HA bridge (protocol v1, spec 3.2)."""

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import logging
import random
from typing import Literal

import aiohttp
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_EVENT,
    ATTR_ROAST_ID,
    ATTR_TEXT,
    ATTR_TITLE,
    CONF_HOSTNAME,
    CONF_UNIT,
    EVENT_ALERT,
    EVENT_ARTISAN,
    EVENT_ROAST_SUMMARY,
    PROTOCOL_VERSION,
    ROAST_EVENTS,
    WS_PATH,
    device_name,
    signal_event,
    signal_update,
)
from .models import JsonDict, RoastState, apply

_LOGGER = logging.getLogger(__name__)

# Timing knobs are module attributes read at call time so tests can patch them.
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.2  # fraction of the delay removed at random
HELLO_TIMEOUT = 10.0  # until the bridge's hello arrives (spec 5.5)
INACTIVITY_TIMEOUT = 45.0  # the bridge pings every 15 s (spec 3.2)
WS_HEARTBEAT = 30.0
MESSAGE_LOG_SIZE = 20

# decode_text=True (the default) makes text frames arrive as str.
type _WebSocket = aiohttp.ClientWebSocketResponse[Literal[True]]

_CLOSED_TYPES = frozenset(
    {
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    }
)


class ArtisanError(Exception):
    """Base error for the bridge client."""


class CannotConnect(ArtisanError):
    """The bridge is unreachable or broke the protocol."""


class InvalidAuth(ArtisanError):
    """The bridge rejected the token (HTTP 401)."""


def bridge_url(host: str, port: int) -> URL:
    """Return the bridge WebSocket URL; yarl brackets IPv6 hosts."""
    return URL.build(scheme="ws", host=host, port=port, path=WS_PATH)


def _decode(msg: aiohttp.WSMessage) -> JsonDict | None:
    """Return a protocol message from a text frame, or None if it is not one."""
    if msg.type is not aiohttp.WSMsgType.TEXT:
        return None
    try:
        data = json.loads(msg.data)
    except ValueError:
        _LOGGER.debug("Ignoring non-JSON frame from bridge")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        _LOGGER.debug("Ignoring frame without a message type")
        return None
    return data


async def _async_open(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    token: str,
    *,
    heartbeat: float | None,
) -> _WebSocket:
    """Open the authenticated socket; the bridge answers 401 before upgrading."""
    try:
        return await session.ws_connect(
            bridge_url(host, port),
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=heartbeat,
        )
    except aiohttp.WSServerHandshakeError as err:
        if err.status == 401:
            raise InvalidAuth from None
        raise CannotConnect(f"Bridge refused the upgrade: HTTP {err.status}") from None
    except (aiohttp.ClientError, OSError) as err:
        raise CannotConnect(str(err) or type(err).__name__) from err


async def _async_receive_hello(ws: _WebSocket) -> JsonDict:
    msg = await ws.receive()
    if msg.type in _CLOSED_TYPES:
        raise CannotConnect("Bridge closed the connection before hello")
    data = _decode(msg)
    if (
        data is None
        or data["type"] != "hello"
        or not isinstance(data.get("bridge_id"), str)
    ):
        raise CannotConnect("First frame from bridge was not a valid hello")
    return data


async def async_validate_connection(
    hass: HomeAssistant, host: str, port: int, token: str
) -> JsonDict:
    """Connect once, return the bridge's `hello` and disconnect.

    Raises InvalidAuth on 401 and CannotConnect on anything else, including no
    `hello` within HELLO_TIMEOUT.
    """
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(HELLO_TIMEOUT):
            # One-shot probe: no heartbeat, which would only leave a timer behind.
            ws = await _async_open(session, host, port, token, heartbeat=None)
            try:
                return await _async_receive_hello(ws)
            finally:
                await ws.close()
    except TimeoutError as err:
        raise CannotConnect("Timed out waiting for hello") from err


class ArtisanBridgeClient:
    """Keeps one WebSocket to the bridge open and mirrors its state.

    Runs as a config entry background task. Every message is reduced into
    `state`, then entities are notified through the dispatcher.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ArtisanConfigEntry, device_id: str
    ) -> None:
        """Initialize the client from the config entry."""
        self._hass = hass
        self._entry = entry
        self.device_id = device_id
        self.bridge_id: str = entry.unique_id or entry.entry_id
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data[CONF_PORT]
        self._token: str = entry.data[CONF_TOKEN]
        self.state = RoastState(
            bridge_id=self.bridge_id,
            hostname=entry.data.get(CONF_HOSTNAME),
            unit=entry.data.get(CONF_UNIT, "C"),
        )
        self.connected = False
        self.failed_attempts = 0
        self.messages_received = 0
        self.messages: deque[JsonDict] = deque(maxlen=MESSAGE_LOG_SIZE)
        self._signal_update = signal_update(entry.entry_id)
        self._signal_event = signal_event(entry.entry_id)
        self._outage_logged = False

    async def async_run(self) -> None:
        """Connect and reconnect forever, until canceled or the token is rejected."""
        while True:
            try:
                await self._async_connect_and_listen()
            except InvalidAuth:
                break
            except TimeoutError:
                self._log_outage("no data received within the timeout")
            except (CannotConnect, aiohttp.ClientError, OSError) as err:
                self._log_outage(str(err) or type(err).__name__)
            finally:
                # Entities go unavailable before the sleep; also runs on cancellation.
                self._set_connected(False)
            await asyncio.sleep(self._next_backoff())
        # Retrying with a rejected token is pointless; wait for the user.
        _LOGGER.error(
            "Artisan bridge at %s:%s rejected the token; reauthentication required",
            self.host,
            self.port,
        )
        self._entry.async_start_reauth(self._hass)

    def _next_backoff(self) -> float:
        """Return 1, 2, 4 … BACKOFF_MAX seconds, minus random jitter."""
        # Cap the exponent so a long outage cannot grow the number unbounded.
        delay = min(BACKOFF_MAX, BACKOFF_INITIAL * 2.0 ** min(self.failed_attempts, 16))
        self.failed_attempts += 1
        # Jitter only spreads reconnects of several clients; not security relevant.
        jitter: float = random.uniform(0, delay * BACKOFF_JITTER)  # noqa: S311
        return delay - jitter

    def _log_outage(self, reason: str) -> None:
        # Log the first failure of an outage only; retries are debug noise.
        if self._outage_logged:
            _LOGGER.debug("Artisan bridge still unavailable: %s", reason)
            return
        self._outage_logged = True
        _LOGGER.warning(
            "Artisan bridge at %s:%s unavailable (%s); retrying",
            self.host,
            self.port,
            reason,
        )

    async def _async_connect_and_listen(self) -> None:
        session = async_get_clientsession(self._hass)
        async with asyncio.timeout(HELLO_TIMEOUT) as hello_deadline:
            ws = await _async_open(
                session, self.host, self.port, self._token, heartbeat=WS_HEARTBEAT
            )
        try:
            # One deadline covers connect + hello, as in the config flow.
            async with asyncio.timeout_at(hello_deadline.when()):
                hello = await _async_receive_hello(ws)
            self._handle_message(hello)
            while True:
                # Raises TimeoutError after INACTIVITY_TIMEOUT without any frame.
                msg = await ws.receive(timeout=INACTIVITY_TIMEOUT)
                if msg.type in _CLOSED_TYPES:
                    raise CannotConnect("Bridge closed the connection")
                if (data := _decode(msg)) is not None:
                    self._handle_message(data)
        finally:
            await ws.close()

    @callback
    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        async_dispatcher_send(self._hass, self._signal_update, None)

    @callback
    def _handle_message(self, msg: JsonDict) -> None:
        msg_type = msg["type"]
        self.messages_received += 1
        if msg_type != "ping":
            self.messages.append(msg)
        self.state = apply(self.state, msg)
        if msg_type == "hello":
            self._on_hello(msg)
        async_dispatcher_send(self._hass, self._signal_update, msg_type)
        if msg_type == "event":
            self._on_event(msg)
        elif msg_type == "alert":
            self._on_alert(msg)
        elif msg_type == "summary":
            self._on_summary()

    @callback
    def _on_hello(self, msg: JsonDict) -> None:
        if self._outage_logged:
            _LOGGER.info("Artisan bridge at %s:%s is back", self.host, self.port)
            self._outage_logged = False
        self.failed_attempts = 0
        self.connected = True
        if msg.get("protocol") != PROTOCOL_VERSION:
            _LOGGER.warning(
                "Artisan bridge speaks protocol %s, expected %s",
                msg.get("protocol"),
                PROTOCOL_VERSION,
            )
        if msg["bridge_id"] != self.bridge_id:
            _LOGGER.warning(
                "Artisan bridge at %s:%s reports id %s, configured for %s",
                self.host,
                self.port,
                msg["bridge_id"],
                self.bridge_id,
            )
        hostname = self.state.hostname or self.host
        dr.async_get(self._hass).async_update_device(
            self.device_id,
            sw_version=self.state.bridge_version,
            name=device_name(hostname),
        )
        # Persist what entities need before the first connection after a restart.
        data = {**self._entry.data, CONF_UNIT: self.state.unit, CONF_HOSTNAME: hostname}
        if data != self._entry.data:
            self._hass.config_entries.async_update_entry(self._entry, data=data)

    @callback
    def _on_event(self, msg: JsonDict) -> None:
        event = msg.get("event")
        if event not in ROAST_EVENTS:
            _LOGGER.debug("Ignoring unknown roast event %s", event)
            return
        attributes = {key: msg.get(key) for key in ("tm", "bt", "et", ATTR_ROAST_ID)}
        self._fire(event, attributes)

    @callback
    def _on_alert(self, msg: JsonDict) -> None:
        # Alerts carry no roast context, so attach the current one.
        state = self.state
        attributes = {
            "tm": state.tm,
            "bt": state.bt,
            "et": state.et,
            ATTR_ROAST_ID: state.roast_id,
            ATTR_TITLE: msg.get(ATTR_TITLE),
            ATTR_TEXT: msg.get(ATTR_TEXT),
        }
        self._fire(EVENT_ALERT, attributes)

    @callback
    def _fire(self, event: str, attributes: JsonDict) -> None:
        self._hass.bus.async_fire(
            EVENT_ARTISAN,
            {"device_id": self.device_id, ATTR_EVENT: event, **attributes},
        )
        async_dispatcher_send(self._hass, self._signal_event, event, attributes)

    @callback
    def _on_summary(self) -> None:
        if (summary := self.state.last_summary) is not None:
            self._hass.bus.async_fire(
                EVENT_ROAST_SUMMARY, {"device_id": self.device_id, **summary}
            )


@dataclass
class ArtisanRuntimeData:
    """Runtime data stored in the config entry."""

    client: ArtisanBridgeClient

    @property
    def state(self) -> RoastState:
        """Return the client's current roast state."""
        return self.client.state


type ArtisanConfigEntry = ConfigEntry[ArtisanRuntimeData]
