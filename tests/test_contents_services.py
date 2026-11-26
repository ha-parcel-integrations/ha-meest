"""Tests for update_contents, the track_parcel field and the store lifecycle."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.parcels import (
    MODE_RECEIVER,
    MODE_SENDER,
    normalize_account_parcel,
)
from custom_components.meest.const import CONF_PARCELS, CONF_TRACKING_CODE, DOMAIN
from custom_components.meest.contents import CONTENTS_STORE_MAX_CODES, ContentsStore
from custom_components.meest.coordinator import MeestCoordinator
from custom_components.meest.diagnostics import async_get_config_entry_diagnostics
from custom_components.meest.services import async_setup_services
from custom_components.meest.tracking.parcels import normalize_tracking_parcel
from tests.payloads import CODE, OTHER_CODE, account_record, account_sent_record, row

GET_PARCEL = "custom_components.meest.tracking.api.MeestTrackingClient.async_get_parcel"
ACCOUNT = "custom_components.meest.account.client.MeestAccountClient"
UNTRACKED = "ZZ000000009ZZ"
HUB_ONLY = "HB000000004ZZ"
SHOES = "Running shoes"


def _hub(codes) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Meest",
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


def _account(name="u") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Meest",
        unique_id=f"account:{name}",
        data={"source": "account", "login": name, "token": "t", "refresh_token": "r"},
    )


def _scan(code):
    return {"rows": [row("606", "2026-10-02 10:00:00", own=code)]}


def _fetch():
    return AsyncMock(side_effect=_scan)


def _account_patches(received=None):
    records = {
        MODE_RECEIVER: [account_record()] if received is None else received,
        MODE_SENDER: [account_sent_record()],
    }
    return (
        patch(
            f"{ACCOUNT}.async_get_parcels",
            new=AsyncMock(side_effect=lambda m: records[m]),
        ),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=AsyncMock(return_value=None)),
    )


async def _setup_hub(hass, codes=(CODE,)):
    entry = _hub(codes)
    entry.add_to_hass(hass)
    with patch(GET_PARCEL, new=_fetch()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass, name="u", received=None):
    entry = _account(name)
    entry.add_to_hass(hass)
    parcels, info = _account_patches(received)
    with parcels, info:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_both(hass, codes=(CODE,)):
    hub, account = _hub(codes), _account()
    hub.add_to_hass(hass)
    account.add_to_hass(hass)
    parcels, info = _account_patches()
    with patch(GET_PARCEL, new=_fetch()), parcels, info:
        await hass.config_entries.async_setup(hub.entry_id)
        await hass.async_block_till_done()
    assert hub.state is ConfigEntryState.LOADED
    assert account.state is ConfigEntryState.LOADED
    return hub, account


async def _call(hass, service, **data):
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry, code):
    for parcels in entry.runtime_data.coordinator.data.values():
        for parcel in parcels:
            if parcel["barcode"] == code:
                return parcel["contents"]
    raise AssertionError(f"{code} is not published")


def _stored(entry, code):
    return entry.runtime_data.coordinator.contents_store.get(code)


# --- normaliser key order ----------------------------------------------------

KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_tracking_normaliser_orders_contents_after_receiver():
    parcel = normalize_tracking_parcel(_scan(CODE), code=CODE)
    assert list(parcel) == KEYS
    assert parcel["contents"] is None


def test_account_normaliser_orders_contents_after_receiver():
    for outgoing, record in ((False, account_record()), (True, account_sent_record())):
        parcel = normalize_account_parcel(record, outgoing=outgoing)
        assert list(parcel) == KEYS
        assert parcel["contents"] is None


# --- setting, replacing, clearing --------------------------------------------


@pytest.mark.parametrize("kind", ["hub", "account"])
async def test_update_contents_sets_replaces_and_clears(hass, kind):
    entry = await (_setup_hub(hass) if kind == "hub" else _setup_account(hass))
    assert _contents(entry, CODE) is None

    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    assert _contents(entry, CODE) == SHOES
    await _call(hass, "update_contents", tracking_code=CODE, contents="Socks")
    assert _contents(entry, CODE) == "Socks"
    await _call(hass, "update_contents", tracking_code=CODE)
    assert _contents(entry, CODE) is None
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    await _call(hass, "update_contents", tracking_code=CODE, contents="   ")
    assert _contents(entry, CODE) is None


async def test_the_code_is_normalised_and_the_text_trimmed(hass):
    entry = await _setup_hub(hass)
    await _call(
        hass,
        "update_contents",
        tracking_code=f" {CODE.lower()} ",
        contents="  Running \n\t shoes  ",
    )
    assert _contents(entry, CODE) == SHOES


async def test_an_empty_code_is_rejected(hass):
    await _setup_hub(hass)
    with pytest.raises(ServiceValidationError, match="tracking code is required"):
        await _call(hass, "update_contents", tracking_code="  ", contents=SHOES)


async def test_an_untracked_code_can_be_set_on_a_single_entry(hass):
    entry = await _setup_hub(hass)
    await _call(hass, "update_contents", tracking_code=UNTRACKED, contents=SHOES)
    assert _stored(entry, UNTRACKED) == SHOES


async def test_a_stored_value_is_applied_at_the_first_poll(hass, hass_storage):
    entry = _hub([CODE])
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.contents.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.contents.{entry.entry_id}",
        "data": {"by_code": {CODE: SHOES}},
    }
    with patch(GET_PARCEL, new=_fetch()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry, CODE) == SHOES


async def test_an_entry_without_a_store_file_reads_as_empty(hass, hass_storage):
    entry = await _setup_hub(hass)
    assert not [k for k in hass_storage if k.startswith(f"{DOMAIN}.contents")]
    assert _contents(entry, CODE) is None


async def test_the_oldest_code_is_dropped_at_the_cap(hass):
    entry = await _setup_hub(hass)
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        await _call(
            hass, "update_contents", tracking_code=f"C{number:04d}", contents="x"
        )
    assert _stored(entry, "C0000") is None
    assert _stored(entry, "C0001") == "x"
    assert _stored(entry, f"C{CONTENTS_STORE_MAX_CODES:04d}") == "x"


async def test_untrack_drops_the_value_on_the_hub(hass):
    entry = await _setup_hub(hass, [CODE, OTHER_CODE])
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    await _call(hass, "untrack_parcel", tracking_code=CODE)
    assert _stored(entry, CODE) is None


async def test_untrack_works_when_the_hub_is_not_loaded(hass):
    entry = await _setup_hub(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {"tracking_code": CODE}, blocking=True
    )
    assert entry.options[CONF_PARCELS] == []
    entry.mock_state(hass, ConfigEntryState.LOADED)


# --- no poll -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["hub", "account"])
async def test_a_contents_change_never_polls_the_carrier(hass, kind):
    entry = await (_setup_hub(hass) if kind == "hub" else _setup_account(hass))
    coordinator = entry.runtime_data.coordinator
    listeners = MagicMock(wraps=coordinator.async_update_listeners)
    coordinator.async_update_listeners = listeners
    fetch = AsyncMock()
    parcels, info = AsyncMock(), AsyncMock()
    with (
        patch.object(MeestCoordinator, "async_request_refresh", new=AsyncMock()) as r,
        patch(GET_PARCEL, new=fetch),
        patch(f"{ACCOUNT}.async_get_parcels", new=parcels),
        patch(f"{ACCOUNT}.async_get_parcel_info", new=info),
    ):
        await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
        assert listeners.call_count == 1
        await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
        assert listeners.call_count == 1
        await _call(hass, "update_contents", tracking_code=CODE)
        assert listeners.call_count == 2
        await _call(hass, "update_contents", tracking_code=CODE)
        assert listeners.call_count == 2
    r.assert_not_called()
    fetch.assert_not_called()
    parcels.assert_not_called()
    info.assert_not_called()


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup_hub(hass)
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    store = entry.runtime_data.coordinator.contents_store
    with patch.object(store._store, "async_delay_save") as save:
        await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
        await _call(hass, "update_contents", tracking_code=UNTRACKED)
    save.assert_not_called()


# --- track_parcel ------------------------------------------------------------


async def test_a_newly_tracked_code_stores_the_value_and_still_refreshes(hass):
    entry = await _setup_hub(hass, [])
    fetch = _fetch()
    with patch(GET_PARCEL, new=fetch):
        await _call(
            hass,
            "track_parcel",
            tracking_code=CODE.lower(),
            contents=SHOES,
            direction="outgoing",
        )
    fetch.assert_awaited_once_with(CODE)
    assert _contents(entry, CODE) == SHOES


async def test_track_without_contents_leaves_the_store_alone(hass):
    entry = await _setup_hub(hass, [])
    with patch(GET_PARCEL, new=_fetch()):
        await _call(hass, "track_parcel", tracking_code=CODE)
    assert _contents(entry, CODE) is None


async def test_tracking_an_existing_code_with_contents_does_not_poll(hass):
    entry = await _setup_hub(hass)
    coordinator = entry.runtime_data.coordinator
    listeners = MagicMock(wraps=coordinator.async_update_listeners)
    coordinator.async_update_listeners = listeners
    fetch = AsyncMock()
    with (
        patch.object(MeestCoordinator, "async_request_refresh", new=AsyncMock()) as r,
        patch(GET_PARCEL, new=fetch),
    ):
        await _call(hass, "track_parcel", tracking_code=CODE, contents=SHOES)
        await _call(hass, "track_parcel", tracking_code=CODE, contents=SHOES)
    assert _contents(entry, CODE) == SHOES
    assert listeners.call_count == 1
    r.assert_not_called()
    fetch.assert_not_called()


async def test_track_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup_hub(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_code": OTHER_CODE, "contents": SHOES},
            blocking=True,
        )
    entry.mock_state(hass, ConfigEntryState.LOADED)


# --- routing -----------------------------------------------------------------


async def test_every_entry_that_lists_the_code_gets_the_value(hass):
    hub, account = await _setup_both(hass)
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    assert _contents(hub, CODE) == SHOES
    assert _contents(account, CODE) == SHOES


async def test_only_the_entry_listing_the_code_gets_it(hass):
    hub, account = await _setup_both(hass, [HUB_ONLY])
    await _call(hass, "update_contents", tracking_code=HUB_ONLY, contents=SHOES)
    assert _stored(hub, HUB_ONLY) == SHOES
    assert _stored(account, HUB_ONLY) is None


async def test_a_code_no_entry_lists_needs_an_entry_when_several_are_loaded(hass):
    await _setup_both(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "update_contents", tracking_code=UNTRACKED, contents=SHOES)
    assert err.value.translation_key == "update_contents_entry_required"


async def test_config_entry_id_targets_exactly_that_entry(hass):
    hub, account = await _setup_both(hass)
    await _call(
        hass,
        "update_contents",
        tracking_code=UNTRACKED,
        contents=SHOES,
        config_entry_id=account.entry_id,
    )
    assert _stored(account, UNTRACKED) == SHOES
    assert _stored(hub, UNTRACKED) is None


async def test_a_config_entry_id_that_is_not_loaded_is_rejected(hass):
    hub, account = await _setup_both(hass)
    with pytest.raises(ServiceValidationError, match="not a loaded"):
        await _call(
            hass,
            "update_contents",
            tracking_code=CODE,
            contents=SHOES,
            config_entry_id="missing",
        )
    assert _stored(hub, CODE) is None and _stored(account, CODE) is None


async def test_an_unloaded_entry_is_never_targeted(hass):
    hub, account = await _setup_both(hass)
    account.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    assert _stored(hub, CODE) == SHOES
    assert _stored(account, CODE) is None
    with pytest.raises(ServiceValidationError, match="not a loaded"):
        await _call(
            hass,
            "update_contents",
            tracking_code=CODE,
            contents=SHOES,
            config_entry_id=account.entry_id,
        )
    account.mock_state(hass, ConfigEntryState.LOADED)


async def test_nothing_loaded_is_an_error(hass):
    async_setup_services(hass, hub=False)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)


# --- service lifecycle -------------------------------------------------------


async def test_an_account_unloading_keeps_every_service_while_a_hub_loads(hass):
    _hub_entry, account = await _setup_both(hass)
    await hass.config_entries.async_unload(account.entry_id)
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "update_contents")


async def test_the_hub_services_go_with_the_last_hub_but_update_contents_stays(hass):
    hub, account = await _setup_both(hass)
    await hass.config_entries.async_unload(hub.entry_id)
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")
    assert hass.services.has_service(DOMAIN, "update_contents")
    await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_an_account_alone_registers_only_update_contents(hass):
    await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_removing_an_entry_deletes_its_stored_contents(hass):
    entry = await _setup_hub(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(ContentsStore, "async_remove", new=AsyncMock()) as remove:
        await hass.config_entries.async_remove(entry.entry_id)
    remove.assert_awaited_once()


# --- diagnostics, calendar, next delivery ------------------------------------


async def test_diagnostics_redact_contents_for_each_entry_type(hass):
    hub, account = await _setup_both(hass)
    await _call(hass, "update_contents", tracking_code=CODE, contents="Secret shoes")
    for entry in (hub, account):
        text = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
        assert "Secret shoes" not in text
        assert '"contents": "**REDACTED**"' in text


def _window_record(start):
    return account_record(
        estimatedDeliveryDate=None,
        delivery={
            "timeFrom": start.isoformat(),
            "timeTo": (start + timedelta(hours=2)).isoformat(),
        },
    )


async def test_the_calendar_title_prefers_contents_then_the_sender(hass):
    start = dt_util.now() + timedelta(days=1)
    entry = await _setup_account(hass, received=[_window_record(start)])
    calendar = next(
        e
        for e in hass.data["calendar"].entities
        if e.unique_id == f"{entry.entry_id}_deliveries"
    )
    assert calendar.event.summary == "Sender Name"
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    assert calendar.event.summary == SHOES
    await _call(hass, "update_contents", tracking_code=CODE)
    assert calendar.event.summary == "Sender Name"


async def test_the_next_delivery_sensor_shows_the_contents(hass):
    start = dt_util.now() + timedelta(days=1)
    await _setup_account(hass, received=[_window_record(start)])
    state = next(
        s
        for s in hass.states.async_all("sensor")
        if s.entity_id.endswith("next_delivery")
    )
    assert state.attributes["contents"] is None
    await _call(hass, "update_contents", tracking_code=CODE, contents=SHOES)
    state = hass.states.get(state.entity_id)
    assert state.attributes["contents"] == SHOES
