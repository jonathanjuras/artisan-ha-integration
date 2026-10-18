"""Tests for device triggers."""

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.artisan.const import DOMAIN, EVENT_TYPES
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from .conftest import FakeBridge, get_client, push_and_wait


async def test_get_triggers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device_id = get_client(init_integration).device_id
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    # Entity triggers of the sensors are listed too; check only ours.
    ours = [t for t in triggers if t["domain"] == DOMAIN]
    assert sorted(ours, key=lambda t: t["type"]) == sorted(
        (
            {
                "platform": "device",
                "domain": DOMAIN,
                "device_id": device_id,
                "type": trigger_type,
                "metadata": {},
            }
            for trigger_type in EVENT_TYPES
        ),
        key=lambda t: t["type"],
    )


async def setup_automation(
    hass: HomeAssistant, device_id: str, trigger_type: str
) -> None:
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "triggers": [
                        {
                            "trigger": "device",
                            "domain": DOMAIN,
                            "device_id": device_id,
                            "type": trigger_type,
                        }
                    ],
                    "actions": [
                        {
                            "action": "test.automation",
                            "data": {
                                "event": "{{ trigger.event.data.event }}",
                                "bt": "{{ trigger.event.data.bt }}",
                            },
                        }
                    ],
                }
            ]
        },
    )


async def test_device_trigger_fires(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
    service_calls: list[ServiceCall],
) -> None:
    device_id = get_client(init_integration).device_id
    await setup_automation(hass, device_id, "fcs")

    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event="drop", tm=620.0, bt=208.4
    )
    assert not [c for c in service_calls if c.domain == "test"]

    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event="fcs", tm=464.0, bt=194.0
    )
    calls = [c for c in service_calls if c.domain == "test"]
    assert len(calls) == 1
    assert calls[0].data == {"event": "fcs", "bt": 194.0}


async def test_alert_device_trigger(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
    service_calls: list[ServiceCall],
) -> None:
    await setup_automation(hass, get_client(init_integration).device_id, "alert")
    await push_and_wait(
        hass, fake_bridge, init_integration, "alert", title=None, text="Hot"
    )
    calls = [c for c in service_calls if c.domain == "test"]
    assert len(calls) == 1
    assert calls[0].data["event"] == "alert"


async def test_trigger_ignores_other_devices(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
    service_calls: list[ServiceCall],
) -> None:
    """An automation for a second bridge does not fire for this one."""
    other_entry = MockConfigEntry(domain=DOMAIN, unique_id="other-bridge")
    other_entry.add_to_hass(hass)
    other_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id, identifiers={(DOMAIN, "other-bridge")}
    )
    await setup_automation(hass, other_device.id, "fcs")
    await push_and_wait(
        hass, fake_bridge, init_integration, "event", event="fcs", tm=1.0
    )
    assert not [c for c in service_calls if c.domain == "test"]
