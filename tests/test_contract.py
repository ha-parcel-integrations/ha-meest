"""The parcel contract's checks, run over every normalised sample."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.meest.account.parcels import normalize_account_parcel
from custom_components.meest.const import ParcelStatus
from custom_components.meest.parcels import parse_iso
from custom_components.meest.tracking.parcels import normalize_tracking_parcel
from tests.payloads import CODE, account_record, row

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


def _tracking(*scans: tuple[str, str], history: bool = True) -> dict:
    rows = [row(code, when) for code, when in scans]
    return normalize_tracking_parcel({"rows": rows}, code=CODE, include_history=history)


def _account(events=None, history: bool = True, **overrides) -> dict:
    return normalize_account_parcel(
        account_record(**overrides),
        outgoing=False,
        events=events,
        include_history=history,
    )


def _collected_tracking() -> dict:
    return _tracking(
        ("606", "2026-10-02 10:00:00"),
        ("1200", "2026-10-03 09:00:00"),
        ("1724", "2026-10-04 16:00:00"),
    )


def _collected_account() -> dict:
    return _account(
        statusCode="1724",
        factDeliveryDate="2026-10-04T16:00:00",
        delivery={"branch": {"name": "Branch 12"}},
    )


SAMPLES = {
    "tracking_delivered": lambda: _tracking(("1622", "2026-10-04 11:00:00")),
    "tracking_active": lambda: _tracking(("606", "2026-10-02 10:00:00")),
    "tracking_at_point": lambda: _tracking(("1220", "2026-10-03 09:00:00")),
    "tracking_collected": _collected_tracking,
    "tracking_returning": lambda: _tracking(("707", "2026-10-03 09:00:00")),
    "tracking_problem": lambda: _tracking(("3", "2026-10-03 09:00:00")),
    "tracking_bare": lambda: normalize_tracking_parcel({}, code=CODE),
    "account_delivered": lambda: _account(
        statusCode="1622", factDeliveryDate="2026-10-04T11:00:00"
    ),
    "account_active": _account,
    "account_at_point": lambda: _account(statusCode="1220"),
    "account_collected": _collected_account,
    "account_returning": lambda: _account(statusCode="1521"),
    "account_problem": lambda: _account(statusCode="3"),
    "account_moment": lambda: _account(
        estimatedDeliveryDate=None, delivery={"timeFrom": "2026-10-05T13:00:00"}
    ),
    "account_date": lambda: _account(delivery={}),
    "account_bare": lambda: normalize_account_parcel({}, outgoing=False),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_every_timestamp_carries_an_offset(parcel):
    stamps = [parcel["delivered_at"], parcel["planned_from"], parcel["planned_to"]]
    stamps += [event["timestamp"] for event in parcel["history"] or []]
    for stamp in filter(None, stamps):
        assert datetime.fromisoformat(stamp).tzinfo is not None


def test_a_collected_tracking_parcel_stays_pickup():
    parcel = _collected_tracking()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["delivered_at"] == "2026-10-04T16:00:00+02:00"


def test_a_collected_account_parcel_stays_pickup_and_keeps_its_point():
    parcel = _collected_account()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Branch 12"
    assert parcel["delivered_at"] == "2026-10-04T16:00:00+02:00"


def test_an_account_parcel_without_a_branch_is_pickup_once_it_reached_a_point():
    events = [
        {"statusCode": "1220", "eventDateTime": "2026-10-03T09:00:00"},
        {"statusCode": "1724", "eventDateTime": "2026-10-04T16:00:00"},
    ]
    parcel = _account(
        events=events, statusCode="1724", delivery={}, estimatedDeliveryDate=None
    )
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_home_delivery_is_not_pickup():
    assert _tracking(("606", "2026-10-02 10:00:00"))["pickup"] is False
    assert _tracking(("1622", "2026-10-04 11:00:00"))["pickup"] is False


def test_a_returning_parcel_is_not_delivered_and_keeps_pickup():
    parcel = _tracking(("1200", "2026-10-03 09:00:00"), ("707", "2026-10-04 09:00:00"))
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["pickup"] is True


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = SAMPLES["account_moment"]()
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == start.date()


def test_a_date_covers_the_whole_day():
    parcel = SAMPLES["account_date"]()
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
