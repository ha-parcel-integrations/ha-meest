"""Meest parcel tracker custom component for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import MeestAccountClient
from .account.coordinator import MeestAccountCoordinator
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TOKEN,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .coordinator import MeestCoordinator
from .services import async_setup_services, async_unload_services
from .tracking.api import MeestTrackingClient
from .tracking.coordinator import MeestTrackingCoordinator


@dataclass
class MeestData:
    """Runtime data attached to a Meest config entry."""

    client: MeestTrackingClient | MeestAccountClient
    coordinator: MeestCoordinator
    # Snapshot of the options the coordinator last acted on, so that other
    # writes to the entry (a rotated token) do not turn into a poll.
    options: dict[str, Any]


type MeestConfigEntry = ConfigEntry[MeestData]


def _is_tracking(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING


async def async_setup_entry(hass: HomeAssistant, entry: MeestConfigEntry) -> bool:
    """Set up Meest from a config entry."""
    client: MeestTrackingClient | MeestAccountClient
    coordinator: MeestCoordinator
    if entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT:

        async def async_store_tokens(tokens: dict[str, str]) -> None:
            # The pair rotates; losing it would force a sign-in after a restart.
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_TOKEN: tokens[CONF_TOKEN],
                    CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
                },
            )

        client = MeestAccountClient(
            async_get_clientsession(hass),
            token=entry.data.get(CONF_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            token_callback=async_store_tokens,
        )
        coordinator = MeestAccountCoordinator(hass, client, entry)
    else:
        client = MeestTrackingClient(async_get_clientsession(hass))
        coordinator = MeestTrackingCoordinator(hass, client, entry)

    # First refresh here, before forwarding: a transient failure then fails the
    # whole entry so HA retries it, instead of half-setting-up a platform.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MeestData(
        client=client, coordinator=coordinator, options=dict(entry.options)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Option changes (added/removed parcels, history) apply live via a refresh
    # rather than a reload, so per-parcel sensors appear and disappear at once.
    # It is also the resume path after polling suspended with nothing to track.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if _is_tracking(entry):
        async_setup_services(hass)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MeestConfigEntry) -> None:
    """Apply changed options by refreshing the coordinator."""
    data = entry.runtime_data
    if dict(entry.options) == data.options:
        return
    data.options = dict(entry.options)
    await data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: MeestConfigEntry) -> bool:
    """Unload a Meest config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services belong to the tracking entry, so they go away with it
    # rather than with the last entry of any kind.
    others_loaded = any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and _is_tracking(other)
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if _is_tracking(entry) and not others_loaded:
        async_unload_services(hass)
    return True
