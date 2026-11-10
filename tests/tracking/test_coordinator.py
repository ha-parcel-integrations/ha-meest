"""Tests for the tracking coordinator: fetching, caching, direction and events."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DIRECTION_OUTGOING,
    DOMAIN,
    ParcelStatus,
)
from custom_components.meest.tracking.api import MeestApiError
from custom_components.meest.tracking.coordinator import (
    MeestTrackingCoordinator,
    tracked_direction,
)
from tests.payloads import CODE, OTHER_CODE, journey, row


def _entry(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        options={
            CONF_PARCELS: parcels,
            # keep-most-recent so the fixed old sample dates never get trimmed
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _in_transit(code: str = CODE) -> dict:
    return {"rows": [row("606", "2026-10-02 10:00:00", own=code)]}


def _delivered(code: str = CODE) -> dict:
    return {"rows": [row("1622", "2026-10-05 11:00:00", own=code)]}


def _coordinator(hass, parcels, client):
    entry = _entry(parcels)
    entry.add_to_hass(hass)
    return MeestTrackingCoordinator(hass, client, entry), entry


def test_tracked_direction_defaults_to_incoming():
    assert tracked_direction({CONF_TRACKING_CODE: "X"}) == "incoming"
    assert tracked_direction({CONF_DIRECTION: DIRECTION_OUTGOING}) == "outgoing"


async def test_buckets_split_by_declared_direction_and_delivery(hass):
    client = AsyncMock()
    results = {
        CODE: _in_transit(),
        OTHER_CODE: _delivered(OTHER_CODE),
        "OUT000001": _in_transit("OUT000001"),
        "OUT000002": _delivered("OUT000002"),
    }
    client.async_get_parcel.side_effect = lambda code: results[code]
    coordinator, _ = _coordinator(
        hass,
        [
            {CONF_TRACKING_CODE: CODE},
            {CONF_TRACKING_CODE: OTHER_CODE, CONF_DIRECTION: "incoming"},
            {CONF_TRACKING_CODE: "OUT000001", CONF_DIRECTION: "outgoing"},
            {CONF_TRACKING_CODE: "OUT000002", CONF_DIRECTION: "outgoing"},
        ],
        client,
    )

    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data["incoming_active"]] == [CODE]
    assert [p["barcode"] for p in data["incoming_delivered"]] == [OTHER_CODE]
    assert [p["barcode"] for p in data["outgoing_active"]] == ["OUT000001"]
    assert [p["barcode"] for p in data["outgoing_delivered"]] == ["OUT000002"]
    assert coordinator.last_success_time is not None
    assert coordinator.delivered_codes == {OTHER_CODE, "OUT000002"}


async def test_delivered_codes_are_not_fetched_again(hass):
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered()
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)

    await coordinator._async_update_data()
    data = await coordinator._async_update_data()

    assert client.async_get_parcel.await_count == 1
    assert [p["barcode"] for p in data["incoming_delivered"]] == [CODE]
    # everything delivered: nothing left to poll
    assert coordinator.update_interval is None


async def test_removed_codes_leave_the_cache_and_skip_set(hass):
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered()
    coordinator, entry = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: []}
    )
    data = await coordinator._async_update_data()

    assert data["incoming_delivered"] == []
    assert coordinator.delivered_codes == set()
    assert coordinator._raw_cache == {}
    assert coordinator.update_interval is None  # nothing tracked: polling stops


async def test_not_found_shows_a_pending_placeholder_then_keeps_last_good(hass):
    client = AsyncMock()
    client.async_get_parcel.return_value = None
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)

    data = await coordinator._async_update_data()
    assert data["incoming_active"][0]["status"] is ParcelStatus.UNKNOWN

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = None
    data = await coordinator._async_update_data()
    assert data["incoming_active"][0]["status"] is ParcelStatus.IN_TRANSIT


async def test_a_failed_lookup_keeps_last_good_data(hass):
    client = AsyncMock()
    client.async_get_parcel.return_value = _in_transit()
    coordinator, _ = _coordinator(
        hass, [{CONF_TRACKING_CODE: CODE}, {CONF_TRACKING_CODE: OTHER_CODE}], client
    )
    await coordinator._async_update_data()

    client.async_get_parcel.side_effect = [
        MeestApiError("scans belong to another parcel"),
        _in_transit(OTHER_CODE),
    ]
    data = await coordinator._async_update_data()

    assert {p["barcode"] for p in data["incoming_active"]} == {CODE, OTHER_CODE}
    assert data["incoming_active"][0]["status"] is ParcelStatus.IN_TRANSIT


async def test_all_lookups_failing_with_no_data_raises_update_failed(hass):
    client = AsyncMock()
    client.async_get_parcel.side_effect = aiohttp.ClientConnectionError("down")
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    # a failure never leaves the interval None
    assert coordinator.update_interval is not None


async def test_rate_limit_backs_off_with_retry_after(hass):
    client = AsyncMock()
    client.async_get_parcel.side_effect = MeestApiError(
        "HTTP 429", status_code=429, retry_after=120.0
    )
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 120.0


async def test_rate_limit_without_header_uses_exponential_backoff(hass):
    client = AsyncMock()
    client.async_get_parcel.side_effect = MeestApiError("HTTP 429", status_code=429)
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)

    with pytest.raises(UpdateFailed) as first:
        await coordinator._async_update_data()
    with pytest.raises(UpdateFailed) as second:
        await coordinator._async_update_data()
    assert second.value.retry_after > first.value.retry_after


async def test_unexpected_exceptions_are_not_swallowed(hass):
    client = AsyncMock()
    client.async_get_parcel.side_effect = RuntimeError("bug")
    coordinator, _ = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)
    with pytest.raises(RuntimeError):
        await coordinator._async_update_data()


async def test_history_option_is_passed_through(hass):
    client = AsyncMock()
    client.async_get_parcel.return_value = {"rows": journey()}
    coordinator, entry = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}], client)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "include_history": True}
    )
    data = await coordinator._async_update_data()
    assert len(data["incoming_delivered"][0]["history"]) == 4
