"""Tests for the shared status vocabulary."""

import logging

import pytest

from custom_components.meest.const import ParcelStatus
from custom_components.meest.status import (
    APP_CODE_MAP,
    NON_MOVEMENT_CODES,
    STATUS_MAP,
    clean_code,
    lookup,
    map_app_code,
    map_scan_code,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("100", ParcelStatus.REGISTERED),
        ("606", ParcelStatus.IN_TRANSIT),
        ("1315", ParcelStatus.OUT_FOR_DELIVERY),
        ("1200", ParcelStatus.AT_PICKUP_POINT),
        ("1214", ParcelStatus.AT_PICKUP_POINT),
        ("1220", ParcelStatus.AT_PICKUP_POINT),
        ("1622", ParcelStatus.DELIVERED),
        ("1724", ParcelStatus.DELIVERED),
        ("1521", ParcelStatus.RETURNING),
        ("2331", ParcelStatus.PROBLEM),
        ("2432", ParcelStatus.IN_TRANSIT),
        ("2000", ParcelStatus.IN_TRANSIT),
        ("2227", ParcelStatus.IN_TRANSIT),
        # live but absent from the carrier's published list
        ("324", ParcelStatus.IN_TRANSIT),
    ],
)
def test_scan_codes_map(code, expected):
    assert lookup(code) is expected


def test_every_mapped_value_is_a_canonical_status():
    assert set(STATUS_MAP.values()) <= set(ParcelStatus)
    assert ParcelStatus.UNKNOWN not in STATUS_MAP.values()


def test_correction_code_is_ignored_silently(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_scan_code("2533") is None
    assert "2533" in NON_MOVEMENT_CODES
    assert "2533" not in STATUS_MAP
    assert not caplog.records


def test_unmapped_code_warns_once_with_issue_link(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_scan_code("9999") is None
        assert map_scan_code("9999") is None
    assert len(caplog.records) == 1
    text = caplog.records[0].getMessage()
    assert "status=9999" in text
    assert "issues/new?template=unrecognised_status.yml" in text


def test_same_code_from_two_sources_warns_per_source(caplog):
    with caplog.at_level(logging.WARNING):
        map_scan_code("9999", source="tracking")
        map_scan_code("9999", source="account")
    assert len(caplog.records) == 2


def test_missing_code_is_silent(caplog):
    assert map_scan_code(None) is None
    assert map_scan_code("") is None
    assert lookup(None) is None
    assert not caplog.records


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (True, None), ("  ", None), (1622, "1622"), (" 12 ", "12")],
)
def test_clean_code(value, expected):
    assert clean_code(value) == expected


@pytest.mark.parametrize("ordinal", range(len(APP_CODE_MAP)))
def test_app_ordinals_map(ordinal):
    assert map_app_code(ordinal) is APP_CODE_MAP[ordinal]
    assert map_app_code(str(ordinal)) is APP_CODE_MAP[ordinal]


def test_app_ordinal_out_of_range_warns_instead_of_defaulting_to_created(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_app_code(9) is None
        assert map_app_code(9) is None
    assert len(caplog.records) == 1
    assert "appCode=9" in caplog.records[0].getMessage()


@pytest.mark.parametrize("value", [None, True, "x", "abc"])
def test_app_ordinal_garbage_is_none(value):
    assert map_app_code(value) is None
