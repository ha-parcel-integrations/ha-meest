"""Tests for the shared coordinator: tiers, scheduling and the event contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.meest.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.meest.coordinator import (
    MeestCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
    bucket,
)
from custom_components.meest.events import fire_incoming_change_events
from custom_components.meest.tracking.coordinator import MeestTrackingCoordinator
from tests.payloads import CODE, row


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, tzinfo=UTC)


def test_quiet_window_bounds():
    assert _in_quiet_window(_local(0))
    assert _in_quiet_window(_local(5, 59))
    assert not _in_quiet_window(_local(6))
    assert not _in_quiet_window(_local(23, 59))


def test_next_anchor_is_six_then_midnight():
    assert _next_anchor(_local(3)) == _local(6)
    assert _next_anchor(_local(9)) == datetime(2026, 10, 6, 0, tzinfo=UTC)


def test_stagger_is_deterministic_and_bounded():
    assert _stagger_minutes("abc") == _stagger_minutes("abc")
    assert 0 <= _stagger_minutes("abc") < STAGGER_MINUTES


def test_out_for_delivery_without_a_window_is_hot():
    parcels = [{"status": "out_for_delivery", "planned_from": None}]
    assert _hottest_tier_minutes(parcels, _local(10)) == HOT_INTERVAL_MINUTES


def test_out_for_delivery_with_unparseable_window_is_hot():
    parcels = [{"status": "out_for_delivery", "planned_from": "nope"}]
    assert _hottest_tier_minutes(parcels, _local(10)) == HOT_INTERVAL_MINUTES


def test_out_for_delivery_hot_only_near_its_window():
    soon = (_local(10) + timedelta(minutes=30)).isoformat()
    later = (_local(10) + timedelta(hours=5)).isoformat()
    assert (
        _hottest_tier_minutes(
            [{"status": "out_for_delivery", "planned_from": soon}], _local(10)
        )
        == HOT_INTERVAL_MINUTES
    )
    assert (
        _hottest_tier_minutes(
            [{"status": "out_for_delivery", "planned_from": later}], _local(10)
        )
        == MID_INTERVAL_MINUTES
    )


def test_anything_else_in_flight_is_mid():
    assert (
        _hottest_tier_minutes([{"status": "in_transit"}], _local(10))
        == MID_INTERVAL_MINUTES
    )
    assert _hottest_tier_minutes([], _local(10)) == MID_INTERVAL_MINUTES


def test_no_tier_suspends_polling():
    assert _next_update_interval(_local(10), None, "e") is None


def test_inside_the_quiet_window_waits_for_the_anchor():
    assert _next_update_interval(_local(2), MID_INTERVAL_MINUTES, "e") == timedelta(
        hours=4
    )


def test_a_candidate_landing_in_the_window_is_clamped_to_the_anchor():
    now = _local(23, 50)
    assert (
        _next_update_interval(now, MID_INTERVAL_MINUTES, "e") == _next_anchor(now) - now
    )


def test_daytime_interval_is_tier_plus_stagger():
    now = _local(10)
    expected = timedelta(minutes=MID_INTERVAL_MINUTES + _stagger_minutes("e"))
    assert _next_update_interval(now, MID_INTERVAL_MINUTES, "e") == expected


def test_bucket_tolerates_no_data():
    assert bucket(None, "incoming_active") == []
    assert bucket({}, "incoming_active") == []


async def test_base_poll_is_abstract(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    with pytest.raises(NotImplementedError):
        await MeestCoordinator(hass, entry, DOMAIN)._async_poll()


# --- events -----------------------------------------------------------------


def _coordinator(hass, parcels):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    return MeestTrackingCoordinator(hass, client, entry), client


def _scan(code: str, status_code: str) -> dict:
    return {"rows": [row(status_code, "2026-10-05 10:00:00", own=code)]}


async def test_first_refresh_is_silent_then_events_follow(hass):
    coordinator, client = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}])
    registered = async_capture_events(hass, f"{DOMAIN}_parcel_registered")
    changed = async_capture_events(hass, f"{DOMAIN}_parcel_status_changed")
    delivered = async_capture_events(hass, f"{DOMAIN}_parcel_delivered")

    client.async_get_parcel.return_value = _scan(CODE, "606")
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert not registered and not changed and not delivered

    client.async_get_parcel.return_value = _scan(CODE, "1315")
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert len(changed) == 1
    assert changed[0].data["old_status"] == "in_transit"
    assert changed[0].data["new_status"] == "out_for_delivery"
    assert changed[0].data["barcode"] == CODE

    client.async_get_parcel.return_value = _scan(CODE, "1622")
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    # the hop to delivered fires only the dedicated event
    assert len(delivered) == 1 and len(changed) == 1


async def test_new_code_after_first_refresh_fires_registered(hass):
    coordinator, client = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}])
    registered = async_capture_events(hass, f"{DOMAIN}_parcel_registered")
    client.async_get_parcel.side_effect = lambda code: _scan(code, "606")
    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(
        coordinator.config_entry,
        options={
            **coordinator.config_entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: CODE},
                {CONF_TRACKING_CODE: "NEW000001"},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert [e.data["barcode"] for e in registered] == ["NEW000001"]


async def test_outgoing_parcels_fire_only_the_outgoing_pair(hass):
    coordinator, client = _coordinator(
        hass, [{CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "outgoing"}]
    )
    incoming_events = [
        async_capture_events(hass, f"{DOMAIN}_{name}")
        for name in ("parcel_registered", "parcel_status_changed", "parcel_delivered")
    ]
    changed = async_capture_events(hass, f"{DOMAIN}_outgoing_parcel_status_changed")
    delivered = async_capture_events(hass, f"{DOMAIN}_outgoing_parcel_delivered")

    client.async_get_parcel.return_value = _scan(CODE, "606")
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _scan(CODE, "1315")
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _scan(CODE, "1622")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(changed) == 1 and len(delivered) == 1
    assert not any(incoming_events)


async def test_delivery_time_change_event(hass):
    coordinator, client = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}])
    times = async_capture_events(hass, f"{DOMAIN}_parcel_delivery_time_changed")
    client.async_get_parcel.return_value = _scan(CODE, "1315")
    await coordinator._async_update_data()

    # the tracking lookup never has a window, so feed events through the helper
    parcel = {
        "barcode": CODE,
        "status": ParcelStatus.OUT_FOR_DELIVERY,
        "planned_from": "2026-10-05T10:00:00",
        "planned_to": "2026-10-05T12:00:00",
    }
    fire_incoming_change_events(
        hass,
        [parcel],
        {CODE: ParcelStatus.OUT_FOR_DELIVERY},
        {CODE: (None, None)},
        "dev",
    )
    await hass.async_block_till_done()
    assert len(times) == 1
    assert times[0].data["new_planned_from"] == "2026-10-05T10:00:00"
    assert times[0].data["device_id"] == "dev"


async def test_a_failed_poll_recomputes_the_interval(hass):
    coordinator, client = _coordinator(hass, [{CONF_TRACKING_CODE: CODE}])
    client.async_get_parcel.side_effect = RuntimeError("boom")
    coordinator.update_interval = timedelta(hours=9)
    with pytest.raises(RuntimeError):
        await coordinator._async_update_data()
    assert coordinator.update_interval != timedelta(hours=9)
    assert coordinator.update_interval is not None


async def test_device_id_is_resolved_and_cached(hass):
    coordinator, _ = _coordinator(hass, [])
    assert coordinator._device_id() is None
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "x")},
    )
    assert coordinator._device_id() == device.id
    assert coordinator._device_id() == device.id
