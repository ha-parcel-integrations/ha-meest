"""Tests for the sensor platform."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.meest.sensor import MeestParcelSensor
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
    # no en_route sensor: the tracking source only learns of a pickup point on arrival
    assert hass.states.get("sensor.meest_en_route_to_pickup_point") is None

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
