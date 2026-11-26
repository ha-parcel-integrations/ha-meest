"""Coordinator for the tracking source: one lookup per tracked code."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_DIRECTION,
    DIRECTION_OUTGOING,
    DOMAIN,
)
from ..contents import normalize_code
from ..coordinator import Buckets, MeestCoordinator
from .api import MeestApiError, MeestTrackingClient
from .parcels import normalize_tracking_parcel

_LOGGER = logging.getLogger(__name__)


def tracked_direction(item: dict) -> str:
    """Return the declared direction of one options entry (incoming by default)."""
    return item.get(CONF_DIRECTION) or DEFAULT_DIRECTION


class MeestTrackingCoordinator(MeestCoordinator):
    """Polls each tracked code and publishes the canonical buckets.

    There is no account feed, so the tracked parcels are the codes the user
    entered, each with the direction they declared. A code whose lookup fails
    keeps its last good data instead of vanishing.
    """

    suspend_when_idle = True

    def __init__(
        self, hass: HomeAssistant, client: MeestTrackingClient, entry: ConfigEntry
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(hass, entry, DOMAIN)
        self._client = client
        # code -> last successful raw payload (resets on restart).
        self._raw_cache: dict[str, dict] = {}

    def _tracked(self) -> dict[str, str]:
        """Return the configured codes mapped to their declared direction."""
        return {
            item[CONF_TRACKING_CODE]: tracked_direction(item)
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        }

    async def _async_poll(self) -> Buckets:
        directions = self._tracked()
        codes = list(directions)
        self.known_codes = frozenset(normalize_code(code) for code in directions)
        self._raw_cache = {c: r for c, r in self._raw_cache.items() if c in directions}
        self._delivered_codes &= set(codes)

        # A delivered parcel's scans never change again, so it is dropped from
        # the fetch — not from the options list.
        to_fetch = [code for code in codes if code not in self._delivered_codes]
        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in to_fetch),
            return_exceptions=True,
        )

        raws: dict[str, dict] = {}
        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for code, result in zip(to_fetch, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, (MeestApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                if isinstance(result, MeestApiError) and result.status_code == 429:
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("Meest lookup failed for a tracked code: %s", result)
                if (cached := self._raw_cache.get(code)) is not None:
                    raws[code] = cached
                continue
            if result is None:
                # No scans: unknown, or aged out. Keep what we had, otherwise
                # show a pending placeholder for the code the user asked for.
                raws[code] = self._raw_cache.get(code) or {}
                continue
            self._raw_cache[code] = result
            raws[code] = result

        for code in self._delivered_codes:
            if (cached := self._raw_cache.get(code)) is not None:
                raws[code] = cached

        if saw_429:
            raise UpdateFailed(
                "Meest rate-limited (429)",
                retry_after=self.backoff_seconds(retry_afters),
            )
        self._consecutive_429 = 0
        if to_fetch and errors == len(to_fetch) and not raws:
            raise UpdateFailed("Meest unreachable for all tracked parcels")

        include_history = self.include_history
        parcels = {
            code: self._apply_contents(
                normalize_tracking_parcel(
                    raw, code=code, include_history=include_history
                )
            )
            for code, raw in raws.items()
        }
        self._delivered_codes = {c for c, p in parcels.items() if p["delivered"]}
        incoming = [
            p for c, p in parcels.items() if directions[c] != DIRECTION_OUTGOING
        ]
        outgoing = [
            p for c, p in parcels.items() if directions[c] == DIRECTION_OUTGOING
        ]

        if not to_fetch or errors < len(to_fetch):
            self._stamp_success()
        return self._publish(incoming, outgoing)
