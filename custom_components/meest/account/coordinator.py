"""Coordinator for the account source: the user's inbox, both directions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import DOMAIN
from ..contents import normalize_code
from ..coordinator import Buckets, MeestCoordinator
from ..status import clean_code
from .client import MeestAccountApiError, MeestAccountClient, MeestAccountReauthRequired
from .parcels import (
    MODE_RECEIVER,
    MODE_SENDER,
    check_direction,
    normalize_account_parcel,
    record_status_unresolved,
)

_LOGGER = logging.getLogger(__name__)


class MeestAccountCoordinator(MeestCoordinator):
    """Reads both directions of the inbox; an account never suspends polling."""

    def __init__(
        self, hass: HomeAssistant, client: MeestAccountClient, entry: ConfigEntry
    ) -> None:
        """Initialise a continuously polled account coordinator."""
        super().__init__(hass, entry, f"{DOMAIN} account")
        self._client = client

    async def _async_enrich(
        self, record: dict[str, Any], *, include_history: bool
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Add the detail record and, when asked for, the event history.

        Both are best-effort: a failure leaves the list record as it was.
        """
        parcel_id = clean_code(record.get("parcelID"))
        number = clean_code(record.get("parcelNumber")) or clean_code(
            record.get("barCode")
        )
        detail: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        if parcel_id and not record.get("factDeliveryDate"):
            try:
                detail = await self._client.async_get_parcel_info(parcel_id)
            except MeestAccountApiError as err:
                if isinstance(err, MeestAccountReauthRequired):
                    raise
                _LOGGER.debug("Meest parcel detail unavailable: %s", err)
        # The status ladder needs the history when the record alone cannot say
        # what the parcel is doing, whether or not the attribute was asked for.
        if number and (
            include_history
            or record_status_unresolved({**record, **detail} if detail else record)
        ):
            try:
                events = await self._client.async_get_history(number)
            except MeestAccountApiError as err:
                if isinstance(err, MeestAccountReauthRequired):
                    raise
                _LOGGER.debug("Meest parcel history unavailable: %s", err)
        return ({**record, **detail} if detail else record), events

    async def _async_poll(self) -> Buckets:
        try:
            received = await self._client.async_get_parcels(MODE_RECEIVER)
            sent = await self._client.async_get_parcels(MODE_SENDER)
            # An account that answers with no parcels at all is otherwise
            # indistinguishable from one the integration cannot read.
            _LOGGER.debug(
                "Meest account returned %s incoming and %s outgoing records",
                len(received),
                len(sent),
            )
            for record in received:
                check_direction(record, MODE_RECEIVER)
            for record in sent:
                check_direction(record, MODE_SENDER)
            # A parcel you both send and receive is listed twice; keep the
            # receiver-side record.
            received_ids = {
                clean_code(r.get("parcelID")) for r in received if r.get("parcelID")
            }
            sent = [
                r
                for r in sent
                if not r.get("parcelID")
                or clean_code(r["parcelID"]) not in received_ids
            ]

            include_history = self.include_history
            records = [(r, False) for r in received] + [(r, True) for r in sent]
            enriched = await asyncio.gather(
                *(
                    self._async_enrich(record, include_history=include_history)
                    for record, _ in records
                )
            )
        except MeestAccountReauthRequired as err:
            raise ConfigEntryAuthFailed("Meest account needs reauthentication") from err
        except MeestAccountApiError as err:
            if err.status_code == 429:
                raise UpdateFailed(
                    "Meest rate-limited (429)", retry_after=self.backoff_seconds([])
                ) from err
            # The detail and status are safe metadata and carry no response
            # body; without them the log says nothing a user can act on.
            where = f" (HTTP {err.status_code})" if err.status_code else ""
            raise UpdateFailed(
                f"Unable to update the Meest account: {err}{where}"
            ) from err
        self._consecutive_429 = 0

        incoming: list[dict] = []
        outgoing: list[dict] = []
        for (_, is_outgoing), (record, events) in zip(records, enriched, strict=True):
            parcel = self._apply_contents(
                normalize_account_parcel(
                    record,
                    outgoing=is_outgoing,
                    events=events,
                    include_history=include_history,
                )
            )
            (outgoing if is_outgoing else incoming).append(parcel)

        self.known_codes = frozenset(
            normalize_code(parcel["barcode"])
            for parcel in (*incoming, *outgoing)
            if parcel["barcode"]
        )
        self._stamp_success()
        return self._publish(incoming, outgoing)
