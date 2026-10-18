"""Constants for the Artisan Roaster Scope integration."""

from typing import Final

DOMAIN: Final = "artisan"

DEFAULT_PORT: Final = 8765
WS_PATH: Final = "/v1"
PROTOCOL_VERSION: Final = 1

CONF_UNIT: Final = "unit"
CONF_HOSTNAME: Final = "hostname"

OPT_SAMPLE_INTERVAL: Final = "sample_interval_s"
OPT_ROR_PRECISION: Final = "ror_precision"
DEFAULT_SAMPLE_INTERVAL: Final = 1.0
DEFAULT_ROR_PRECISION: Final = 1
MIN_SAMPLE_INTERVAL: Final = 1.0
MAX_SAMPLE_INTERVAL: Final = 10.0
MAX_ROR_PRECISION: Final = 2

EVENT_ARTISAN: Final = "artisan_event"
EVENT_ROAST_SUMMARY: Final = "artisan_roast_summary"

# Ingest event names (spec 3.1), in roast order.
ROAST_EVENTS: Final[tuple[str, ...]] = (
    "on",
    "start",
    "charge",
    "dry",
    "fcs",
    "fce",
    "scs",
    "sce",
    "drop",
    "cool",
    "off",
    "reset",
)
EVENT_ALERT: Final = "alert"
EVENT_TYPES: Final[tuple[str, ...]] = (*ROAST_EVENTS, EVENT_ALERT)

PHASES: Final[tuple[str, ...]] = (
    "off",
    "monitoring",
    "recording",
    "drying",
    "maillard",
    "development",
    "cooling",
    "finished",
)
ROASTING_PHASES: Final = frozenset({"drying", "maillard", "development"})

ATTR_EVENT: Final = "event"
ATTR_ROAST_ID: Final = "roast_id"
ATTR_TITLE: Final = "title"
ATTR_TEXT: Final = "text"

MANUFACTURER: Final = "Artisan"
MODEL: Final = "Artisan HA Bridge"


def signal_update(entry_id: str) -> str:
    """Return the dispatcher signal for any state change of one entry.

    Listeners receive the message type, or None for a connection change.
    """
    return f"{DOMAIN}_update_{entry_id}"


def signal_event(entry_id: str) -> str:
    """Return the dispatcher signal for roast events and alerts of one entry."""
    return f"{DOMAIN}_event_{entry_id}"


def device_name(hostname: str) -> str:
    """Return the device name for a bridge host."""
    return f"Artisan ({hostname})"
