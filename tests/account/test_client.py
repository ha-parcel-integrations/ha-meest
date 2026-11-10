"""Tests for the account client: login, the 401 ladder and response shapes."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.meest.account.client import (
    MAX_REFRESHES,
    MeestAccountApiError,
    MeestAccountClient,
    MeestAccountInvalidCredentials,
    MeestAccountReauthRequired,
)
from custom_components.meest.const import ACCOUNT_PLATFORM
from tests.payloads import account_record

BAD_TOKEN = {
    "status": "error",
    "info": {"fieldName": "", "message": "Bad Token", "messageDetails": "210"},
    "result": "",
}
TOKENS = {"token": "tok-new", "refreshToken": "ref-new", "expiresIn": 3600}


def _ok(result):
    return 200, {"status": "OK", "info": {}, "result": result}


class _Ctx:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, body = item
        return _Ctx(status, body)


def _client(session, **kw):
    return MeestAccountClient(session, token="tok-old", refresh_token="ref-old", **kw)


async def test_login_returns_and_applies_the_token_pair():
    session = _Session(_ok(TOKENS), _ok([account_record()]))
    client = MeestAccountClient(session)

    tokens = await client.async_login("user", "pw")

    assert tokens == {"token": "tok-new", "refresh_token": "ref-new"}
    body = session.calls[0]["json"]
    assert body["login"] == "user" and body["platform"] == ACCOUNT_PLATFORM
    assert "token" not in session.calls[0]["headers"]
    await client.async_get_parcels("receiver")
    # a plain token header, not a bearer credential
    assert session.calls[1]["headers"]["token"] == "tok-new"
    assert "Authorization" not in session.calls[1]["headers"]
    assert session.calls[1]["headers"]["Localization"] == "en"


async def test_login_accepts_tokens_in_a_nested_envelope():
    session = _Session((200, {"data": TOKENS}))
    tokens = await MeestAccountClient(session).async_login("u", "p")
    assert tokens["token"] == "tok-new"


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_login_rejections_are_invalid_credentials(status):
    session = _Session((status, BAD_TOKEN))
    with pytest.raises(MeestAccountInvalidCredentials):
        await MeestAccountClient(session).async_login("u", "p")


async def test_login_error_envelope_is_invalid_credentials():
    session = _Session((200, {"status": "error", "result": ""}))
    with pytest.raises(MeestAccountInvalidCredentials):
        await MeestAccountClient(session).async_login("u", "p")


async def test_login_without_tokens_is_an_api_error_and_warns_once(caplog):
    client = MeestAccountClient(_Session(_ok({"other": 1}), _ok({"other": 1})))
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(MeestAccountApiError) as err:
                await client.async_login("u", "p")
            assert not isinstance(err.value, MeestAccountInvalidCredentials)
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "expected=login" in warnings[0]


async def test_a_401_refreshes_then_retries_the_read_and_stores_the_pair():
    stored = AsyncMock()
    session = _Session((401, BAD_TOKEN), _ok(TOKENS), _ok([account_record()]))
    client = _client(session, token_callback=stored)

    parcels = await client.async_get_parcels("receiver")

    assert parcels[0]["parcelID"] == "P-1001"
    stored.assert_awaited_once_with({"token": "tok-new", "refresh_token": "ref-new"})
    assert session.calls[1]["url"].endswith("app/refreshToken")
    assert session.calls[1]["json"] == {"refreshToken": "ref-old"}
    assert session.calls[2]["headers"]["token"] == "tok-new"


async def test_a_401_after_refreshing_twice_asks_for_reauth():
    session = _Session(
        (401, BAD_TOKEN), _ok(TOKENS), (401, BAD_TOKEN), _ok(TOKENS), (401, BAD_TOKEN)
    )
    with pytest.raises(MeestAccountReauthRequired):
        await _client(session).async_get_parcels("receiver")
    assert MAX_REFRESHES == 2
    assert len(session.calls) == 5  # three reads, two refreshes, then reauth


async def test_a_rejected_refresh_asks_for_reauth_immediately():
    session = _Session((401, BAD_TOKEN), (401, BAD_TOKEN))
    with pytest.raises(MeestAccountReauthRequired):
        await _client(session).async_get_parcels("receiver")
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_a_refresh_rejected_with_any_status_asks_for_reauth(status):
    """Seen live: the refresh call answers a dead token pair with a 400."""
    session = _Session((401, BAD_TOKEN), (status, BAD_TOKEN))
    with pytest.raises(MeestAccountReauthRequired):
        await _client(session).async_get_parcels("receiver")
    assert len(session.calls) == 2


async def test_refresh_without_a_refresh_token_needs_reauth():
    session = _Session((401, BAD_TOKEN))
    client = MeestAccountClient(session, token="t")
    with pytest.raises(MeestAccountReauthRequired):
        await client.async_get_parcels("receiver")


async def test_refresh_works_without_a_callback():
    session = _Session((401, BAD_TOKEN), _ok(TOKENS), _ok([]))
    assert await _client(session).async_get_parcels("sender") == []


async def test_401_is_never_read_as_not_found():
    session = _Session((401, BAD_TOKEN), (401, BAD_TOKEN))
    with pytest.raises(MeestAccountReauthRequired):
        await _client(session).async_get_parcel_info("P-1")


async def test_error_envelope_on_a_read_is_an_api_error_not_reauth():
    session = _Session((200, {"status": "error", "result": ""}))
    with pytest.raises(MeestAccountApiError) as err:
        await _client(session).async_get_parcels("receiver")
    assert not isinstance(err.value, MeestAccountReauthRequired)


@pytest.mark.parametrize("status", [429, 500])
async def test_other_statuses_are_api_errors_with_the_code(status):
    session = _Session((status, {}))
    with pytest.raises(MeestAccountApiError) as err:
        await _client(session).async_get_parcels("receiver")
    assert err.value.status_code == status


async def test_invalid_json_is_an_api_error():
    session = _Session((200, ValueError("bad")))
    with pytest.raises(MeestAccountApiError, match="invalid JSON"):
        await _client(session).async_get_parcels("receiver")


async def test_network_errors_propagate():
    session = _Session(aiohttp.ClientConnectionError("down"))
    with pytest.raises(aiohttp.ClientError):
        await _client(session).async_get_parcels("receiver")


@pytest.mark.parametrize(
    "result",
    [
        [account_record()],
        {"parcelsForRequestedPage": [account_record()], "total": 1},
        {"parcels": [account_record()]},
    ],
)
async def test_list_shapes(result):
    session = _Session(_ok(result))
    assert len(await _client(session).async_get_parcels("receiver")) == 1
    call = session.calls[0]
    assert call["url"].endswith("app/parcelsForDelivery/receiver")
    assert call["params"]["platform"] == ACCOUNT_PLATFORM


@pytest.mark.parametrize("result", ["", None, {}, []])
async def test_empty_results_are_an_empty_inbox(result):
    assert await _client(_Session(_ok(result))).async_get_parcels("receiver") == []


@pytest.mark.parametrize("result", [{"unknown": 1}, 7])
async def test_unfamiliar_list_shapes_warn_once_and_fail(result, caplog):
    client = _client(_Session(_ok(result), _ok(result)))
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(MeestAccountApiError):
                await client.async_get_parcels("receiver")
    assert len([r for r in caplog.records if "expected=list" in r.getMessage()]) == 1


async def test_non_dict_items_are_dropped():
    session = _Session(_ok([account_record(), "junk", 3]))
    assert len(await _client(session).async_get_parcels("receiver")) == 1


async def test_detail_and_history_reads_quote_their_path():
    session = _Session(_ok([{"parcelID": "x"}]), _ok([]), _ok([{"appCode": 1}]))
    client = _client(session)
    assert (await client.async_get_parcel_info("a/b"))["parcelID"] == "x"
    assert await client.async_get_parcel_info("none") is None
    assert await client.async_get_history("N 1") == [{"appCode": 1}]
    assert session.calls[0]["url"].endswith("app/parcelInfo/a%2Fb")
    assert session.calls[2]["url"].endswith("app/trackAPPNew/N%201")
    assert all(c["allow_redirects"] is False for c in session.calls)


async def test_a_redirect_is_reported_as_such():
    """A bounce to a sign-in page must not read as a generic failure."""
    session = _Session((302, None))
    with pytest.raises(MeestAccountApiError) as err:
        await _client(session).async_get_parcels("receiver")
    assert err.value.status_code == 302
    assert "redirected" in str(err.value)
