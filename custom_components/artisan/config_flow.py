"""Config flow for the Artisan Roaster Scope integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

import probatio

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .client import (
    ArtisanConfigEntry,
    CannotConnect,
    InvalidAuth,
    async_validate_connection,
)
from .const import (
    CONF_HOSTNAME,
    CONF_UNIT,
    DEFAULT_PORT,
    DEFAULT_ROR_PRECISION,
    DEFAULT_SAMPLE_INTERVAL,
    DOMAIN,
    MAX_ROR_PRECISION,
    MAX_SAMPLE_INTERVAL,
    MIN_SAMPLE_INTERVAL,
    OPT_ROR_PRECISION,
    OPT_SAMPLE_INTERVAL,
    device_name,
)
from .models import JsonDict

_LOGGER = logging.getLogger(__name__)

_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TOKEN_SCHEMA = probatio.Schema({probatio.Required(CONF_TOKEN): _TOKEN_SELECTOR})
_USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        probatio.Required(CONF_TOKEN): _TOKEN_SELECTOR,
    }
)


async def _async_try_connect(
    flow: ConfigFlow, host: str, port: int, token: str
) -> tuple[JsonDict | None, dict[str, str]]:
    """Return the bridge's hello, or form errors."""
    try:
        hello = await async_validate_connection(flow.hass, host, port, token)
    except InvalidAuth:
        return None, {"base": "invalid_auth"}
    except CannotConnect as err:
        _LOGGER.debug("Cannot connect to Artisan bridge at %s:%s: %s", host, port, err)
        return None, {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error validating the Artisan bridge")
        return None, {"base": "unknown"}
    return hello, {}


def _entry_data(host: str, port: int, token: str, hello: JsonDict) -> dict[str, Any]:
    hostname = hello.get("hostname")
    return {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_TOKEN: token,
        CONF_HOSTNAME: hostname if isinstance(hostname, str) and hostname else host,
        CONF_UNIT: "F" if hello.get("unit") == "F" else "C",
    }


class ArtisanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow: manual, zeroconf discovery and reauth."""

    VERSION = 1

    _host: str
    _port: int
    _name: str

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ArtisanConfigEntry,
    ) -> ArtisanOptionsFlow:
        """Return the options flow."""
        return ArtisanOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a bridge by host, port and token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host: str = user_input[CONF_HOST].strip()
            port: int = user_input[CONF_PORT]
            token: str = user_input[CONF_TOKEN].strip()
            hello, errors = await _async_try_connect(self, host, port, token)
            if hello is not None:
                await self.async_set_unique_id(hello["bridge_id"])
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host, CONF_PORT: port}
                )
                data = _entry_data(host, port, token, hello)
                return self.async_create_entry(
                    title=device_name(data[CONF_HOSTNAME]), data=data
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_USER_SCHEMA, user_input),
            errors=errors,
        )

    @override
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a bridge advertised as `_artisan-bridge._tcp.local.`."""
        bridge_id = discovery_info.properties.get("id")
        if not isinstance(bridge_id, str) or not bridge_id or not discovery_info.port:
            return self.async_abort(reason="not_artisan_bridge")
        await self.async_set_unique_id(bridge_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.host, CONF_PORT: discovery_info.port}
        )
        self._host = discovery_info.host
        self._port = discovery_info.port
        # Instance name is "Artisan Bridge (<hostname>)"; show it as-is.
        self._name = discovery_info.name.removesuffix(f".{discovery_info.type}")
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the token of a discovered bridge."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token: str = user_input[CONF_TOKEN].strip()
            hello, errors = await _async_try_connect(
                self, self._host, self._port, token
            )
            if hello is not None:
                # Trust the bridge over the TXT record if they ever disagree.
                await self.async_set_unique_id(
                    hello["bridge_id"], raise_on_progress=False
                )
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: self._host, CONF_PORT: self._port}
                )
                data = _entry_data(self._host, self._port, token, hello)
                return self.async_create_entry(
                    title=device_name(data[CONF_HOSTNAME]), data=data
                )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=_TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={
                "name": self._name,
                "host": self._host,
                "port": str(self._port),
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the bridge rejected the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token: str = user_input[CONF_TOKEN].strip()
            hello, errors = await _async_try_connect(
                self, entry.data[CONF_HOST], entry.data[CONF_PORT], token
            )
            if hello is not None:
                await self.async_set_unique_id(hello["bridge_id"])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"name": entry.title},
        )


class ArtisanOptionsFlow(OptionsFlowWithReload):
    """Options: client-side write throttle and RoR precision."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    OPT_SAMPLE_INTERVAL: float(user_input[OPT_SAMPLE_INTERVAL]),
                    OPT_ROR_PRECISION: int(user_input[OPT_ROR_PRECISION]),
                }
            )
        schema = probatio.Schema(
            {
                probatio.Required(
                    OPT_SAMPLE_INTERVAL, default=DEFAULT_SAMPLE_INTERVAL
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SAMPLE_INTERVAL,
                        max=MAX_SAMPLE_INTERVAL,
                        step=0.5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                probatio.Required(
                    OPT_ROR_PRECISION, default=DEFAULT_ROR_PRECISION
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_ROR_PRECISION,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
