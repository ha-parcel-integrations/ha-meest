"""Tests for the tracking client: the two steps, retry and the failure map."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.meest.const import MAX_BODY_BYTES
from custom_components.meest.tracking import api
from custom_components.meest.tracking.api import (
    MeestApiError,
    MeestTrackingClient,
    send_code,
)
from tests.payloads import (
    CODE,
    LATE_LABEL,
    NEXT_PAGE_VALUE,
    OTHER_CODE,
    PAGE_VALUE,
    journey,
    lookup_xml,
    page_html,
    row,
)


class _Response:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.content = MagicMock()

        async def _chunks(_size):
            # Small chunks, so a reader that stops after the first one fails.
            for start in range(0, len(body), 7):
                yield body[start : start + 7]

        self.content.iter_chunked = MagicMock(side_effect=_chunks)


class _Ctx:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Serves queued responses per method and records every request."""

    def __init__(self, gets=(), posts=()):
        self.queues = {"get": list(gets), "post": list(posts)}
        self.calls: list[tuple[str, str, dict]] = []

    def _serve(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.queues[method].pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Ctx(item)

    def get(self, url, **kwargs):
        return self._serve("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._serve("post", url, **kwargs)


def _ok(rows, error_code="000"):
    return _Response(200, lookup_xml(rows, error_code))


def _page(value=PAGE_VALUE):
    return _Response(200, page_html(value))


@pytest.fixture(autouse=True)
def _reset():
    api._warned_error_codes.clear()


def test_send_code_strips_hyphens_spaces_and_case():
    assert send_code(" ab-12 3 ") == "AB123"


async def test_lookup_request_shape():
    session = _Session([_page()], [_ok(journey())])
    client = MeestTrackingClient(session)

    result = await client.async_get_parcel(CODE)

    assert len(result["rows"]) == 4
    _, url, kwargs = session.calls[1]
    expected = hashlib.md5(f"{PAGE_VALUE}{CODE}{PAGE_VALUE}".encode()).hexdigest()
    assert f"chk={expected}" in url
    assert f"number={CODE}" in url
    assert "&test&" in url
    assert kwargs["headers"]["Accept"] == "application/xml, text/xml"
    assert kwargs["allow_redirects"] is False
    # the page URL carries the code, and nothing but the value is read from it
    assert session.calls[0][1].endswith(f"/n/{CODE}")


async def test_page_read_is_cached_across_lookups():
    session = _Session([_page()], [_ok(journey()), _ok(journey())])
    client = MeestTrackingClient(session)

    await client.async_get_parcel(CODE)
    await client.async_get_parcel(CODE)

    assert [c[0] for c in session.calls].count("get") == 1


async def test_stale_cache_rereads_the_page(monkeypatch):
    session = _Session([_page(), _page()], [_ok(journey()), _ok(journey())])
    client = MeestTrackingClient(session)
    await client.async_get_parcel(CODE)
    client._page_read_at -= 10**6  # older than the TTL

    await client.async_get_parcel(CODE)

    assert [c[0] for c in session.calls].count("get") == 2


async def test_empty_table_is_not_found_not_an_error():
    session = _Session([_page()], [_ok([])])
    assert await MeestTrackingClient(session).async_get_parcel(CODE) is None


async def test_400_rereads_the_page_and_retries_once():
    session = _Session(
        [_page(), _page(NEXT_PAGE_VALUE)],
        [_Response(400), _ok(journey())],
    )
    client = MeestTrackingClient(session)

    result = await client.async_get_parcel(CODE)

    assert result is not None
    fresh = hashlib.md5(
        f"{NEXT_PAGE_VALUE}{CODE}{NEXT_PAGE_VALUE}".encode()
    ).hexdigest()
    assert f"chk={fresh}" in session.calls[3][1]


async def test_second_400_is_a_transport_error_not_a_loop():
    session = _Session(
        [_page(), _page(NEXT_PAGE_VALUE)], [_Response(400), _Response(400)]
    )
    with pytest.raises(MeestApiError) as err:
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 400
    assert len(session.calls) == 4  # page, lookup, page, lookup — nothing more


async def test_concurrent_400s_share_one_page_read():
    """Two lookups refused on the same stale value read the page once more."""
    old_chk = hashlib.md5(f"{PAGE_VALUE}{CODE}{PAGE_VALUE}".encode()).hexdigest()

    class _Rotating(_Session):
        def post(self, url, **kwargs):
            self.calls.append(("post", url, kwargs))
            if old_chk in url:
                return _Ctx(_Response(400))
            return _Ctx(_ok(journey()))

    session = _Rotating([_page(), _page(NEXT_PAGE_VALUE)])
    client = MeestTrackingClient(session)

    results = await asyncio.gather(
        client.async_get_parcel(CODE), client.async_get_parcel(CODE)
    )

    assert all(results)
    assert [c[0] for c in session.calls].count("get") == 2


async def test_unusable_page_fails_closed():
    session = _Session([_Response(200, b"<html>nothing here</html>")])
    with pytest.raises(MeestApiError, match="page unusable"):
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert not session.queues["post"]


@pytest.mark.parametrize("status", [403, 302, 500])
async def test_page_failures_are_transport_errors(status):
    session = _Session([_Response(status)])
    with pytest.raises(MeestApiError) as err:
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert err.value.status_code == status


async def test_page_429_carries_retry_after():
    session = _Session([_Response(429, headers={"Retry-After": "30"})])
    with pytest.raises(MeestApiError) as err:
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert err.value.retry_after == 30.0


@pytest.mark.parametrize(("header", "expected"), [("soon", None), (None, None)])
async def test_lookup_429_retry_after_variants(header, expected):
    headers = {"Retry-After": header} if header else {}
    session = _Session([_page()], [_Response(429, headers=headers)])
    with pytest.raises(MeestApiError) as err:
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after is expected


@pytest.mark.parametrize("status", [403, 301, 503])
async def test_lookup_failures_are_transport_errors(status):
    session = _Session([_page()], [_Response(status)])
    with pytest.raises(MeestApiError) as err:
        await MeestTrackingClient(session).async_get_parcel(CODE)
    assert err.value.status_code == status


@pytest.mark.parametrize(
    "body",
    [
        b"<html>blocked</html>",
        b"not xml at all",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><return/>',
    ],
)
async def test_non_xml_or_doctype_bodies_are_rejected(body):
    session = _Session([_page()], [_Response(200, body)])
    with pytest.raises(MeestApiError):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_oversized_body_is_rejected():
    session = _Session([_page()], [_Response(200, b"x" * (MAX_BODY_BYTES + 1))])
    with pytest.raises(MeestApiError, match="too large"):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_unexpected_xml_root_is_rejected():
    session = _Session([_page()], [_Response(200, b"<other/>")])
    with pytest.raises(MeestApiError, match="root"):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_error_code_is_inconclusive_and_warns_once(caplog):
    session = _Session(
        [_page()],
        [_ok(journey(), "042"), _ok(journey(), "042")],
    )
    client = MeestTrackingClient(session)
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(MeestApiError, match="declined"):
                await client.async_get_parcel(CODE)
    warnings = [r for r in caplog.records if "result code" in r.message]
    assert len(warnings) == 1
    assert "errors/code=042" in warnings[0].getMessage()
    assert "issues/new?template=unrecognised_status.yml" in warnings[0].getMessage()


async def test_empty_error_code_is_reported_as_empty(caplog):
    body = b"<return><result_table/><errors><code></code></errors></return>"
    session = _Session([_page()], [_Response(200, body)])
    with pytest.raises(MeestApiError, match="empty"):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_mid_journey_number_change_matches_on_sender_number():
    """Final rows have an empty ShipmentNumber and a different Barcode."""
    session = _Session([_page()], [_ok(journey())])
    result = await MeestTrackingClient(session).async_get_parcel(CODE)
    last = result["rows"][-1]
    assert last["ShipmentNumber"] == ""
    assert last["Barcode"] == LATE_LABEL
    assert last["StatusCode"] == "1622"


async def test_transit_reference_only_is_inconclusive():
    partner = [
        row("1315", "2026-10-05 08:00:00", own=OTHER_CODE, transit=CODE),
    ]
    session = _Session([_page()], [_ok(partner)])
    with pytest.raises(MeestApiError, match="another parcel"):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_rows_naming_a_different_number_are_inconclusive():
    session = _Session(
        [_page()], [_ok([row("100", "2026-10-01 09:00:00", own=OTHER_CODE)])]
    )
    with pytest.raises(MeestApiError, match="another parcel"):
        await MeestTrackingClient(session).async_get_parcel(CODE)


async def test_network_errors_propagate():
    session = _Session([aiohttp.ClientConnectionError("down")])
    with pytest.raises(aiohttp.ClientError):
        await MeestTrackingClient(session).async_get_parcel(CODE)
