"""Tests for the deliveries calendar."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.const import DOMAIN
from tests.payloads import account_record, account_sent_record

ACCOUNT = "custom_components.meest.account.client.MeestAccountClient"


async def _setup(hass, received):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Meest",
        unique_id="account:u",
        data={"source": "account", "login": "u", "token": "t", "refresh_token": "r"},
    )
    entry.add_to_hass(hass)
    records = {MODE_RECEIVER: received, MODE_SENDER: [account_sent_record()]}
    with (
        patch(
            f"{ACCOUNT}.async_get_parcels",
            new=AsyncMock(side_effect=lambda m: records[m]),
        ),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _window(start, hours=2, **extra):
    return account_record(
        estimatedDeliveryDate=None,
        delivery={
            "timeFrom": start.isoformat(),
            "timeTo": (start + timedelta(hours=hours)).isoformat(),
            "branch": "Branch 12",
        },
        **extra,
    )


async def test_incoming_windows_become_events_and_outgoing_are_left_out(hass):
    start = dt_util.now() + timedelta(days=1)
    entry = await _setup(
        hass,
        [
            _window(start, statusCode="1220"),
            account_record(
                delivery={},
                estimatedDeliveryDate=None,
                parcelID="Z",
                parcelNumber="NOWIN",
            ),
        ],
    )
    calendar = next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )

    events = await calendar.async_get_events(
        hass, start - timedelta(hours=1), start + timedelta(days=1)
    )

    assert len(events) == 1
    assert events[0].uid == "CV000000001US"
    assert events[0].summary == "Sender Name" or events[0].summary.startswith("Parcel")
    assert "Status: at_pickup_point" in events[0].description
    assert events[0].location == "Branch 12"  # at a pickup point
    assert calendar.event.uid == "CV000000001US"
    assert (
        await calendar.async_get_events(
            hass, start + timedelta(days=5), start + timedelta(days=6)
        )
        == []
    )


async def test_a_point_estimate_gets_a_default_duration(hass):
    start = dt_util.now() + timedelta(days=1)
    record = account_record(
        estimatedDeliveryDate=None,
        delivery={"timeFrom": start.isoformat()},
        sender=None,
    )
    entry = await _setup(hass, [record])
    calendar = next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )
    event = calendar.event
    assert event.end - event.start == timedelta(hours=1)
    assert event.summary == "Parcel CV000000001US"
    assert event.location is None


async def test_no_upcoming_event_when_everything_is_past(hass):
    entry = await _setup(hass, [_window(dt_util.now() - timedelta(days=3))])
    calendar = next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )
    assert calendar.event is None
