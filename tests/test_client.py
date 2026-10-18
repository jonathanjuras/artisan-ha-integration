"""Tests for the bridge client: connection lifecycle and the message reducer."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.artisan import client as client_module
from custom_components.artisan.client import ArtisanBridgeClient
from custom_components.artisan.const import CONF_UNIT, DOMAIN
from custom_components.artisan.models import RoastState, apply
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import (
    BRIDGE_ID,
    DEVICE_PREFIX,
    HOSTNAME,
    TOKEN,
    FakeBridge,
    get_client,
    push_and_wait,
    snapshot_state,
    unused_port,
    wait_for,
)

BT = f"sensor.{DEVICE_PREFIX}_bean_temperature"
PHASE = f"sensor.{DEVICE_PREFIX}_phase"
CONNECTED = f"binary_sensor.{DEVICE_PREFIX}_bridge_connected"


async def test_setup_connects_and_updates_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    assert fake_bridge.connections == 1
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, BRIDGE_ID), init_integration.entry_id
    )
    assert device is not None
    assert device.sw_version == "0.1.0"
    assert device.name == f"Artisan ({HOSTNAME})"
    assert device.manufacturer == "Artisan"
    assert hass.states.get(CONNECTED).state == STATE_ON
    assert hass.states.get(BT).state == "196.3"


async def test_setup_does_not_wait_for_bridge(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Setup succeeds with the bridge down; entities start unavailable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: unused_port(), CONF_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # Entity IDs fall back to the host until the first hello names the device.
    assert hass.states.get("sensor.artisan_127_0_0_1_bean_temperature").state == (
        STATE_UNAVAILABLE
    )
    assert hass.states.get(
        "binary_sensor.artisan_127_0_0_1_bridge_connected"
    ).state == (STATE_OFF)
    await wait_for(hass, lambda: get_client(entry).failed_attempts >= 2)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_on_disconnect_and_snapshot_on_reconnect(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    client = get_client(init_integration)
    fake_bridge.reject_status = 503  # keep the client out while we look
    await fake_bridge.drop_clients()
    await wait_for(hass, lambda: not client.connected)
    assert hass.states.get(BT).state == STATE_UNAVAILABLE
    assert hass.states.get(PHASE).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_OFF
    await wait_for(hass, lambda: client.failed_attempts >= 2)

    # The roast moved on while HA was disconnected.
    fake_bridge.state.update(phase="cooling", bt=201.5, controls={"heat": 0.0})
    fake_bridge.reject_status = None
    await wait_for(hass, lambda: client.connected and client.state.phase == "cooling")
    assert fake_bridge.connections == 2
    assert client.failed_attempts == 0  # reset by hello
    assert hass.states.get(BT).state == "201.5"
    assert hass.states.get(PHASE).state == "cooling"
    assert hass.states.get(f"binary_sensor.{DEVICE_PREFIX}_roasting").state == STATE_OFF
    assert hass.states.get(f"sensor.{DEVICE_PREFIX}_heat").state == "0.0"
    # A channel missing from the new snapshot has no value anymore.
    assert hass.states.get(f"sensor.{DEVICE_PREFIX}_fan").state == "unknown"
    assert hass.states.get(CONNECTED).state == STATE_ON


async def test_invalid_auth_starts_reauth_and_stops(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    fake_bridge.token = "rotated"
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await wait_for(
        hass,
        lambda: any(
            flow["context"]["source"] == SOURCE_REAUTH
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        ),
    )
    assert fake_bridge.auth_failures == 1
    # No retries with a token known to be wrong.
    for _ in range(20):
        await hass.async_block_till_done()
    assert fake_bridge.auth_failures == 1
    assert hass.states.get(BT).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_inactivity_timeout_reconnects(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """45 s (here 0.2 s) without any frame means the link is dead."""
    monkeypatch.setattr(client_module, "INACTIVITY_TIMEOUT", 0.2)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_for(hass, lambda: fake_bridge.connections >= 2)
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_pings_keep_connection_alive(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = get_client(init_integration)
    before = len(client.messages)
    await push_and_wait(hass, fake_bridge, init_integration, "ping")
    # Pings are not worth a diagnostics slot.
    assert len(client.messages) == before


async def test_garbage_frames_are_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    client = get_client(init_integration)
    for ws in fake_bridge.clients:
        await ws.send_str("not json")
        await ws.send_str("[1, 2]")
        await ws.send_bytes(b"\x00")
    await push_and_wait(hass, fake_bridge, init_integration, "sample", bt=197.0)
    assert client.connected
    assert hass.states.get(BT).state == "197.0"


async def test_unknown_message_type_is_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    client = get_client(init_integration)
    before = client.state
    await push_and_wait(hass, fake_bridge, init_integration, "future_thing", x=1)
    assert client.state == before


async def test_fahrenheit_bridge(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    fake_bridge.hello["unit"] = "F"
    fake_bridge.state.update(bt=392.0, bt_ror=18.0)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    client = get_client(config_entry)
    await wait_for(hass, lambda: client.connected and client.state.bt == 392.0)
    # HA converts temperatures to the (metric) unit system; RoR stays native.
    bt = hass.states.get(BT)
    assert bt.attributes["unit_of_measurement"] == "°C"
    assert float(bt.state) == pytest.approx(200.0)
    ror = hass.states.get(f"sensor.{DEVICE_PREFIX}_bt_rate_of_rise")
    assert ror.attributes["unit_of_measurement"] == "°F/min"
    assert ror.state == "18.0"
    # Remembered for the next start, before the bridge is reachable.
    assert config_entry.data[CONF_UNIT] == "F"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    await wait_for(hass, lambda: not fake_bridge.clients)


def test_backoff_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "BACKOFF_INITIAL", 1.0)
    monkeypatch.setattr(client_module, "BACKOFF_MAX", 60.0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        data={CONF_HOST: "h", CONF_PORT: 1, CONF_TOKEN: TOKEN},
    )
    client = ArtisanBridgeClient(None, entry, "device")  # type: ignore[arg-type]
    with patch.object(client_module.random, "uniform", return_value=0.0):
        delays = [client._next_backoff() for _ in range(9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    with patch.object(client_module.random, "uniform", side_effect=lambda a, b: b):
        client.failed_attempts = 0
        assert client._next_backoff() == pytest.approx(0.8)  # 20% jitter


# --- reducer -----------------------------------------------------------------


def msg(msg_type: str, **fields: Any) -> dict[str, Any]:
    return {"v": 1, "type": msg_type, "ts": 1792252483254, **fields}


def snapshot(**overrides: Any) -> dict[str, Any]:
    return msg("snapshot", state={**snapshot_state(), **overrides})


def test_reducer_hello() -> None:
    state = apply(
        RoastState(),
        msg(
            "hello",
            bridge_version="0.2.0",
            bridge_id="b",
            hostname="lab",
            unit="F",
            protocol=1,
        ),
    )
    assert (state.bridge_id, state.bridge_version, state.hostname) == (
        "b",
        "0.2.0",
        "lab",
    )
    assert (state.unit, state.protocol) == ("F", 1)
    # An invalid unit keeps the previous one.
    assert apply(state, msg("hello", bridge_id="b", unit="K")).unit == "F"


def test_reducer_snapshot_replaces_everything() -> None:
    state = apply(RoastState(), snapshot())
    assert state.phase == "development"
    assert state.roast_time == 512.0
    assert state.controls == {"heat": 70.0, "fan": 40.0}
    assert state.last_event == {"event": "fcs", "ts": 1792252483254}
    state = apply(state, snapshot(phase="bogus", controls=None, events=None, bt=None))
    assert state.phase is None
    assert state.controls == {}
    assert state.events == {}
    assert state.bt is None
    assert state.roast_time is None  # not charged
    # A malformed snapshot is ignored.
    assert apply(state, msg("snapshot", state=[])) is state


def test_reducer_sample_partial_and_live_development() -> None:
    state = apply(RoastState(), snapshot())
    state = apply(state, msg("sample", tm=520.0, bt=197.1))
    assert (state.tm, state.bt, state.et) == (520.0, 197.1, 225.0)
    assert state.dev_time == 56.0
    assert state.dtr == 10.8
    state = apply(state, msg("sample", bt=None, bt_ror="x"))
    assert state.bt is None
    assert state.bt_ror is None
    # Frozen after DROP.
    state = apply(state, msg("state", phase="cooling"))
    frozen = state.dev_time
    assert apply(state, msg("sample", tm=600.0)).dev_time == frozen


def test_reducer_events() -> None:
    state = apply(RoastState(), snapshot())
    state = apply(
        state, msg("event", event="drop", tm=620.0, bt=208.4, et=230.0, roast_id="r1")
    )
    assert state.events["drop"] == {"tm": 620.0, "bt": 208.4, "et": 230.0}
    assert state.last_event == {"event": "drop", "ts": 1792252483254}
    assert state.roast_id == "r1"
    state = apply(
        state, msg("event", event="charge", tm=0.0, bt=180.0, et=200.0, roast_id="r2")
    )
    assert set(state.events) == {"charge"}
    assert state.dev_time is None
    assert state.roast_id == "r2"
    state = apply(state, msg("event", event="reset", tm=None, bt=None, et=None))
    assert state.events == {}
    assert state.roast_time is None
    assert apply(state, msg("event", tm=1.0)) is state


def test_reducer_control_state_summary() -> None:
    state = apply(RoastState(), msg("control", channel="drum", value=55))
    assert state.controls == {"drum": 55.0}
    assert apply(state, msg("control", channel="", value=1)) is state
    state = apply(
        state, msg("state", phase="drying", monitoring=True, artisan_online=False)
    )
    assert (state.phase, state.monitoring, state.artisan_online) == (
        "drying",
        True,
        False,
    )
    assert state.recording is None
    summary = msg("summary", roast_id="r1", final=False, drop_bt=208.4)
    state = apply(state, summary)
    assert state.last_summary == {
        "ts": 1792252483254,
        "roast_id": "r1",
        "final": False,
        "drop_bt": 208.4,
    }
    assert state.last_final_summary is None
    state = apply(state, {**summary, "final": True})
    assert state.last_final_summary is not None
    # The next roast's DROP summary must not erase the last completed roast.
    state = apply(state, msg("summary", roast_id="r2", final=False))
    assert state.last_final_summary["roast_id"] == "r1"
    # Snapshots carry the last summary too.
    state = apply(
        state, snapshot(last_summary={"roast_id": "r3", "final": True, "ts": 5})
    )
    assert state.last_final_summary == {"roast_id": "r3", "final": True, "ts": 5}


def test_reducer_ignores_untyped() -> None:
    state = RoastState()
    assert apply(state, {"v": 1}) is state
    assert apply(state, msg("ping")) is state
    assert apply(state, msg("alert", text="x")) is state


def test_reducer_summary_freezes_dev_time_at_drop() -> None:
    """A throttled sample stream lags DROP; the summary's frozen values win."""
    state = RoastState(
        phase="development",
        roast_id="r1",
        tm=600.0,
        events={"fcs": {"tm": 464.0, "bt": 194.0, "et": None}},
    )
    state = apply(state, {"v": 1, "type": "sample", "ts": 1, "tm": 600.0})
    assert state.dev_time == 136.0
    summary = {"v": 1, "type": "summary", "ts": 2, "roast_id": "r1", "final": False}
    state = apply(state, {**summary, "dev_time": 156.0, "dtr": 25.2})
    assert (state.dev_time, state.dtr) == (156.0, 25.2)
    # A summary for another roast must not touch the current roast's values.
    state = apply(state, {**summary, "roast_id": "r0", "dev_time": 1.0, "dtr": 1.0})
    assert (state.dev_time, state.dtr) == (156.0, 25.2)
