"""Diagnostics support for the Meest parcel tracker integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MeestConfigEntry
from .coordinator import bucket

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# "raw_status" is a deliberate over-redaction: the canonical top-level field
# and each history entry's own text share the key name, and a history line can
# name the town of a scan, which locates the recipient.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "url",
    "raw_status",
    "pickup_point",
    # tracking rows: every number that names the parcel, and every place
    "ShipmentNumber",
    "ShipmentNumberSender",
    "ShipmentNumberTransit",
    "Barcode",
    "ShipmentIdRef",
    "DocumentIdRef",
    "AgentsIdRef",
    "City",
    "City_EN",
    "City_RU",
    "Country",
    "Country_EN",
    "Country_RU",
    "CountryDel",
    "Recipient_Country",
    "DetailMessages",
    "DetailMessages_EN",
    "DetailMessages_RU",
    # account entry data
    "login",
    "token",
    "refresh_token",
    "refreshToken",
    "globalUserId",
    # account parcel records
    "parcelNumber",
    "barCode",
    "parcelID",
    "contentName",
    "contentsItems",
    "COD",
    "insurance",
    "branch",
    "address",
    "city",
    "country",
    "messages",
    "messageDetails",
    "senderCountry",
    "receiverCountry",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MeestConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Meest config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    names = (
        "incoming_active",
        "incoming_delivered",
        "outgoing_active",
        "outgoing_delivered",
    )
    published = {
        "incoming_active": "incoming",
        "incoming_delivered": "delivered",
        "outgoing_active": "outgoing",
        "outgoing_delivered": "delivered_outgoing",
    }
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(bucket(data, "incoming_active")),
            "delivered": len(bucket(data, "incoming_delivered")),
            "outgoing_active": len(bucket(data, "outgoing_active")),
            "outgoing_delivered": len(bucket(data, "outgoing_delivered")),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        **{
            published[name]: async_redact_data(bucket(data, name), TO_REDACT)
            for name in names
        },
    }
