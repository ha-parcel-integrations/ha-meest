"""Client for the public tracking lookup.

Each lookup session starts with a read of the tracker page, then the lookup
itself answers in XML. Nothing else on the page is parsed and no parcel data is
taken from it. No account, cookie or key is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import aiohttp

from ..const import (
    MAX_BODY_BYTES,
    PAGE_READ_TTL_SECONDS,
    TRACKER_LOOKUP_URL,
    TRACKER_PAGE_URL,
)
from ..status import NEW_ISSUE_URL
from .parcels import match_rows

_LOGGER = logging.getLogger(__name__)

_PAGE_PATTERN = re.compile(rb"var salt = '([0-9a-f]{32})'")
_ACCEPT = {"Accept": "application/xml, text/xml"}

_warned_error_codes: set[str] = set()


class MeestApiError(Exception):
    """Raised when a lookup fails or cannot be trusted; the caller keeps last-good."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Meest API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def send_code(code: str) -> str:
    """Return the code as the lookup wants it: upper-case, no spaces or hyphens."""
    return re.sub(r"[\s-]+", "", code.upper())


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise MeestApiError("response too large")
    return bytes(body)


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None  # an HTTP-date, not seconds; the caller's own backoff applies


class MeestTrackingClient:
    """Client for the keyless tracking lookup."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._page_value: str | None = None
        self._page_read_at = 0.0
        self._lock = asyncio.Lock()

    async def _async_page_value(self, code: str, *, stale: str | None = None) -> str:
        """Return the cached page read, reading the page again when needed.

        ``stale`` is the read the lookup just refused: a concurrent lookup may
        already have replaced it, in which case no second page read happens.
        """
        async with self._lock:
            fresh = (
                self._page_value is not None
                and self._page_value != stale
                and time.monotonic() - self._page_read_at < PAGE_READ_TTL_SECONDS
            )
            if fresh:
                return self._page_value  # type: ignore[return-value]
            url = TRACKER_PAGE_URL.format(code=quote(code, safe=""))
            async with self._session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise MeestApiError(
                        f"tracker page HTTP {response.status}",
                        status_code=response.status,
                        retry_after=_retry_after(response)
                        if response.status == 429
                        else None,
                    )
                body = await _read_capped(response)
            # Fail closed: only this one anchored pattern is ever read.
            match = _PAGE_PATTERN.search(body)
            if match is None:
                raise MeestApiError("tracker page unusable")
            self._page_value = match.group(1).decode()
            self._page_read_at = time.monotonic()
            return self._page_value

    async def _async_lookup(self, code: str, page_value: str) -> tuple[int, bytes]:
        proof = hashlib.md5(
            f"{page_value}{code}{page_value}".encode(), usedforsecurity=False
        ).hexdigest()
        url = TRACKER_LOOKUP_URL.format(code=quote(code, safe=""), chk=proof)
        async with self._session.post(
            url, headers=_ACCEPT, allow_redirects=False
        ) as response:
            if response.status == 400:
                return 400, b""
            if response.status != 200:
                raise MeestApiError(
                    f"HTTP {response.status}",
                    status_code=response.status,
                    retry_after=_retry_after(response)
                    if response.status == 429
                    else None,
                )
            return 200, await _read_capped(response)

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's scans.

        Returns ``{"rows": [...]}``, or ``None`` when the lookup has no scans —
        which is both an unknown number and one whose history has aged out.
        Anything inconclusive raises :class:`MeestApiError`; network errors
        propagate as ``aiohttp.ClientError``.
        """
        code = send_code(tracking_code)
        page_value = await self._async_page_value(code)
        status, body = await self._async_lookup(code, page_value)
        if status == 400:
            # Read the page again and retry once. A second
            # refusal on a fresh value is a transport failure, never a loop.
            page_value = await self._async_page_value(code, stale=page_value)
            status, body = await self._async_lookup(code, page_value)
            if status == 400:
                raise MeestApiError("lookup refused twice", status_code=400)

        rows = _parse(body)
        if not rows:
            return None
        accepted, own = match_rows(rows, tracking_code)
        if not accepted or not own:
            raise MeestApiError("scans belong to another parcel")
        return {"rows": accepted}


def _parse(body: bytes) -> list[dict[str, str]]:
    """Parse the lookup XML into plain row dicts; raise when it is not usable."""
    # A doctype is never legitimate here, and refusing it keeps entity
    # expansion out of the parser entirely.
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise MeestApiError("unexpected markup in response")
    try:
        root = ET.fromstring(body)  # noqa: S314 - doctype refused above
    except ET.ParseError as err:
        raise MeestApiError("response is not XML") from err
    if root.tag != "return":
        raise MeestApiError("unexpected response root")
    error_code = (root.findtext("errors/code") or "").strip()
    if error_code != "000":
        if error_code not in _warned_error_codes:
            _warned_error_codes.add(error_code)
            _LOGGER.warning(
                "Meest lookup answered with an unfamiliar result code. Open an "
                "issue and paste this line: %s\n  errors/code=%s",
                NEW_ISSUE_URL,
                error_code or "(empty)",
            )
        raise MeestApiError(f"lookup declined (code {error_code or 'empty'})")
    return [
        {child.tag: (child.text or "") for child in item}
        for item in root.findall("result_table/items")
    ]
