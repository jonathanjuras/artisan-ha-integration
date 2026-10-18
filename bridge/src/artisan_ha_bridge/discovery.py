"""Zeroconf advertisement of the HA server (spec 4.3)."""

from __future__ import annotations

import inspect
import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import Final, Protocol

import ifaddr
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .ha_server import PATH
from .protocol import PROTOCOL_VERSION

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE: Final = "_artisan-bridge._tcp.local."
WILDCARD_BINDS: Final = frozenset({"", "0.0.0.0", "::"})


class Advertiser(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _AsyncZeroconfLike(Protocol):
    async def async_register_service(
        self, info: ServiceInfo, *, allow_name_change: bool = ...
    ) -> object: ...

    async def async_unregister_service(self, info: ServiceInfo) -> object: ...

    async def async_close(self) -> None: ...


def local_ipv4_addresses() -> list[str]:
    """All non-loopback IPv4 addresses of this machine."""
    found: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4 or not isinstance(ip.ip, str):
                continue
            if ipaddress.IPv4Address(ip.ip).is_loopback or ip.ip in found:
                continue
            found.append(ip.ip)
    return found


def advertised_addresses(
    bind: str, addresses: Callable[[], list[str]] = local_ipv4_addresses
) -> list[str]:
    if bind in WILDCARD_BINDS:
        return addresses()
    return [bind]


def build_service_info(
    *, bridge_id: str, hostname: str, port: int, addresses: list[str]
) -> ServiceInfo:
    short_host = hostname.split(".", maxsplit=1)[0] or "artisan"
    return ServiceInfo(
        SERVICE_TYPE,
        f"Artisan Bridge ({hostname}).{SERVICE_TYPE}",
        addresses=[socket.inet_aton(address) for address in addresses],
        port=port,
        properties={
            "id": bridge_id,
            "version": __version__,
            "path": PATH,
            "protocol": str(PROTOCOL_VERSION),
        },
        server=f"{short_host}.local.",
    )


class Discovery:
    """Registers `_artisan-bridge._tcp.local.`. Failures are logged, never fatal."""

    def __init__(
        self,
        *,
        bridge_id: str,
        hostname: str,
        bind: str,
        port: int,
        zeroconf_factory: Callable[[], _AsyncZeroconfLike] = AsyncZeroconf,
        addresses: Callable[[], list[str]] = local_ipv4_addresses,
    ) -> None:
        self._bridge_id = bridge_id
        self._hostname = hostname
        self._bind = bind
        self._port = port
        self._factory = zeroconf_factory
        self._addresses = addresses
        self._zeroconf: _AsyncZeroconfLike | None = None
        self._info: ServiceInfo | None = None

    async def start(self) -> None:
        addresses = advertised_addresses(self._bind, self._addresses)
        if not addresses:
            _LOGGER.warning("No non-loopback IPv4 address found; zeroconf advertisement skipped")
            return
        info = build_service_info(
            bridge_id=self._bridge_id, hostname=self._hostname, port=self._port, addresses=addresses
        )
        zeroconf = self._factory()
        try:
            pending = await zeroconf.async_register_service(info, allow_name_change=True)
            if inspect.isawaitable(pending):  # AsyncZeroconf returns the registration task
                await pending
        except Exception:  # zeroconf raises a variety of errors; discovery is optional
            _LOGGER.exception("Zeroconf registration failed; add the bridge in HA manually")
            await zeroconf.async_close()
            return
        self._zeroconf, self._info = zeroconf, info
        _LOGGER.info("Advertising %s on %s port %d", info.name, ", ".join(addresses), self._port)

    async def stop(self) -> None:
        if self._zeroconf is None or self._info is None:
            return
        try:
            pending = await self._zeroconf.async_unregister_service(self._info)
            if inspect.isawaitable(pending):
                await pending
        finally:
            await self._zeroconf.async_close()
            self._zeroconf = self._info = None
