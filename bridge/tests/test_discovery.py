"""Zeroconf advertisement with a fake AsyncZeroconf (no multicast in tests)."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from typing import Any

import pytest
from zeroconf import ServiceInfo

from artisan_ha_bridge import __version__
from artisan_ha_bridge.discovery import (
    SERVICE_TYPE,
    Discovery,
    advertised_addresses,
    build_service_info,
    local_ipv4_addresses,
)


@dataclass
class FakeIP:
    ip: Any
    is_IPv4: bool


@dataclass
class FakeAdapter:
    ips: list[FakeIP]


def test_local_ipv4_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = [
        FakeAdapter([FakeIP("127.0.0.1", True), FakeIP(("::1", 0, 0), False)]),
        FakeAdapter([FakeIP("192.168.1.20", True), FakeIP(("fe80::1", 0, 2), False)]),
        FakeAdapter([FakeIP("10.0.0.5", True), FakeIP("192.168.1.20", True)]),
    ]
    monkeypatch.setattr("ifaddr.get_adapters", lambda: adapters)
    assert local_ipv4_addresses() == ["192.168.1.20", "10.0.0.5"]


def test_local_ipv4_addresses_real() -> None:
    for address in local_ipv4_addresses():
        socket.inet_aton(address)
        assert not address.startswith("127.")


@pytest.mark.parametrize("bind", ["0.0.0.0", "", "::"])
def test_wildcard_bind_uses_all_addresses(bind: str) -> None:
    assert advertised_addresses(bind, lambda: ["10.0.0.5", "192.168.1.20"]) == [
        "10.0.0.5",
        "192.168.1.20",
    ]


def test_specific_bind_uses_that_address() -> None:
    assert advertised_addresses("192.168.1.20", lambda: ["10.0.0.5"]) == ["192.168.1.20"]


def test_service_info() -> None:
    info = build_service_info(
        bridge_id="abc", hostname="roaster.lan", port=8765, addresses=["192.168.1.20"]
    )
    assert info.type == SERVICE_TYPE == "_artisan-bridge._tcp.local."
    assert info.name == "Artisan Bridge (roaster.lan)._artisan-bridge._tcp.local."
    assert info.port == 8765
    assert info.server == "roaster.local."
    assert info.parsed_addresses() == ["192.168.1.20"]
    assert info.decoded_properties == {
        "id": "abc",
        "version": __version__,
        "path": "/v1",
        "protocol": "1",
    }


@dataclass
class FakeZeroconf:
    fail: bool = False
    awaitable: bool = True
    registered: list[ServiceInfo] = field(default_factory=list)
    unregistered: list[ServiceInfo] = field(default_factory=list)
    closed: bool = False

    async def async_register_service(
        self, info: ServiceInfo, *, allow_name_change: bool = False
    ) -> object:
        assert allow_name_change
        if self.fail:
            raise OSError("no multicast")
        self.registered.append(info)
        return self._done() if self.awaitable else None

    async def async_unregister_service(self, info: ServiceInfo) -> object:
        self.unregistered.append(info)
        return self._done() if self.awaitable else None

    async def async_close(self) -> None:
        self.closed = True

    async def _done(self) -> None:
        return None


@pytest.mark.parametrize("awaitable", [True, False])
async def test_register_and_unregister(awaitable: bool) -> None:
    fake = FakeZeroconf(awaitable=awaitable)
    advertiser = Discovery(
        bridge_id="abc",
        hostname="roaster",
        bind="0.0.0.0",
        port=8765,
        zeroconf_factory=lambda: fake,
        addresses=lambda: ["192.168.1.20"],
    )
    await advertiser.start()
    assert [info.name for info in fake.registered] == [
        "Artisan Bridge (roaster)._artisan-bridge._tcp.local."
    ]
    await advertiser.stop()
    assert fake.unregistered == fake.registered
    assert fake.closed
    await advertiser.stop()  # idempotent


async def test_registration_failure_is_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeZeroconf(fail=True)
    advertiser = Discovery(
        bridge_id="abc",
        hostname="roaster",
        bind="192.168.1.20",
        port=8765,
        zeroconf_factory=lambda: fake,
    )
    with caplog.at_level(logging.ERROR):
        await advertiser.start()
    assert "registration failed" in caplog.text
    assert fake.closed
    await advertiser.stop()
    assert fake.unregistered == []


async def test_no_addresses_skips(caplog: pytest.LogCaptureFixture) -> None:
    created: list[FakeZeroconf] = []

    def factory() -> FakeZeroconf:
        created.append(FakeZeroconf())
        return created[-1]

    advertiser = Discovery(
        bridge_id="abc",
        hostname="roaster",
        bind="0.0.0.0",
        port=8765,
        zeroconf_factory=factory,
        addresses=list,
    )
    with caplog.at_level(logging.WARNING):
        await advertiser.start()
    assert "advertisement skipped" in caplog.text
    assert created == []
