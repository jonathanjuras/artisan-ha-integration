"""Tests for the config flow."""

from collections.abc import Generator
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.artisan.const import (
    CONF_HOSTNAME,
    CONF_UNIT,
    DOMAIN,
    OPT_ROR_PRECISION,
    OPT_SAMPLE_INTERVAL,
)
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import BRIDGE_ID, HOSTNAME, TOKEN, FakeBridge, unused_port


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep flow tests from starting the real client."""
    with patch(
        "custom_components.artisan.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def zeroconf_info(
    port: int, properties: dict[str, Any] | None = None
) -> ZeroconfServiceInfo:
    """Return discovery info as the bridge advertises it (spec 4.3 discovery)."""
    return ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        port=port,
        hostname=f"{HOSTNAME}.local.",
        type="_artisan-bridge._tcp.local.",
        name=f"Artisan Bridge ({HOSTNAME})._artisan-bridge._tcp.local.",
        properties=(
            {"id": BRIDGE_ID, "version": "0.1.0", "path": "/v1", "protocol": "1"}
            if properties is None
            else properties
        ),
    )


def user_input(bridge: FakeBridge, token: str = TOKEN) -> dict[str, Any]:
    return {CONF_HOST: "127.0.0.1", CONF_PORT: bridge.port, CONF_TOKEN: token}


async def test_user_flow(hass: HomeAssistant, fake_bridge: FakeBridge) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Artisan ({HOSTNAME})"
    assert result["result"].unique_id == BRIDGE_ID
    assert result["data"] == {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: fake_bridge.port,
        CONF_TOKEN: TOKEN,
        CONF_HOSTNAME: HOSTNAME,
        CONF_UNIT: "C",
    }
    # Validation connected exactly once and disconnected again.
    assert fake_bridge.connections == 1


async def test_user_flow_invalid_auth_then_recover(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge, token="wrong")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_bridge.auth_failures == 1

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: unused_port(), CONF_TOKEN: TOKEN},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_http_error(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    fake_bridge.reject_status = 503
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_no_hello(
    hass: HomeAssistant, fake_bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that upgrades but never says hello is not a bridge."""
    monkeypatch.setattr("custom_components.artisan.client.HELLO_TIMEOUT", 0.2)
    fake_bridge.send_hello = False
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_wrong_first_frame(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    fake_bridge.hello = {"v": 1, "type": "sample", "ts": 0}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.artisan.config_flow.async_validate_connection",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input(fake_bridge)
        )
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_fahrenheit(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    fake_bridge.hello["unit"] = "F"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["data"][CONF_UNIT] == "F"


async def test_user_flow_already_configured_updates_host_port(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        data={CONF_HOST: "10.0.0.9", CONF_PORT: 1234, CONF_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_bridge)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert entry.data[CONF_PORT] == fake_bridge.port


async def test_zeroconf_flow(hass: HomeAssistant, fake_bridge: FakeBridge) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=zeroconf_info(fake_bridge.port),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {
        "name": f"Artisan Bridge ({HOSTNAME})",
        "host": "127.0.0.1",
        "port": str(fake_bridge.port),
    }
    # Discovery alone must not connect: the token is still unknown.
    assert fake_bridge.connections == 0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Artisan ({HOSTNAME})"
    assert result["result"].unique_id == BRIDGE_ID
    assert result["data"][CONF_HOST] == "127.0.0.1"
    assert result["data"][CONF_PORT] == fake_bridge.port
    assert result["data"][CONF_TOKEN] == TOKEN


async def test_zeroconf_cannot_connect(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(unused_port())
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("properties", [{}, {"id": ""}, {"version": "0.1.0"}])
async def test_zeroconf_without_bridge_id(
    hass: HomeAssistant, properties: dict[str, str]
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=zeroconf_info(8765, properties),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_artisan_bridge"


async def test_zeroconf_already_configured_updates_host_port(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        data={CONF_HOST: "10.0.0.9", CONF_PORT: 1234, CONF_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(8765)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert entry.data[CONF_PORT] == 8765
    assert entry.data[CONF_TOKEN] == TOKEN


def configured_entry(
    hass: HomeAssistant, bridge: FakeBridge, token: str
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title=f"Artisan ({HOSTNAME})",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: bridge.port,
            CONF_TOKEN: token,
            CONF_HOSTNAME: HOSTNAME,
            CONF_UNIT: "C",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_flow(hass: HomeAssistant, fake_bridge: FakeBridge) -> None:
    entry = configured_entry(hass, fake_bridge, "old-token")
    fake_bridge.token = "new-token"

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new-token"
    assert entry.data[CONF_HOST] == "127.0.0.1"


async def test_reauth_cannot_connect(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    entry = configured_entry(hass, fake_bridge, "old-token")
    fake_bridge.reject_status = 500
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_different_bridge(
    hass: HomeAssistant, fake_bridge: FakeBridge
) -> None:
    """A different bridge now answers at the configured address."""
    entry = configured_entry(hass, fake_bridge, "old-token")
    fake_bridge.hello["bridge_id"] = "another-bridge"
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_TOKEN] == "old-token"


async def test_options_flow(hass: HomeAssistant, fake_bridge: FakeBridge) -> None:
    entry = configured_entry(hass, fake_bridge, TOKEN)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_SAMPLE_INTERVAL: 2.5, OPT_ROR_PRECISION: 0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {OPT_SAMPLE_INTERVAL: 2.5, OPT_ROR_PRECISION: 0}
    assert isinstance(entry.options[OPT_ROR_PRECISION], int)


@pytest.mark.parametrize(
    "options",
    [
        {OPT_SAMPLE_INTERVAL: 0.5, OPT_ROR_PRECISION: 1},
        {OPT_SAMPLE_INTERVAL: 11, OPT_ROR_PRECISION: 1},
        {OPT_SAMPLE_INTERVAL: 1, OPT_ROR_PRECISION: 3},
    ],
)
async def test_options_flow_out_of_range(
    hass: HomeAssistant, fake_bridge: FakeBridge, options: dict[str, float]
) -> None:
    entry = configured_entry(hass, fake_bridge, TOKEN)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], options)
