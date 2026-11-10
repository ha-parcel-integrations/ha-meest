"""Tests for the account normaliser."""

from __future__ import annotations

import logging

import pytest

from custom_components.meest.account.parcels import (
    check_direction,
    normalize_account_parcel,
)
from custom_components.meest.const import ParcelStatus
from tests.payloads import account_events, account_record, account_sent_record


def _in(record, **kw):
    return normalize_account_parcel(record, outgoing=False, **kw)


def test_incoming_record_maps_the_canonical_fields():
    parcel = _in(account_record())

    assert parcel["barcode"] == "CV000000001US"
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "Handed to the courier"
    assert parcel["planned_from"] == "2026-10-05T10:00:00"
    assert parcel["planned_to"] == "2026-10-05T12:00:00"
    assert parcel["pickup_point"] == "Branch 12"
    assert parcel["sender"] == "Sender Name"
    assert parcel["receiver"] == "Rec Eiver"
    assert parcel["raw"]["barCode"] == "CR000000002US"
    assert parcel["url"].endswith("/n/CV000000001US")


def test_weight_and_dimensions_stay_none_until_units_are_confirmed():
    parcel = _in(account_record(parcelDimensions=[{"length": 10}], placesWeight=2))
    assert parcel["weight"] is None and parcel["dimensions"] is None


def test_outgoing_reads_the_sending_block_and_has_no_pickup_point():
    parcel = normalize_account_parcel(account_sent_record(), outgoing=True)
    assert parcel["planned_from"] == "2026-10-04T14:00:00"
    assert parcel["planned_to"] == "2026-10-04T16:00:00"
    assert parcel["pickup_point"] is None
    assert parcel["status"] is ParcelStatus.IN_TRANSIT


def test_outgoing_without_a_window_gets_no_estimated_date():
    record = account_sent_record(sending={}, estimatedDeliveryDate="2026-10-09")
    parcel = normalize_account_parcel(record, outgoing=True)
    assert parcel["planned_from"] is None


def test_estimated_date_alone_fills_planned_from_for_incoming():
    parcel = _in(account_record(delivery={"branch": "B"}))
    assert parcel["planned_from"] == "2026-10-05"
    assert parcel["planned_to"] is None
    assert parcel["pickup_point"] == "B"


def test_full_timestamps_pass_through_and_equal_bounds_collapse():
    block = {"timeFrom": "2026-10-05T10:00:00", "timeTo": "2026-10-05T10:00:00"}
    parcel = _in(account_record(delivery=block))
    assert parcel["planned_from"] == "2026-10-05T10:00:00"
    assert parcel["planned_to"] is None


def test_time_of_day_with_seconds_and_unusable_bounds():
    parcel = _in(account_record(delivery={"timeFrom": "9:05:30", "timeTo": "late"}))
    assert parcel["planned_from"] == "2026-10-05T09:05:30"
    assert parcel["planned_to"] is None
    parcel = _in(
        account_record(estimatedDeliveryDate=None, delivery={"timeFrom": "10:00"})
    )
    assert parcel["planned_from"] is None


def test_delivered_uses_fact_date_and_drops_the_window():
    parcel = _in(
        account_record(statusCode="1622", factDeliveryDate="2026-10-05T11:30:00")
    )
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-05T11:30:00"
    assert parcel["planned_from"] is None


def test_branch_shapes():
    assert (
        _in(account_record(delivery={"branch": {"title": "T"}}))["pickup_point"] == "T"
    )
    assert _in(account_record(delivery={"branch": {"x": 1}}))["pickup_point"] is None
    assert _in(account_record(delivery={"branch": "  "}))["pickup_point"] is None
    assert _in(account_record(delivery=None))["pickup_point"] is None


def test_parties_as_strings_or_nameless():
    parcel = _in(account_record(sender="Plain Name", receiver={"x": 1}))
    assert parcel["sender"] == "Plain Name"
    assert parcel["receiver"] is None
    assert _in(account_record(sender=7))["sender"] is None


def test_barcode_falls_back_through_the_identifiers():
    assert _in(account_record(parcelNumber=None))["barcode"] == "CR000000002US"
    assert _in(account_record(parcelNumber=None, barCode=None))["barcode"] == "P-1001"


def test_unmapped_status_code_warns_then_falls_back_to_the_coarse_ordinal(caplog):
    record = account_record(statusCode="9999", appCode=2)
    with caplog.at_level(logging.WARNING):
        parcel = _in(record)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert any("status=9999" in r.getMessage() for r in caplog.records)


def test_missing_status_code_uses_the_ordinal_without_warning(caplog):
    record = account_record(statusCode=None, appCode=1)
    with caplog.at_level(logging.WARNING):
        parcel = _in(record)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Handed to the courier"
    assert not [r for r in caplog.records if "status=" in r.getMessage()]


def test_an_out_of_range_ordinal_is_not_silently_created(caplog):
    record = account_record(statusCode=None, appCode=42)
    with caplog.at_level(logging.WARNING):
        parcel = _in(record)
    assert parcel["status"] is ParcelStatus.UNKNOWN


def test_falls_back_to_the_newest_mappable_event_then_unknown():
    record = account_record(statusCode=None)
    events = [
        *account_events(),
        {"appCode": 77, "eventDateTime": "2026-10-03T10:00:00"},
    ]
    assert _in(record, events=events)["status"] is ParcelStatus.IN_TRANSIT
    assert _in(record)["status"] is ParcelStatus.UNKNOWN


def test_event_fine_code_wins_over_its_ordinal_and_corrections_are_skipped():
    events = [
        {"appCode": 0, "statusCode": "1220", "eventDateTime": "2026-10-03T10:00:00"},
        {"appCode": 0, "statusCode": "2533", "eventDateTime": "2026-10-04T10:00:00"},
    ]
    parcel = _in(account_record(statusCode=None), events=events)
    # the correction's ordinal 0 would read "created"; the older pickup wins
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    events[1]["appCode"] = None
    assert (
        _in(account_record(statusCode=None), events=events)["status"]
        is ParcelStatus.AT_PICKUP_POINT
    )


def test_history_is_opt_in_ordered_and_capped():
    assert _in(account_record(), events=account_events())["history"] is None
    history = _in(account_record(), events=account_events(), include_history=True)[
        "history"
    ]
    assert [h["timestamp"] for h in history] == [
        "2026-10-01T09:00:00",
        "2026-10-02T10:00:00",
    ]
    assert history[0]["raw_status"] == "Created (Testville, UA)"
    assert history[0]["status"] is ParcelStatus.REGISTERED
    assert history[1]["raw_status"] == "Sent"
    many = [
        {"appCode": 1, "eventDateTime": f"2026-10-{d:02d}T10:00:00"}
        for d in range(1, 26)
    ]
    capped = _in(account_record(), events=many, include_history=True)["history"]
    assert len(capped) == 20 and capped[0]["raw_status"] is None


def test_history_skips_undated_events_and_corrections():
    events = [
        {"appCode": 1},
        {"statusCode": "2533", "eventDateTime": "2026-10-04T10:00:00"},
    ]
    history = _in(account_record(), events=events, include_history=True)["history"]
    assert history == [
        {"timestamp": "2026-10-04T10:00:00", "status": None, "raw_status": None}
    ]


def test_first_record_structure_is_logged_once_without_values(caplog):
    with caplog.at_level(logging.WARNING):
        _in(account_record())
        _in(account_record())
    notes = [
        r.getMessage() for r in caplog.records if "not been confirmed" in r.getMessage()
    ]
    assert len(notes) == 1
    assert "parcelNumber" in notes[0] and "timeFrom" in notes[0]
    assert "CV000000001US" not in notes[0] and "Sender Name" not in notes[0]
    assert "issues/new?template=unrecognised_status.yml" in notes[0]


def test_direction_disagreement_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        check_direction({"direction": 1}, "receiver")
        check_direction({"direction": 2}, "sender")
        assert not caplog.records
        check_direction({"direction": 1}, "sender")
        check_direction({"direction": 1}, "sender")
    assert len([r for r in caplog.records if "disagree" in r.getMessage()]) == 1


@pytest.mark.parametrize("value", [None, True])
def test_direction_ignores_missing_or_bool(value, caplog):
    check_direction({"direction": value}, "receiver")
    check_direction({"direction": value}, "sender")
    assert not caplog.records


def test_a_backfilled_registration_on_the_record_loses_to_the_history():
    """The rule must hold on the record's own code, not only on events."""
    events = [
        {"statusCode": "1220", "eventDateTime": "2026-10-03T10:00:00"},
    ]
    parcel = _in(account_record(statusCode="100", appCode=None), events=events)
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT


def test_a_record_only_registered_stays_registered():
    parcel = _in(account_record(statusCode="100", appCode=None), events=[])
    assert parcel["status"] is ParcelStatus.REGISTERED
