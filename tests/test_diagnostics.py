"""Tests for diagnostics redaction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import MODE_RECEIVER, MODE_SENDER
from custom_components.meest.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN
from custom_components.meest.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from tests.payloads import (
    CODE,
    LATE_LABEL,
    OTHER_CODE,
    account_record,
    account_sent_record,
    journey,
    row,
)

GET_PARCEL = "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel"
ACCOUNT = "custom_components.meest.account.client.MeestAccountClient"


async def test_tracking_diagnostics_redact_every_number_and_place(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: CODE},
                {CONF_TRACKING_CODE: OTHER_CODE, "direction": "outgoing"},
            ],
            "delivered_filter_type": "parcels",
        },
    )
    entry.add_to_hass(hass)
    transit = {
        "rows": [
            row("606", "2026-10-02 10:00:00", own=OTHER_CODE, transit="PARTNER001")
        ]
    }
    results = {CODE: {"rows": journey()}, OTHER_CODE: transit}
    with patch(GET_PARCEL, new=AsyncMock(side_effect=lambda code: results[code])):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diagnostics, default=str)

    for secret in (
        CODE,
        LATE_LABEL,
        OTHER_CODE,
        "PARTNER001",
        "Testville",
        "UKRAINE",
        "0xAAAA0001",
    ):
        assert secret not in text, secret
    assert diagnostics["counts"] == {
        "incoming_active": 0,
        "delivered": 1,
        "outgoing_active": 1,
        "outgoing_delivered": 0,
        "skipped_from_fetch": 1,
    }
    assert diagnostics["polling"]["tier_minutes"] is not None
    assert len(diagnostics["delivered"]) == 1
    assert len(diagnostics["outgoing"]) == 1
    assert diagnostics["incoming"] == [] and diagnostics["delivered_outgoing"] == []
    # the status and the codes remain readable for mapping reports
    assert diagnostics["delivered"][0]["status"] == "delivered"
    assert diagnostics["delivered"][0]["raw"]["rows"][0]["StatusCode"] == "100"


async def test_suspended_polling_is_reported(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    with patch(GET_PARCEL, new=AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["polling"]["suspended"] is True
    assert diagnostics["polling"]["update_interval_seconds"] is None


async def test_account_diagnostics_redact_login_tokens_and_parcel_blocks(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Meest",
        unique_id="account:person@example.test",
        data={
            "source": "account",
            "login": "person@example.test",
            "token": "secret-token",
            "refresh_token": "secret-refresh",
        },
    )
    entry.add_to_hass(hass)
    receiver = account_record(
        contentName="Secret contents",
        COD={"amount": 5},
        insurance={"x": 1},
        globalUserId="user-guid",
    )
    records = {MODE_RECEIVER: [receiver], MODE_SENDER: [account_sent_record()]}
    with (
        patch(
            f"{ACCOUNT}.async_get_parcels",
            new=AsyncMock(side_effect=lambda m: records[m]),
        ),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diagnostics, default=str)

    for secret in (
        "person@example.test",
        "secret-token",
        "secret-refresh",
        "CV000000001US",
        "CR000000002US",
        "Secret contents",
        "Sender Name",
        "Eiver",
        "Branch 12",
        "1 Test Street",
        "user-guid",
        "P-1001",
        "Drop-off 3",
    ):
        assert secret not in text, secret
    # the window stays readable: it is what the open questions are about
    assert diagnostics["incoming"][0]["raw"]["delivery"]["timeFrom"] == "10:00"
    assert diagnostics["counts"]["outgoing_active"] == 1


def test_redaction_list_covers_the_documented_fields():
    for key in (
        "ShipmentNumber",
        "ShipmentNumberSender",
        "ShipmentNumberTransit",
        "Barcode",
        "ShipmentIdRef",
        "DocumentIdRef",
        "AgentsIdRef",
        "City_EN",
        "Country_EN",
        "login",
        "globalUserId",
        "token",
        "parcelNumber",
        "barCode",
        "sender",
        "receiver",
        "contentName",
        "contentsItems",
        "COD",
        "insurance",
        "branch",
    ):
        assert key in TO_REDACT
