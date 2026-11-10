"""Services for the Meest parcel tracker integration.

``meest.track_parcel`` / ``meest.untrack_parcel`` add or remove a tracked code
without opening the options, so a dashboard button can start tracking a parcel.
Validation is format-only: a service call must not wait on a network lookup.
Only the tracking entry has a code list, so an account entry never sees them.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import normalize_tracking_code, valid_tracking_code
from .const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DEFAULT_DIRECTION,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    SOURCE_TRACKING,
)
from .tracking.coordinator import tracked_direction

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_DIRECTION, default=DEFAULT_DIRECTION): vol.In(
            [DIRECTION_INCOMING, DIRECTION_OUTGOING]
        ),
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})


def _resolve_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the tracking entry, or raise when it is not set up."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING:
            return entry
    raise ServiceValidationError("Meest tracking is not set up")


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Meest services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(code):
            raise ServiceValidationError(f"'{code}' is not a valid Meest parcel number")
        entry = _resolve_entry(hass)
        direction = call.data[CONF_DIRECTION]
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        existing = next((p for p in parcels if p[CONF_TRACKING_CODE] == code), None)
        if existing is not None:
            # Calling again with the other direction is how a parcel filed the
            # wrong way is corrected; the same direction stays a no-op.
            if tracked_direction(existing) == direction:
                return
            existing[CONF_DIRECTION] = direction
        else:
            parcels.append({CONF_TRACKING_CODE: code, CONF_DIRECTION: direction})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Meest services."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
