"""Tests for sensors and binary sensors fed by the fake bridge."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.artisan.const import (
    DOMAIN,
    EVENT_ROAST_SUMMARY,
    OPT_ROR_PRECISION,
    OPT_SAMPLE_INTERVAL,
    PHASES,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import (
    BRIDGE_ID,
    DEVICE_PREFIX,
    FakeBridge,
    get_client,
    push_and_wait,
    wait_for,
)


def sensor(key: str) -> str:
    return f"sensor.{DEVICE_PREFIX}_{key}"


def binary(key: str) -> str:
    return f"binary_sensor.{DEVICE_PREFIX}_{key}"


async def test_entities_from_snapshot(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    expected = {
        sensor("bean_temperature"): ("196.3", "°C"),
        sensor("environment_temperature"): ("225.0", "°C"),
        sensor("bt_rate_of_rise"): ("9.8", "°C/min"),
        sensor("et_rate_of_rise"): ("4.1", "°C/min"),
        sensor("roast_time"): ("512.0", "s"),
        sensor("development_time"): ("48.0", "s"),
        sensor("development_time_ratio"): ("9.4", "%"),
        sensor("phase"): ("development", None),
        sensor("heat"): ("70.0", "%"),
        sensor("fan"): ("40.0", "%"),
        sensor("charge_temperature"): (STATE_UNKNOWN, "°C"),
        sensor("first_crack_temperature"): (STATE_UNKNOWN, "°C"),
        sensor("drop_temperature"): (STATE_UNKNOWN, "°C"),
        sensor("last_roast_completed"): (STATE_UNKNOWN, None),
        binary("bridge_connected"): (STATE_ON, None),
        binary("artisan_running"): (STATE_ON, None),
        binary("roasting"): (STATE_ON, None),
    }
    for entity_id, (value, unit) in expected.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state == value, entity_id
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, entity_id

    phase = hass.states.get(sensor("phase"))
    assert phase.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert phase.attributes["options"] == list(PHASES)

    registry = er.async_get(hass)
    entry = registry.async_get(binary("bridge_connected"))
    assert entry.unique_id == f"{BRIDGE_ID}_connected"
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert registry.async_get(sensor("bean_temperature")).unique_id == f"{BRIDGE_ID}_bt"
    assert registry.async_get(sensor("heat")).unique_id == f"{BRIDGE_ID}_control_heat"


async def test_sample_updates(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await push_and_wait(
        hass,
        fake_bridge,
        init_integration,
        "sample",
        tm=520.0,
        bt=197.1,
        et=226.0,
        bt_ror=9.87,
        et_ror=None,
    )
    assert hass.states.get(sensor("bean_temperature")).state == "197.1"
    assert hass.states.get(sensor("environment_temperature")).state == "226.0"
    # ror_precision=1 rounds the state.
    assert hass.states.get(sensor("bt_rate_of_rise")).state == "9.9"
    assert hass.states.get(sensor("et_rate_of_rise")).state == STATE_UNKNOWN
    assert hass.states.get(sensor("roast_time")).state == "520.0"
    # Derived live from tm and the FCs time while in development.
    assert hass.states.get(sensor("development_time")).state == "56.0"
    assert hass.states.get(sensor("development_time_ratio")).state == "10.8"


async def test_state_message(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await push_and_wait(
        hass,
        fake_bridge,
        init_integration,
        "state",
        phase="cooling",
        monitoring=True,
        recording=True,
        artisan_online=False,
        roast_id="2026-10-17T14:03:12Z",
    )
    assert hass.states.get(sensor("phase")).state == "cooling"
    assert hass.states.get(binary("roasting")).state == STATE_OFF
    assert hass.states.get(binary("artisan_running")).state == STATE_OFF


async def test_roast_time_unknown_before_charge(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await push_and_wait(
        hass,
        fake_bridge,
        init_integration,
        "event",
        event="reset",
        tm=None,
        bt=None,
        et=None,
        roast_id=None,
    )
    await push_and_wait(hass, fake_bridge, init_integration, "sample", tm=12.0)
    assert hass.states.get(sensor("roast_time")).state == STATE_UNKNOWN
    await push_and_wait(
        hass,
        fake_bridge,
        init_integration,
        "event",
        event="charge",
        tm=0.0,
        bt=185.0,
        et=210.0,
        roast_id="r2",
    )
    await push_and_wait(hass, fake_bridge, init_integration, "sample", tm=3.0)
    assert hass.states.get(sensor("roast_time")).state == "3.0"
    assert hass.states.get(sensor("development_time")).state == STATE_UNKNOWN


async def test_summary_sensors_and_bus_event(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    events = async_capture_events(hass, EVENT_ROAST_SUMMARY)
    summary = {
        "roast_id": "2026-10-17T14:03:12Z",
        "final": False,
        "charge_bt": 185.2,
        "dry_tm": 245.0,
        "dry_bt": 152.1,
        "fcs_tm": 464.0,
        "fcs_bt": 194.0,
        "drop_tm": 620.0,
        "drop_bt": 208.4,
        "dev_time": 156.0,
        "dtr": 25.2,
        "cool_tm": None,
        "title": None,
        "beans": None,
        "weight_in_g": None,
        "weight_out_g": None,
    }
    sent = await push_and_wait(
        hass, fake_bridge, init_integration, "summary", **summary
    )
    assert hass.states.get(sensor("charge_temperature")).state == "185.2"
    assert hass.states.get(sensor("first_crack_temperature")).state == "194.0"
    assert hass.states.get(sensor("drop_temperature")).state == "208.4"
    # Not final yet: the roast has not completed.
    assert hass.states.get(sensor("last_roast_completed")).state == STATE_UNKNOWN

    device_id = get_client(init_integration).device_id
    assert len(events) == 1
    assert events[0].data == {"device_id": device_id, "ts": sent["ts"], **summary}

    final = {**summary, "final": True, "cool_tm": 860.0, "title": "Ethiopia"}
    sent = await push_and_wait(hass, fake_bridge, init_integration, "summary", **final)
    last = hass.states.get(sensor("last_roast_completed"))
    assert last.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    # HA stores timestamp states with second resolution.
    assert dt_util.parse_datetime(last.state) == dt_util.utc_from_timestamp(
        sent["ts"] // 1000
    )
    assert last.attributes["title"] == "Ethiopia"
    assert last.attributes["dtr"] == 25.2
    assert last.attributes["final"] is True
    assert len(events) == 2
    assert events[1].data["final"] is True


async def test_dynamic_control_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    assert hass.states.get(sensor("drum")) is None
    await push_and_wait(
        hass, fake_bridge, init_integration, "control", channel="drum", value=55.0
    )
    state = hass.states.get(sensor("drum"))
    assert state is not None
    assert state.state == "55.0"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert state.attributes["friendly_name"].endswith(" Drum")

    await push_and_wait(
        hass, fake_bridge, init_integration, "control", channel="heat", value=85.0
    )
    assert hass.states.get(sensor("heat")).state == "85.0"
    # Channels outside [a-z0-9]{1,16} never become entities.
    await push_and_wait(
        hass, fake_bridge, init_integration, "control", channel="Bad Name", value=1.0
    )
    assert not [
        entity_id
        for entity_id in hass.states.async_entity_ids("sensor")
        if "bad" in entity_id
    ]


async def test_control_sensors_restored_from_registry(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    """Channels seen before a restart exist while the bridge is still down."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{BRIDGE_ID}_control_drum",
        config_entry=config_entry,
        suggested_object_id=f"{DEVICE_PREFIX}_drum",
    )
    fake_bridge.reject_status = 503
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(sensor("drum")).state == STATE_UNAVAILABLE

    fake_bridge.reject_status = None
    client = get_client(config_entry)
    await wait_for(hass, lambda: client.connected and client.state.phase is not None)
    # The new snapshot has no drum value; the entity stays, without a value.
    assert hass.states.get(sensor("drum")).state == STATE_UNKNOWN
    assert hass.states.get(sensor("heat")).state == "70.0"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "entry_options", [{OPT_SAMPLE_INTERVAL: 5.0, OPT_ROR_PRECISION: 1}]
)
async def test_sample_interval_throttles_writes(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    bt = sensor("bean_temperature")
    # The snapshot is written at once, not throttled.
    assert hass.states.get(bt).state == "196.3"
    await push_and_wait(hass, fake_bridge, init_integration, "sample", bt=197.0)
    await push_and_wait(hass, fake_bridge, init_integration, "sample", bt=198.0)
    # Within the interval of the snapshot write: deferred.
    assert hass.states.get(bt).state == "196.3"
    # Low-rate sensors are never throttled.
    await push_and_wait(hass, fake_bridge, init_integration, "state", phase="cooling")
    assert hass.states.get(sensor("phase")).state == "cooling"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    # The latest value is written once the interval ends.
    assert hass.states.get(bt).state == "198.0"

    # Going unavailable bypasses the throttle.
    fake_bridge.reject_status = 503
    await fake_bridge.drop_clients()
    client = get_client(init_integration)
    await wait_for(hass, lambda: not client.connected)
    assert hass.states.get(bt).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    "entry_options", [{OPT_SAMPLE_INTERVAL: 0.0, OPT_ROR_PRECISION: 0}]
)
async def test_ror_precision(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await push_and_wait(hass, fake_bridge, init_integration, "sample", bt_ror=9.6)
    assert float(hass.states.get(sensor("bt_rate_of_rise")).state) == 10
    entry = er.async_get(hass).async_get(sensor("bt_rate_of_rise"))
    assert entry.options["sensor"]["suggested_display_precision"] == 0
