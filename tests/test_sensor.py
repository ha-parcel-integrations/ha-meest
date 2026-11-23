"""Tests for the sensor platform."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.meest.sensor import (
    MeestAwaitingPickupSensor,
    MeestEnRouteToPickupPointSensor,
    MeestNextDeliverySensor,
    MeestParcelSensor,
)
from tests.payloads import CODE, OTHER_CODE, account_record, account_sent_record, row

GET_PARCEL = "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel"
ACCOUNT = "custom_components.meest.account.client.MeestAccountClient"


def _scan(status_code):
    return lambda code: {"rows": [row(status_code, "2026-10-02 10:00:00", own=code)]}


async def _setup_tracking(hass, parcels, side_effect):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: parcels, "delivered_filter_type": "parcels"},
    )
    entry.add_to_hass(hass)
    with patch(GET_PARCEL, new=AsyncMock(side_effect=side_effect)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_summary_sensors_and_per_parcel_sensor(hass):
    results = {CODE: _scan("1220")(CODE), OTHER_CODE: _scan("1622")(OTHER_CODE)}
    await _setup_tracking(
        hass,
        [{CONF_TRACKING_CODE: CODE}, {CONF_TRACKING_CODE: OTHER_CODE}],
        lambda code: results[code],
    )

    incoming = hass.states.get("sensor.meest_incoming_parcels")
    assert incoming.state == "1"
    assert incoming.attributes["parcels"][0]["barcode"] == CODE
    assert hass.states.get("sensor.meest_awaiting_pickup").state == "1"
    assert hass.states.get("sensor.meest_delivered_parcels").state == "1"
    assert hass.states.get("sensor.meest_outgoing_parcels").state == "0"
    assert hass.states.get("sensor.meest_last_successful_update").state != "unknown"
    # the parcel has arrived, so it is awaiting pickup and no longer en route
    assert hass.states.get("sensor.meest_en_route_to_pickup_point").state == "0"

    parcel = hass.states.get(f"sensor.meest_parcel_{CODE.lower()}")
    assert parcel.state == "at_pickup_point"
    assert parcel.attributes["barcode"] == CODE
    assert parcel.attributes["carrier"] == "Meest"


async def test_next_delivery_reads_the_earliest_incoming_window(hass):
    when = dt_util.now() + timedelta(hours=3)
    records = {
        MODE_RECEIVER: [
            account_record(
                parcelID="A",
                parcelNumber="N1",
                delivery={"timeFrom": (when + timedelta(hours=2)).isoformat()},
                estimatedDeliveryDate=None,
            ),
            account_record(
                parcelID="B",
                parcelNumber="N2",
                delivery={"timeFrom": when.isoformat()},
                estimatedDeliveryDate=None,
            ),
        ],
        MODE_SENDER: [account_sent_record()],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Meest",
        unique_id="account:u",
        data={"source": "account", "login": "u", "token": "t", "refresh_token": "r"},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            f"{ACCOUNT}.async_get_parcels",
            new=AsyncMock(side_effect=lambda m: records[m]),
        ),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.meest_u_next_delivery")
    assert state.attributes["barcode"] == "N2"
    assert dt_util.parse_datetime(state.state) == when.replace(microsecond=0)


async def test_next_delivery_is_empty_without_windows(hass):
    await _setup_tracking(hass, [{CONF_TRACKING_CODE: CODE}], _scan("606"))
    state = hass.states.get("sensor.meest_next_delivery")
    assert state.state == "unknown"
    assert "barcode" not in state.attributes


async def test_outgoing_parcels_get_their_own_summaries_and_sensors(hass):
    results = {CODE: _scan("606")(CODE), OTHER_CODE: _scan("1622")(OTHER_CODE)}
    await _setup_tracking(
        hass,
        [
            {CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "outgoing"},
            {CONF_TRACKING_CODE: OTHER_CODE, CONF_DIRECTION: "outgoing"},
        ],
        lambda code: results[code],
    )
    assert hass.states.get("sensor.meest_incoming_parcels").state == "0"
    assert hass.states.get("sensor.meest_outgoing_parcels").state == "1"
    assert hass.states.get("sensor.meest_outgoing_delivered_parcels").state == "1"
    assert hass.states.get(f"sensor.meest_parcel_{CODE.lower()}").state == "in_transit"


async def test_per_parcel_sensor_is_removed_on_delivery_but_reads_every_list(hass):
    entry = await _setup_tracking(hass, [{CONF_TRACKING_CODE: CODE}], _scan("606"))
    coordinator = entry.runtime_data.coordinator
    with patch(GET_PARCEL, new=AsyncMock(side_effect=_scan("1622"))):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # the summary sensor retires it through the registry
    assert hass.states.get(f"sensor.meest_parcel_{CODE.lower()}") is None
    # while a sensor still holding the barcode finds it in the delivered list
    sensor = MeestParcelSensor(coordinator, entry, CODE)
    assert sensor.native_value == "delivered"
    assert MeestParcelSensor(coordinator, entry, "NOPE00001").native_value is None
    assert (
        MeestParcelSensor(coordinator, entry, "NOPE00001").extra_state_attributes == {}
    )


async def test_stale_parcel_sensors_are_swept_at_setup_but_summaries_survive(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_GONE000001", config_entry=entry
    )
    summary = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_outgoing_parcels", config_entry=entry
    )
    other_domain = registry.async_get_or_create(
        "button", DOMAIN, f"{entry.entry_id}_refresh", config_entry=entry
    )
    with patch(GET_PARCEL, new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get(summary.entity_id) is not None
    assert registry.async_get(other_domain.entity_id) is not None


def _fake_coordinator(parcels):

    coordinator = MagicMock()
    coordinator.data = {"incoming_active": parcels}
    return coordinator


def _fake_entry():

    entry = MagicMock()
    entry.entry_id = "e1"
    return entry


def _fake_parcel(barcode, status, pickup=False, planned_from=None, planned_to=None):

    return {
        "barcode": barcode,
        "status": ParcelStatus(status),
        "pickup": pickup,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "sender": None,
        "receiver": None,
    }


def test_en_route_and_awaiting_pickup_split_without_overlap():

    parcels = [
        _fake_parcel("GOING", "in_transit", pickup=True),
        _fake_parcel("READY", "at_pickup_point", pickup=True),
        _fake_parcel("BACK", "returning", pickup=True),
        _fake_parcel("HOME", "in_transit"),
    ]
    en_route = MeestEnRouteToPickupPointSensor(
        _fake_coordinator(parcels), _fake_entry()
    )
    awaiting = MeestAwaitingPickupSensor(_fake_coordinator(parcels), _fake_entry())
    assert en_route.unique_id == "e1_en_route_to_pickup_point"
    assert [p["barcode"] for p in en_route.extra_state_attributes["parcels"]] == [
        "GOING"
    ]
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == [
        "READY"
    ]
    assert en_route.native_value == 1 and awaiting.native_value == 1


def test_next_delivery_skips_a_window_that_has_ended():

    parcels = [
        _fake_parcel(
            "OLD",
            "in_transit",
            planned_from="2020-01-01T10:00:00+00:00",
            planned_to="2020-01-01T23:59:59+00:00",
        ),
        _fake_parcel("NEW", "in_transit", planned_from="2099-01-01T10:00:00+00:00"),
    ]
    sensor = MeestNextDeliverySensor(_fake_coordinator(parcels), _fake_entry())
    assert sensor.extra_state_attributes["barcode"] == "NEW"


async def test_the_pickup_summaries_survive_the_stale_sweep(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    kept = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_en_route_to_pickup_point",
        config_entry=entry,
    )
    with patch(GET_PARCEL, new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(kept.entity_id) is not None
