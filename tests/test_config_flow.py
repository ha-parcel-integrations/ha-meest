"""Tests for the Meest config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meest.account.client import (
    MeestAccountApiError,
    MeestAccountInvalidCredentials,
)
from custom_components.meest.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.meest.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from tests.payloads import CODE, OTHER_CODE

LOGIN = "User@Example.test"
LOGIN_METHOD = "custom_components.meest.account.client.MeestAccountClient.async_login"
TOKENS = {"token": "tok", "refresh_token": "ref"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" ab 12-3 ", "AB12-3"), ("", ""), (None, "")],
)
def test_normalize_keeps_hyphens_and_uppercases(raw, expected):
    assert normalize_tracking_code(raw) == expected


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code(CODE)
    assert valid_tracking_code("201839638567")
    assert valid_tracking_code("723-3993703")
    assert valid_tracking_code("12345")
    assert not valid_tracking_code("")


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["account", "tracking"]
    return result


async def test_tracking_source_creates_a_hub_without_input(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source": "tracking"}
    assert result["options"][CONF_PARCELS] == []
    assert result["options"]["include_history"] is False


async def test_a_second_tracking_hub_is_refused(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id="tracking", data={"source": "tracking"}
    ).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_account_source_stores_tokens_never_the_password(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["step_id"] == "account"
    with (
        patch(LOGIN_METHOD, new=AsyncMock(return_value=TOKENS)),
        patch("custom_components.meest.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"login": f" {LOGIN} ", "password": "hunter2"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Meest ({LOGIN})"
    assert result["data"] == {
        "source": "account",
        "login": LOGIN,
        "token": "tok",
        "refresh_token": "ref",
    }
    assert "hunter2" not in str(result["data"])
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == f"account:{LOGIN.lower()}"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (MeestAccountInvalidCredentials("x"), "invalid_auth"),
        (MeestAccountApiError("x"), "cannot_connect"),
    ],
)
async def test_account_errors_are_shown(hass, error, key):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch(LOGIN_METHOD, new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"login": LOGIN, "password": "pw"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_blank_account_credentials_are_refused(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"login": "  ", "password": "pw"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_the_same_account_cannot_be_added_twice_in_another_case(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id=f"account:{LOGIN.lower()}", data={"source": "account"}
    ).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch(LOGIN_METHOD, new=AsyncMock(return_value=TOKENS)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"login": LOGIN, "password": "pw"}
        )
    assert result["type"] is FlowResultType.ABORT


def _account_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{LOGIN.lower()}",
        data={
            "source": "account",
            "login": LOGIN,
            "token": "old",
            "refresh_token": "old",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_replaces_only_the_tokens(hass):
    entry = _account_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["login"] == LOGIN
    with (
        patch(LOGIN_METHOD, new=AsyncMock(return_value=TOKENS)) as login,
        patch("custom_components.meest.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    login.assert_awaited_once_with(LOGIN, "new")
    assert entry.data["token"] == "tok" and entry.data["login"] == LOGIN
    assert "password" not in entry.data


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (MeestAccountInvalidCredentials("x"), "invalid_auth"),
        (MeestAccountApiError("x"), "cannot_connect"),
    ],
)
async def test_reauth_errors(hass, error, key):
    entry = _account_entry(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(LOGIN_METHOD, new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "bad"}
        )
    assert result["errors"] == {"base": key}


def _tracking_entry(hass, parcels=()) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={"source": "tracking"},
        options={CONF_PARCELS: list(parcels), "include_history": False},
    )
    entry.add_to_hass(hass)
    return entry


async def _options(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "incoming_parcels",
        "outgoing_parcels",
        "settings",
    ]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_incoming_and_outgoing_lists_edit_their_own_direction(hass):
    entry = _tracking_entry(
        hass,
        [
            {CONF_TRACKING_CODE: CODE},
            {CONF_TRACKING_CODE: OTHER_CODE, CONF_DIRECTION: "outgoing"},
        ],
    )
    result = await _options(hass, entry, "incoming_parcels")
    assert result["step_id"] == "incoming_parcels"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"tracking_codes": [" ab12-34567 ", "AB12-34567", "NEW000001"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: OTHER_CODE, CONF_DIRECTION: "outgoing"},
        {CONF_TRACKING_CODE: "AB12-34567", CONF_DIRECTION: "incoming"},
        {CONF_TRACKING_CODE: "NEW000001", CONF_DIRECTION: "incoming"},
    ]


async def test_refiling_a_code_under_the_other_direction_moves_it(hass):
    entry = _tracking_entry(hass, [{CONF_TRACKING_CODE: CODE}])
    result = await _options(hass, entry, "outgoing_parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": [CODE]}
    )
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: CODE, CONF_DIRECTION: "outgoing"}
    ]


async def test_clearing_the_list_is_allowed(hass):
    entry = _tracking_entry(hass, [{CONF_TRACKING_CODE: CODE}])
    result = await _options(hass, entry, "incoming_parcels")
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options[CONF_PARCELS] == []


async def test_settings_keep_the_parcels(hass):
    entry = _tracking_entry(hass, [{CONF_TRACKING_CODE: CODE}])
    result = await _options(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "delivered_filter_type": "parcels",
            "delivered_filter_amount": 5,
            "include_history": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: CODE}]
    assert entry.options["delivered_filter_amount"] == 5
    assert entry.options["include_history"] is True


async def test_an_account_entry_goes_straight_to_settings(hass):
    entry = _account_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
