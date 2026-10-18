"""Input-layer interfaces (spec section 8).

Telemetry and event inputs talk to the state machine only through these protocols, so a future
Artisan `/telemetry` source can replace WebLCDs + ingest without touching `state`.
"""

from __future__ import annotations

from typing import Protocol

from .protocol import EventName


class TelemetrySink(Protocol):
    @property
    def monitoring(self) -> bool: ...

    @property
    def artisan_online(self) -> bool: ...

    def on_sample(self, bt: float | None, et: float | None, tm: float | None, now: float) -> None:
        """`now` is a monotonic timestamp in seconds."""

    def on_alert(self, text: str, title: str | None) -> None: ...

    def set_artisan_online(self, online: bool) -> None: ...


class EventSink(Protocol):
    def on_event(
        self, event: EventName, bt: float | None, et: float | None, tm: float | None
    ) -> None: ...

    def on_control(self, channel: str, value: float | None) -> None: ...


class TelemetrySource(Protocol):
    """Pushes samples, alerts and liveness into a `TelemetrySink`."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class EventSource(Protocol):
    """Pushes roast events and control changes into an `EventSink`."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
