"""Client for the user's own Meest account.

The user's login is the only credential: a token pair is exchanged for it once,
the tokens (never the password) are stored, and the client refreshes them on
its own and calls back so the entry always holds the current pair.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from ..const import ACCOUNT_API_URL, ACCOUNT_APP_VERSION, ACCOUNT_PLATFORM
from ..status import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

# A rejected token is refreshed and the read retried, at most this often,
# before the user is asked to sign in again.
MAX_REFRESHES = 2

TokenCallback = Callable[[dict[str, str]], Awaitable[None]]

_LIST_KEYS = ("parcelsForRequestedPage", "parcels", "items", "result")
_warned_shapes: set[str] = set()


class MeestAccountApiError(Exception):
    """An unexpected account response; carries no response body."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Store safe failure metadata only."""
        super().__init__(detail)
        self.status_code = status_code


class MeestAccountInvalidCredentials(MeestAccountApiError):
    """The supplied login was rejected."""


class MeestAccountReauthRequired(MeestAccountApiError):
    """The stored tokens cannot be refreshed."""


class _Unauthorized(Exception):
    """A rejected token on a read; resolved by the refresh ladder."""


def _unwrap(body: Any) -> Any:
    """Return the payload of a response envelope, raising on an error envelope."""
    if isinstance(body, dict) and "status" in body:
        if str(body["status"]).lower() == "error":
            raise MeestAccountApiError("account API reported an error")
        if "result" in body:
            return body["result"]
    return body


def _as_list(result: Any) -> list[dict[str, Any]]:
    """Return the dict items of a list-shaped result; empty for no data."""
    if result in (None, "", {}):
        return []
    if isinstance(result, dict):
        for key in _LIST_KEYS:
            if isinstance(result.get(key), list):
                result = result[key]
                break
        else:
            _warn_shape("list", result)
            raise MeestAccountApiError("unexpected list response")
    if not isinstance(result, list):
        _warn_shape("list", result)
        raise MeestAccountApiError("unexpected list response")
    return [item for item in result if isinstance(item, dict)]


def _warn_shape(kind: str, result: Any) -> None:
    if kind in _warned_shapes:
        return
    _warned_shapes.add(kind)
    keys = sorted(result) if isinstance(result, dict) else type(result).__name__
    _LOGGER.warning(
        "Meest account answered in a shape this integration has not seen. Open "
        "an issue and paste this line: %s\n  expected=%s keys=%s",
        NEW_ISSUE_URL,
        kind,
        keys,
    )


def _tokens(result: Any) -> dict[str, str]:
    """Extract the only token fields that are persisted."""
    candidates = [result]
    if isinstance(result, dict):
        candidates += [
            result[key] for key in ("result", "data", "response") if key in result
        ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        token = candidate.get("token")
        refresh = candidate.get("refreshToken")
        if isinstance(token, str) and token and isinstance(refresh, str) and refresh:
            return {"token": token, "refresh_token": refresh}
    _warn_shape("login", result)
    raise MeestAccountApiError("missing account tokens")


class MeestAccountClient:
    """Authenticated reads against the account API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialise the client with entry-owned tokens and a persistence hook."""
        self._session = session
        self._token = token
        self._refresh_token = refresh_token
        self._token_callback = token_callback

    def _headers(self) -> dict[str, str]:
        # The token travels in a plain header, not as a bearer credential.
        headers = {"Content-Type": "application/json", "Localization": "en"}
        if self._token:
            headers["token"] = self._token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        credential: bool = False,
    ) -> Any:
        async with self._session.request(
            method,
            f"{ACCOUNT_API_URL}{path}",
            json=json,
            params=params,
            headers=self._headers(),
            allow_redirects=False,
        ) as response:
            status = response.status
            if status in (401, 403) or (credential and status == 400):
                if credential:
                    # Rejecting a login or a refresh token is answered with a
                    # 400 as readily as with a 401.
                    raise MeestAccountInvalidCredentials(
                        "credential rejected", status_code=status
                    )
                # A rejected token and an unknown parcel answer identically, so
                # this is never read as "not found".
                raise _Unauthorized
            if 300 <= status < 400:
                # allow_redirects is off: a redirect means the request was
                # bounced to a sign-in page rather than answered.
                raise MeestAccountApiError(
                    "account API redirected instead of answering",
                    status_code=status,
                )
            if status != 200:
                raise MeestAccountApiError(
                    "account API request failed", status_code=status
                )
            try:
                body = await response.json(content_type=None)
            except ValueError as err:
                raise MeestAccountApiError("account API returned invalid JSON") from err
        try:
            return _unwrap(body)
        except MeestAccountApiError:
            if credential:
                raise MeestAccountInvalidCredentials("credential rejected") from None
            raise

    async def _authed(self, method: str, path: str, **kwargs: Any) -> Any:
        """Run an authenticated call, refreshing a rejected token up to twice."""
        for attempt in range(MAX_REFRESHES + 1):
            try:
                return await self._request(method, path, **kwargs)
            except _Unauthorized:
                if attempt == MAX_REFRESHES:
                    raise MeestAccountReauthRequired(
                        "account token rejected", status_code=401
                    ) from None
                await self.async_refresh()
        raise AssertionError  # pragma: no cover

    async def async_login(self, login: str, password: str) -> dict[str, str]:
        """Authenticate once; callers persist only the returned token pair."""
        tokens = _tokens(
            await self._request(
                "POST",
                "app/login",
                json={
                    "login": login,
                    "password": password,
                    "osVersion": "14",
                    "appVersion": ACCOUNT_APP_VERSION,
                    "platform": ACCOUNT_PLATFORM,
                },
                credential=True,
            )
        )
        self._token = tokens["token"]
        self._refresh_token = tokens["refresh_token"]
        return tokens

    async def async_refresh(self) -> dict[str, str]:
        """Rotate the token pair and tell the entry owner to store it."""
        if not self._refresh_token:
            raise MeestAccountReauthRequired("no refresh token")
        try:
            result = await self._request(
                "POST",
                "app/refreshToken",
                json={"refreshToken": self._refresh_token},
                credential=True,
            )
        except MeestAccountInvalidCredentials as err:
            raise MeestAccountReauthRequired(
                "refresh token rejected", status_code=err.status_code
            ) from None
        tokens = _tokens(result)
        self._token = tokens["token"]
        self._refresh_token = tokens["refresh_token"]
        if self._token_callback:
            await self._token_callback(tokens)
        return tokens

    async def async_get_parcels(self, mode: str) -> list[dict[str, Any]]:
        """Return the account's parcels for one of the app's fetch modes."""
        result = await self._authed(
            "GET",
            f"app/parcelsForDelivery/{mode}",
            params={"appVersion": ACCOUNT_APP_VERSION, "platform": ACCOUNT_PLATFORM},
        )
        return _as_list(result)

    async def async_get_parcel_info(self, parcel_id: str) -> dict[str, Any] | None:
        """Return one parcel's detail record, or ``None`` when it has none."""
        items = _as_list(
            await self._authed(
                "GET", f"app/parcelInfo/{quote(str(parcel_id), safe='')}"
            )
        )
        return items[0] if items else None

    async def async_get_history(self, number: str) -> list[dict[str, Any]]:
        """Return one parcel's event history."""
        return _as_list(
            await self._authed("GET", f"app/trackAPPNew/{quote(str(number), safe='')}")
        )
