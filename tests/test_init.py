"""Tests for Meest setup, token persistence and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.client import MeestAccountReauthRequired
from custom_components.meest.account.parcels import MODE_RECEIVER
from custom_components.meest.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from tests.payloads import CODE, OTHER_CODE, account_record, account_sent_record, row

GET_PARCEL = "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel"
ACCOUNT = "custom_components.meest.account.client.MeestAccountClient"
LOGIN = "user@example.test"


def _tracking_entry(parcels) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: parcels},
    )


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Meest ({LOGIN})",
        unique_id=f"account:{LOGIN}",
        data={"source": "account", "login": LOGIN, "token": "t", "refresh_token": "r"},
    )


def _transit(code=CODE):
    return {"rows": [row("606", "2026-10-02 10:00:00", own=code)]}


async def test_tracking_setup_registers_services_and_unload_removes_them(hass):
    entry = _tracking_entry([{CONF_TRACKING_CODE: CODE}])
    entry.add_to_hass(hass)
    with patch(GET_PARCEL, new=AsyncMock(return_value=_transit())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.states.get("sensor.meest_incoming_parcels").state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_unloading_an_account_leaves_the_tracking_services_alone(hass):
    tracking = _tracking_entry([])
    tracking.add_to_hass(hass)
    account = _account_entry()
    account.add_to_hass(hass)
    with (
        patch(GET_PARCEL, new=AsyncMock(return_value=None)),
        patch(f"{ACCOUNT}.async_get_parcels", new=AsyncMock(return_value=[])),
    ):
        assert await hass.config_entries.async_setup(tracking.entry_id)
        await hass.async_block_till_done()
        assert account.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_unload(account.entry_id)
        assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_account_setup_registers_no_services(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            f"{ACCOUNT}.async_get_parcels",
            new=AsyncMock(
                side_effect=lambda mode: (
                    [account_record()]
                    if mode == MODE_RECEIVER
                    else [account_sent_record()]
                )
            ),
        ),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert (
        hass.states.get("sensor.meest_user_example_test_incoming_parcels").state == "1"
    )
    assert (
        hass.states.get("sensor.meest_user_example_test_outgoing_parcels").state == "1"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rotated_tokens_are_written_back_to_the_entry(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    client_calls = []

    async def fake_parcels(self, mode):
        if not client_calls:
            client_calls.append(1)
            await self.async_refresh()
        return []

    class _Rotated(AsyncMock):
        pass

    async def fake_request(self, method, path, **kw):
        return {"token": "tok-2", "refreshToken": "ref-2"}

    with (
        patch(f"{ACCOUNT}.async_get_parcels", new=fake_parcels),
        patch(f"{ACCOUNT}._request", new=fake_request),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.data["token"] == "tok-2"
    assert entry.data["refresh_token"] == "ref-2"
    assert entry.data["login"] == LOGIN


async def test_a_rejected_session_at_setup_starts_reauth(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with patch(
        f"{ACCOUNT}.async_get_parcels",
        new=AsyncMock(side_effect=MeestAccountReauthRequired("no")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_a_transient_failure_at_setup_retries(hass):
    entry = _tracking_entry([{CONF_TRACKING_CODE: CODE}])
    entry.add_to_hass(hass)
    with patch(
        GET_PARCEL, new=AsyncMock(side_effect=aiohttp.ClientConnectionError("x"))
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_option_changes_refresh_live_and_add_a_sensor(hass):
    entry = _tracking_entry([])
    entry.add_to_hass(hass)
    with patch(GET_PARCEL, new=AsyncMock(side_effect=lambda code: _transit(code))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.meest_incoming_parcels").state == "0"

        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: CODE},
                    {CONF_TRACKING_CODE: OTHER_CODE, CONF_DIRECTION: "outgoing"},
                ]
            },
        )
        await hass.async_block_till_done()

        assert hass.states.get("sensor.meest_incoming_parcels").state == "1"
        assert hass.states.get("sensor.meest_outgoing_parcels").state == "1"
        registry = er.async_get(hass)
        # per-parcel sensors exist for both directions
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{CODE}"
        )
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )

        hass.config_entries.async_update_entry(entry, options={CONF_PARCELS: []})
        # the listener's refresh is debounced after the first one
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{CODE}"
        )
        assert not registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )


async def test_a_token_rotation_does_not_trigger_a_poll(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    parcels = AsyncMock(return_value=[])
    with patch(f"{ACCOUNT}.async_get_parcels", new=parcels):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        polls = parcels.await_count

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": "rotated"}
        )
        await hass.async_block_till_done()

        assert parcels.await_count == polls
