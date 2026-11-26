"""Tests for the track/untrack services."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.meest.services import async_setup_services, async_unload_services
from tests.payloads import CODE, OTHER_CODE


async def _setup(hass, parcels=()):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: list(parcels)},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel",
        new=AsyncMock(return_value=None),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service, **data):
    with patch(
        "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel",
        new=AsyncMock(return_value=None),
    ):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()


async def test_track_normalises_and_defaults_to_incoming(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", tracking_code=" cv00 0000001us ")
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "incoming"}
    ]


async def test_track_outgoing_and_the_same_call_twice_is_a_noop(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", tracking_code=CODE, direction="outgoing")
    await _call(hass, "track_parcel", tracking_code=CODE, direction="outgoing")
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "outgoing"}
    ]


async def test_tracking_again_with_the_other_direction_moves_the_code(hass):
    entry = await _setup(hass, [{CONF_TRACKING_CODE: CODE}])
    await _call(hass, "track_parcel", tracking_code=CODE, direction="outgoing")
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "outgoing"}
    ]


async def test_a_blank_code_is_refused(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "track_parcel", tracking_code="   ")


async def test_untrack_removes_only_that_code(hass):
    entry = await _setup(
        hass, [{CONF_TRACKING_CODE: CODE}, {CONF_TRACKING_CODE: OTHER_CODE}]
    )
    await _call(hass, "untrack_parcel", tracking_code=CODE.lower())
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: OTHER_CODE}]
    await _call(hass, "untrack_parcel", tracking_code="UNKNOWN99")
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: OTHER_CODE}]


async def test_services_need_a_tracking_entry(hass):
    async_setup_services(hass)
    async_setup_services(hass)  # idempotent
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {"tracking_code": CODE}, blocking=True
        )
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": CODE}, blocking=True
        )
    async_unload_services(hass, hubs_left=False, entries_left=False)
    async_unload_services(hass, hubs_left=False, entries_left=False)
    assert not hass.services.has_service(DOMAIN, "track_parcel")
