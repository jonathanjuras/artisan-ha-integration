"""Tests for diagnostics."""

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import BRIDGE_ID, TOKEN, FakeBridge, push_and_wait


async def test_diagnostics_redact_token(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_bridge: FakeBridge,
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    for i in range(25):
        await push_and_wait(hass, fake_bridge, init_integration, "sample", bt=190.0 + i)
    await push_and_wait(hass, fake_bridge, init_integration, "ping")

    diag = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert TOKEN not in json.dumps(diag)
    assert diag["entry"]["data"]["token"] == REDACTED
    assert diag["entry"]["data"]["host"] == "127.0.0.1"
    assert diag["entry"]["unique_id"] == BRIDGE_ID
    assert diag["connection"]["connected"] is True
    assert diag["state"]["phase"] == "development"
    assert diag["state"]["bt"] == 214.0
    # Ring buffer: the last 20 messages, pings excluded.
    messages = diag["messages"]
    assert len(messages) == 20
    assert messages[-1]["type"] == "sample"
    assert messages[-1]["bt"] == 214.0
