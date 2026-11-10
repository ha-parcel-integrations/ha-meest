"""Tests for the account coordinator."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.meest.account.client import (
    MeestAccountApiError,
    MeestAccountReauthRequired,
)
from custom_components.meest.account.coordinator import MeestAccountCoordinator
from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    DOMAIN,
    ParcelStatus,
)
from tests.payloads import account_events, account_record, account_sent_record


def _setup(hass, *, received=(), sent=(), **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user",
        data={"source": "account"},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            **options,
        },
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    modes = {MODE_RECEIVER: list(received), MODE_SENDER: list(sent)}
    client.async_get_parcels.side_effect = lambda mode: modes[mode]
    client.async_get_parcel_info.return_value = None
    client.async_get_history.return_value = []
    return MeestAccountCoordinator(hass, client, entry), client, modes


async def test_both_modes_are_fetched_and_split_by_direction(hass):
    coordinator, client, _ = _setup(
        hass,
        received=[
            account_record(),
            account_record(parcelID="P-9", parcelNumber="D1", statusCode="1622"),
        ],
        sent=[account_sent_record()],
    )

    data = await coordinator._async_update_data()

    assert [c.args[0] for c in client.async_get_parcels.await_args_list] == [
        MODE_RECEIVER,
        MODE_SENDER,
    ]
    assert [p["barcode"] for p in data["incoming_active"]] == ["CV000000001US"]
    assert [p["barcode"] for p in data["incoming_delivered"]] == ["D1"]
    assert [p["barcode"] for p in data["outgoing_active"]] == ["AB000000003CD00010G"]
    assert coordinator.last_success_time is not None
    # an account never suspends: new parcels are found by polling
    assert coordinator.update_interval is not None


async def test_empty_account_keeps_polling(hass):
    coordinator, _, _ = _setup(hass)
    data = await coordinator._async_update_data()
    assert data["incoming_active"] == []
    assert coordinator.update_interval is not None


async def test_a_parcel_you_send_and_receive_appears_once_as_incoming(hass):
    same = account_record(parcelID="P-7")
    coordinator, _, _ = _setup(
        hass, received=[same], sent=[account_sent_record(parcelID="P-7")]
    )
    data = await coordinator._async_update_data()
    assert len(data["incoming_active"]) == 1
    assert data["outgoing_active"] == []


async def test_records_without_an_id_are_not_deduplicated(hass):
    coordinator, _, _ = _setup(
        hass,
        received=[account_record(parcelID=None)],
        sent=[account_sent_record(parcelID=None)],
    )
    data = await coordinator._async_update_data()
    assert len(data["incoming_active"]) == 1 and len(data["outgoing_active"]) == 1


async def test_detail_is_merged_over_the_list_record(hass):
    coordinator, client, _ = _setup(hass, received=[account_record(statusCode="606")])
    client.async_get_parcel_info.return_value = {"statusCode": "1220"}
    data = await coordinator._async_update_data()
    assert data["incoming_active"][0]["status"] is ParcelStatus.AT_PICKUP_POINT
    client.async_get_parcel_info.assert_awaited_once_with("P-1001")


async def test_delivered_parcels_skip_the_detail_call(hass):
    coordinator, client, _ = _setup(
        hass,
        received=[
            account_record(factDeliveryDate="2026-10-05T10:00:00", statusCode="1622")
        ],
    )
    await coordinator._async_update_data()
    client.async_get_parcel_info.assert_not_awaited()


async def test_history_is_fetched_only_when_enabled(hass):
    coordinator, client, _ = _setup(hass, received=[account_record()])
    await coordinator._async_update_data()
    client.async_get_history.assert_not_awaited()

    coordinator, client, _ = _setup(
        hass, received=[account_record()], **{CONF_INCLUDE_HISTORY: True}
    )
    client.async_get_history.return_value = account_events()
    data = await coordinator._async_update_data()
    client.async_get_history.assert_awaited_once_with("CV000000001US")
    assert len(data["incoming_active"][0]["history"]) == 2


async def test_enrichment_failures_keep_the_list_record(hass, caplog):
    coordinator, client, _ = _setup(
        hass, received=[account_record()], **{CONF_INCLUDE_HISTORY: True}
    )
    client.async_get_parcel_info.side_effect = MeestAccountApiError("x")
    client.async_get_history.side_effect = MeestAccountApiError("x")
    data = await coordinator._async_update_data()
    assert len(data["incoming_active"]) == 1
    assert data["incoming_active"][0]["status"] is ParcelStatus.OUT_FOR_DELIVERY


async def test_enrichment_without_ids_makes_no_calls(hass):
    coordinator, client, _ = _setup(
        hass,
        received=[account_record(parcelID=None, parcelNumber=None, barCode=None)],
        **{CONF_INCLUDE_HISTORY: True},
    )
    await coordinator._async_update_data()
    client.async_get_parcel_info.assert_not_awaited()
    client.async_get_history.assert_not_awaited()


@pytest.mark.parametrize("where", ["list", "detail", "history"])
async def test_a_rejected_session_starts_reauth(hass, where):
    coordinator, client, _ = _setup(
        hass, received=[account_record()], **{CONF_INCLUDE_HISTORY: True}
    )
    err = MeestAccountReauthRequired("no")
    if where == "list":
        client.async_get_parcels.side_effect = err
    elif where == "detail":
        client.async_get_parcel_info.side_effect = err
    else:
        client.async_get_history.side_effect = err
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_api_errors_keep_last_good_via_update_failed(hass):
    coordinator, client, _ = _setup(hass)
    client.async_get_parcels.side_effect = MeestAccountApiError("x", status_code=500)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval is not None


async def test_429_backs_off(hass):
    coordinator, client, _ = _setup(hass)
    client.async_get_parcels.side_effect = MeestAccountApiError("x", status_code=429)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 120


async def test_network_errors_propagate_to_the_base_class(hass):
    coordinator, client, _ = _setup(hass)
    client.async_get_parcels.side_effect = aiohttp.ClientConnectionError("down")
    with pytest.raises(aiohttp.ClientError):
        await coordinator._async_update_data()


async def test_events_cover_both_directions_and_the_first_refresh_is_silent(hass):
    coordinator, _, modes = _setup(
        hass,
        received=[account_record(statusCode="606")],
        sent=[account_sent_record(statusCode="606")],
    )
    changed = async_capture_events(hass, f"{DOMAIN}_parcel_status_changed")
    out_changed = async_capture_events(hass, f"{DOMAIN}_outgoing_parcel_status_changed")
    out_delivered = async_capture_events(hass, f"{DOMAIN}_outgoing_parcel_delivered")

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert not changed and not out_changed

    modes[MODE_RECEIVER][0]["statusCode"] = "1315"
    modes[MODE_SENDER][0]["statusCode"] = "1315"
    await coordinator._async_update_data()
    modes[MODE_SENDER][0]["statusCode"] = "1622"
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(changed) == 1 and len(out_changed) == 1 and len(out_delivered) == 1


async def test_direction_mismatch_between_modes_is_logged(hass, caplog):
    coordinator, _, _ = _setup(
        hass,
        received=[account_record(direction=1)],
        sent=[account_sent_record(direction=1)],
    )
    with caplog.at_level(logging.WARNING):
        await coordinator._async_update_data()
    assert any("disagree about direction" in r.getMessage() for r in caplog.records)


async def test_history_is_fetched_when_the_record_cannot_say_the_status(hass):
    """The contract's middle rung must not depend on the history option."""
    coordinator, client, _ = _setup(
        hass,
        received=[account_record(statusCode="9998", appCode=None)],
        sent=[],
    )
    client.async_get_history.return_value = [
        {"statusCode": "1622", "eventDateTime": "2026-10-04T10:00:00"}
    ]

    data = await coordinator._async_update_data()

    assert client.async_get_history.await_count == 1
    assert data["incoming_delivered"][0]["status"] is ParcelStatus.DELIVERED
    # the attribute itself stays opt-in
    assert data["incoming_delivered"][0]["history"] is None


async def test_a_resolved_record_does_not_fetch_history(hass):
    coordinator, client, _ = _setup(hass, received=[account_record()], sent=[])

    await coordinator._async_update_data()

    assert client.async_get_history.await_count == 0
