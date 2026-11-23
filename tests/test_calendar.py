"""Tests for the deliveries calendar."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.calendar import MeestDeliveriesCalendar
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
            _window(start),
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
    assert "Status: out_for_delivery" in events[0].description
    assert events[0].location == "Branch 12"  # the route ends at a pickup point
    assert calendar.event.uid == "CV000000001US"
    assert (
        await calendar.async_get_events(
            hass, start + timedelta(days=5), start + timedelta(days=6)
        )
        == []
    )


async def _calendar(hass, records):
    entry = await _setup(hass, records)
    return next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )


async def test_a_moment_runs_to_the_end_of_its_day_as_a_timed_event(hass):
    record = account_record(
        estimatedDeliveryDate=None,
        delivery={"timeFrom": "2099-03-10T10:00:00+00:00"},
        sender=None,
    )
    calendar = await _calendar(hass, [record])
    event = calendar.event
    assert isinstance(event.start, datetime)
    end = dt_util.as_local(event.end)
    assert (end.hour, end.minute, end.second) == (23, 59, 59)
    assert end.date() == dt_util.as_local(event.start).date()
    assert event.summary == "Parcel CV000000001US"


async def test_a_whole_day_window_is_an_all_day_event(hass):
    record = account_record(estimatedDeliveryDate="2099-03-10", delivery={})
    calendar = await _calendar(hass, [record])
    event = calendar.event
    assert event.start == date(2099, 3, 10)
    assert event.end == date(2099, 3, 11)
    assert event.location is None
    listed = await calendar.async_get_events(
        hass,
        dt_util.start_of_local_day(date(2099, 3, 9)),
        dt_util.start_of_local_day(date(2099, 3, 12)),
    )
    assert [e.start for e in listed] == [date(2099, 3, 10)]


async def test_no_upcoming_event_when_everything_is_past(hass):
    entry = await _setup(hass, [_window(dt_util.now() - timedelta(days=3))])
    calendar = next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )
    assert calendar.event is None


def test_a_moment_with_no_time_left_in_its_day_gets_an_hour():
    coordinator = MagicMock()
    coordinator.data = {
        "incoming_active": [
            {
                "barcode": "X1",
                "planned_from": "2099-03-10T23:59:59+00:00",
                "planned_to": None,
            }
        ]
    }
    entry = MagicMock()
    entry.entry_id = "e1"
    (event,) = MeestDeliveriesCalendar(coordinator, entry)._events()
    assert event.end - event.start == timedelta(hours=1)
