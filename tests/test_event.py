"""Tests for the roast event entity and the artisan_event bus event."""

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.artisan.const import EVENT_ARTISAN, EVENT_TYPES, ROAST_EVENTS
from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .conftest import DEVICE_PREFIX, FakeBridge, get_client, push_and_wait, wait_for

EVENT_ENTITY = f"event.{DEVICE_PREFIX}_roast_event"


async def test_event_entity_setup(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(EVENT_ENTITY)
    assert state is not None
    # The snapshot's last_event is history, not a new event.
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPES] == list(EVENT_TYPES)
    assert len(EVENT_TYPES) == 13


@pytest.mark.parametrize("event", ROAST_EVENTS)
async def test_roast_events(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
    event: str,
) -> None:
    bus_events = async_capture_events(hass, EVENT_ARTISAN)
    fields = {"tm": 464.0, "bt": 194.0, "et": 221.3, "roast_id": "2026-10-17T14:03:12Z"}
    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event=event, **fields
    )

    state = hass.states.get(EVENT_ENTITY)
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert state.attributes[ATTR_EVENT_TYPE] == event
    for key, value in fields.items():
        assert state.attributes[key] == value

    assert len(bus_events) == 1
    assert bus_events[0].data == {
        "device_id": get_client(init_integration).device_id,
        "event": event,
        **fields,
    }


async def test_alert(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    bus_events = async_capture_events(hass, EVENT_ARTISAN)
    await push_and_wait(
        hass, fake_bridge, init_integration, "alert", title="Alarm", text="BT above 220"
    )
    state = hass.states.get(EVENT_ENTITY)
    assert state.attributes[ATTR_EVENT_TYPE] == "alert"
    assert state.attributes["text"] == "BT above 220"
    assert state.attributes["title"] == "Alarm"
    assert len(bus_events) == 1
    # Alerts carry the roast context at the time they arrive.
    assert bus_events[0].data == {
        "device_id": get_client(init_integration).device_id,
        "event": "alert",
        "tm": 512.0,
        "bt": 196.3,
        "et": 225.0,
        "roast_id": "2026-10-17T14:03:12Z",
        "title": "Alarm",
        "text": "BT above 220",
    }


async def test_unknown_event_is_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    bus_events = async_capture_events(hass, EVENT_ARTISAN)
    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event="explode", tm=1.0
    )
    assert bus_events == []
    assert hass.states.get(EVENT_ENTITY).state == STATE_UNKNOWN


async def test_samples_do_not_touch_event_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event="drop", tm=620.0
    )
    before = hass.states.get(EVENT_ENTITY)
    await push_and_wait(hass, fake_bridge, init_integration, "sample", bt=200.0)
    after = hass.states.get(EVENT_ENTITY)
    assert after.state == before.state
    assert after.last_reported == before.last_reported


async def test_event_entity_unavailable_on_disconnect(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_bridge: FakeBridge
) -> None:
    fake_bridge.reject_status = 503
    await fake_bridge.drop_clients()
    client = get_client(init_integration)
    await wait_for(hass, lambda: not client.connected)
    assert hass.states.get(EVENT_ENTITY).state == STATE_UNAVAILABLE
