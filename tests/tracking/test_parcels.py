"""Tests for the tracking normaliser."""

from __future__ import annotations

import logging

import pytest

from custom_components.meest.const import ParcelStatus
from custom_components.meest.tracking.parcels import (
    canonical_code,
    clock_to_iso,
    match_rows,
    normalize_tracking_parcel,
)
from tests.payloads import (
    CODE,
    LATE_LABEL,
    OTHER_CODE,
    backfilled_registration,
    journey,
    row,
)


def _normalize(rows, **kwargs):
    return normalize_tracking_parcel({"rows": rows}, code=CODE, **kwargs)


def test_journey_is_delivered_with_the_entered_code_as_barcode():
    parcel = _normalize(journey())

    assert parcel["barcode"] == CODE  # never the label number on the late rows
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered_at"] == "2026-10-05T11:37:53+02:00"
    assert parcel["planned_from"] is None
    assert parcel["url"].endswith(f"/n/{CODE}")
    assert parcel["raw"]["rows"][-1]["Barcode"] == LATE_LABEL


def test_unavailable_fields_stay_none():
    parcel = _normalize(journey())
    for key in (
        "sender",
        "receiver",
        "pickup_point",
        "weight",
        "dimensions",
        "history",
    ):
        assert parcel[key] is None


def test_status_is_newest_row_even_when_the_reply_is_unordered():
    rows = list(reversed(journey()))
    assert _normalize(rows)["status"] is ParcelStatus.DELIVERED


def test_equal_timestamps_keep_reply_order():
    rows = [
        row("606", "2026-10-02 10:00:00"),
        row("1315", "2026-10-02 10:00:00"),
    ]
    assert _normalize(rows)["status"] is ParcelStatus.OUT_FOR_DELIVERY


def test_out_for_delivery_and_pickup_point():
    rows = [row("606", "2026-10-02 10:00:00"), row("1315", "2026-10-03 08:00:00")]
    assert _normalize(rows)["status"] is ParcelStatus.OUT_FOR_DELIVERY
    rows = [row("606", "2026-10-02 10:00:00"), row("1220", "2026-10-03 08:00:00")]
    parcel = _normalize(rows)
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["delivered_at"] is None


def test_unmapped_newest_code_falls_back_to_newest_mappable_and_warns(caplog):
    rows = [
        row("606", "2026-10-02 10:00:00", wording="Accepted"),
        row("9999", "2026-10-03 08:00:00", wording="Something new"),
    ]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(rows)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Accepted"
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "status=9999" in warnings[0]


def test_only_unmapped_codes_give_unknown_with_the_newest_wording(caplog):
    rows = [
        row("9998", "2026-10-02 10:00:00"),
        row("9999", "2026-10-03 08:00:00", wording="Odd"),
    ]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(rows)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "Odd"
    assert len(caplog.records) == 2


def test_unpublished_scan_324_is_mapped_not_unknown(caplog):
    rows = [
        row("11", "2026-10-02 10:00:00"),
        row("324", "2026-10-03 08:00:00", wording="In the queue"),
    ]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(rows)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "In the queue"
    assert not caplog.records


def test_correction_code_never_moves_the_status(caplog):
    rows = [row("1315", "2026-10-03 08:00:00"), row("2533", "2026-10-03 09:00:00")]
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(rows, include_history=True)
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["history"][-1]["status"] is None
    assert not caplog.records


def test_not_found_is_a_silent_pending_unknown(caplog):
    with caplog.at_level(logging.WARNING):
        parcel = normalize_tracking_parcel({}, code=CODE, include_history=True)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["barcode"] == CODE
    assert parcel["raw_status"] is None
    assert parcel["history"] == []
    assert not caplog.records


def test_wording_falls_back_to_native_then_to_the_code():
    native = row("606", "2026-10-02 10:00:00", wording="")
    native["ActionMessages"] = "Прийнято"
    assert _normalize([native])["raw_status"] == "Прийнято"
    bare = row("606", "2026-10-02 10:00:00", wording="")
    assert _normalize([bare])["raw_status"] == "606"


def test_history_is_oldest_first_capped_and_carries_place():
    rows = [
        row("606", f"2026-10-{d:02d} 10:00:00", wording=f"Scan {d}")
        for d in range(1, 26)
    ]
    history = _normalize(rows, include_history=True)["history"]
    assert len(history) == 20
    assert history[0]["raw_status"] == "Scan 6 (Testville, UKRAINE)"
    assert history[-1]["timestamp"] == "2026-10-25T10:00:00+01:00"
    assert history[-1]["status"] is ParcelStatus.IN_TRANSIT
    assert set(history[0]) == {"timestamp", "status", "raw_status"}


def test_history_without_place_or_wording():
    bare = row("606", "2026-10-02 10:00:00", wording="", city="", country="")
    assert _normalize([bare], include_history=True)["history"][0]["raw_status"] == "606"
    no_place = row("606", "2026-10-02 10:00:00", city="", country="")
    assert (
        _normalize([no_place], include_history=True)["history"][0]["raw_status"]
        == "Parcel registered"
    )


def test_history_skips_rows_without_a_usable_timestamp():
    rows = [row("606", "garbage"), row("1315", "2026-10-03 08:00:00")]
    assert len(_normalize(rows, include_history=True)["history"]) == 1


def test_a_day_without_a_time_is_midnight_of_that_day():
    assert clock_to_iso("2026-10-05 00:00:00") == "2026-10-05T00:00:00+02:00"
    assert clock_to_iso("2026-10-05 00:00:01") == "2026-10-05T00:00:00+02:00"
    assert clock_to_iso("2026-10-05 00:00:02") == "2026-10-05T00:00:02+02:00"
    rows = [row("1622", "2026-10-05 00:00:00")]
    parcel = _normalize(rows, include_history=True)
    assert parcel["delivered_at"] == "2026-10-05T00:00:00+02:00"
    assert parcel["history"][0]["timestamp"] == "2026-10-05T00:00:00+02:00"


@pytest.mark.parametrize("value", [None, "", "05/10/2026", "2026-10-05"])
def test_clock_to_iso_rejects_other_shapes(value):
    assert clock_to_iso(value) is None


def test_canonical_code_ignores_case_spaces_and_hyphens():
    assert canonical_code(" ab-12 3 ") == "AB123"
    assert canonical_code(None) == ""


def test_match_accepts_any_identifying_field_and_flags_own():
    rows = [
        row("100", "2026-10-01 09:00:00", own=OTHER_CODE, number=CODE),
        row("606", "2026-10-02 10:00:00", own=OTHER_CODE, barcode=CODE),
        row("1315", "2026-10-03 10:00:00", own=OTHER_CODE, transit=CODE),
        row("11", "2026-10-03 11:00:00", own=OTHER_CODE),
    ]
    accepted, own = match_rows(rows, CODE)
    assert [r["StatusCode"] for r in accepted] == ["100", "606", "1315"]
    assert own is True


def test_match_with_only_the_transit_reference_is_not_own():
    rows = [row("1315", "2026-10-03 10:00:00", own=OTHER_CODE, transit=CODE)]
    accepted, own = match_rows(rows, CODE)
    assert len(accepted) == 1 and own is False


def test_match_dedupes_the_same_scan_from_two_feeds():
    first = row("606", "2026-10-02 10:00:00")
    second = dict(first, AgentCode="OTHER", ActionId="903")
    other = row("606", "2026-10-02 10:00:00", wording="Different wording")
    accepted, _ = match_rows([first, second, other], CODE)
    assert len(accepted) == 2
    assert accepted[0] is first


def test_match_compares_hyphenated_labels():
    rows = [row("100", "2026-10-01 09:00:00", own="723-3993703")]
    accepted, own = match_rows(rows, "7233993703")
    assert accepted and own


def test_a_backfilled_registration_does_not_undo_the_journey():
    parcel = _normalize(backfilled_registration(), include_history=True)

    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Departure from sorting center"
    assert parcel["delivered_at"] is None
    # the backfilled rows stay in the history, newest last
    assert parcel["history"][-1]["raw_status"].startswith("To be delivered")
    assert parcel["history"][-1]["status"] is ParcelStatus.REGISTERED


def test_a_parcel_only_registered_still_reads_registered():
    parcel = _normalize(
        [row("100", "2026-10-01 09:00:00"), row("101", "2026-10-01 09:05:00")]
    )

    assert parcel["status"] is ParcelStatus.REGISTERED
