"""Tests for the shared parcel helpers."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.const import (
    CAPABILITIES_BY_VARIANT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES_BY_VARIANT,
    ParcelStatus,
)
from custom_components.meest.parcels import (
    apply_delivered_filter,
    build_parcel,
    format_dimensions,
    is_pickup,
    parse_iso,
    planned_window,
    sort_parcels_by_ts,
    tracking_url,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_build_parcel_publishes_exactly_the_canonical_keys_in_order():
    parcel = build_parcel(barcode="X1", status=ParcelStatus.IN_TRANSIT)
    assert list(parcel) == CANONICAL_KEYS
    assert parcel["carrier"] == "Meest"


def test_delivered_clears_the_window_and_keeps_delivered_at():
    parcel = build_parcel(
        barcode="X1",
        status=ParcelStatus.DELIVERED,
        delivered_at="2026-10-05T11:00:00",
        planned_from="2026-10-05T09:00:00",
        planned_to="2026-10-05T10:00:00",
    )
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-05T11:00:00"
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


def test_undelivered_drops_delivered_at_and_an_arrived_parcel_has_no_window():
    parcel = build_parcel(
        barcode="X1",
        status=ParcelStatus.AT_PICKUP_POINT,
        delivered_at="2026-10-05T11:00:00",
        planned_from="2026-10-05T09:00:00",
        pickup=True,
        pickup_point="Branch 1",
    )
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] is None
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Branch 1"


def test_the_pickup_point_needs_pickup():
    parcel = build_parcel(
        barcode="X1", status=ParcelStatus.IN_TRANSIT, pickup_point="B"
    )
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_empty_parties_become_none():
    parcel = build_parcel(
        barcode="X1", status=ParcelStatus.UNKNOWN, sender="", receiver=""
    )
    assert parcel["sender"] is None and parcel["receiver"] is None


def test_tracking_url():
    assert tracking_url("AB123") == "https://t.meest-group.com/n/AB123"
    assert tracking_url(None) is None


def test_parse_iso():
    assert parse_iso("2026-10-05T10:00:00").tzinfo is UTC
    assert parse_iso("2026-10-05").year == 2026
    assert parse_iso("nonsense") is None
    assert parse_iso(None) is None


def test_format_dimensions():
    assert format_dimensions(10.4, 20, 30)["text"] == "10 x 20 x 30 cm"
    assert format_dimensions(None, 1, 1) is None


def test_sort_puts_unparseable_last_and_honours_direction():
    parcels = [
        {"id": "late", "planned_from": "2026-10-06T10:00:00"},
        {"id": "none", "planned_from": None},
        {"id": "early", "planned_from": "2026-10-05T10:00:00"},
    ]
    assert [p["id"] for p in sort_parcels_by_ts(parcels, "planned_from")] == [
        "early",
        "late",
        "none",
    ]
    assert [
        p["id"] for p in sort_parcels_by_ts(parcels, "planned_from", descending=True)
    ] == ["late", "early", "none"]


def _entry(**options):
    return MockConfigEntry(domain=DOMAIN, options=options)


def test_delivered_filter_by_days_keeps_recent_and_unparseable():
    now = datetime.now(UTC)
    parcels = [
        {"delivered_at": now.isoformat()},
        {"delivered_at": "garbage"},
        {"delivered_at": (now - timedelta(days=30)).isoformat()},
    ]
    kept = apply_delivered_filter(parcels, _entry())
    assert len(kept) == 2


def test_delivered_filter_by_count():
    parcels = [{"delivered_at": str(i)} for i in range(5)]
    entry = _entry(
        **{CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 2}
    )
    assert apply_delivered_filter(parcels, entry) == parcels[:2]


def test_capabilities_are_consistent():
    for variant, caps in CAPABILITIES_BY_VARIANT.items():
        assert caps <= KNOWN_CAPABILITIES
        assert not caps & PENDING_CAPABILITIES_BY_VARIANT.get(variant, frozenset())
    # the tracking lookup has no ETA, weight, dimensions or pickup point
    assert CAPABILITIES_BY_VARIANT["Tracking"] == {"url", "history"}
    # nothing the account source might add is claimed before a real account shows it
    assert CAPABILITIES_BY_VARIANT["Account"] == {"url", "history"}
    assert PENDING_CAPABILITIES_BY_VARIANT["Account"] == {
        "weight",
        "dimensions",
        "delivery_window",
        "pickup_point",
    }


@pytest.mark.parametrize("key", CANONICAL_KEYS)
def test_key_is_present(key):
    assert key in build_parcel(barcode=None, status=ParcelStatus.UNKNOWN)


def test_planned_window_shapes():
    zone = ZoneInfo("Europe/Kyiv")
    assert planned_window(date(2099, 1, 5), None, zone=zone) == (
        "2099-01-05T00:00:00+02:00",
        "2099-01-05T23:59:59+02:00",
    )
    assert planned_window("2099-01-05", "2099-01-07", zone=zone) == (
        "2099-01-05T00:00:00+02:00",
        "2099-01-07T23:59:59+02:00",
    )
    assert planned_window(
        datetime(2099, 1, 5, 10, 0, tzinfo=UTC), "2099-01-05T08:00:00Z", zone=zone
    ) == ("2099-01-05T08:00:00Z", "2099-01-05T10:00:00+00:00")
    assert planned_window("junk", "2099-01-05T08:00:00", zone=zone) == (
        "2099-01-05T08:00:00+02:00",
        "2099-01-05T23:59:59+02:00",
    )
    assert planned_window("junk", 5, zone=zone) == (None, None)
    assert planned_window(None, None, zone=zone) == (None, None)
    assert planned_window("2099-01-05T23:59:59", None, zone=zone) == (
        "2099-01-05T23:59:59+02:00",
        None,
    )


def test_is_pickup_route_flag_status_and_events():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(None, ParcelStatus.IN_TRANSIT, [None]) is False
