"""Shared coordinator behaviour: scheduling, events and the published buckets.

Each source's coordinator fetches and normalises; everything after that — the
split into incoming/outgoing and active/delivered, the events, the dynamic
polling tier — is the same, so it lives here and the two sources cannot drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INCLUDE_HISTORY,
    DEFAULT_INCLUDE_HISTORY,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .contents import ContentsStore
from .events import (
    fire_incoming_change_events,
    fire_outgoing_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from .parcels import apply_delivered_filter, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)

# Base for the 429 backoff when the carrier's response carries no
# ``Retry-After`` of its own: ``BACKOFF_BASE_SECONDS * 2**consecutive_429``,
# capped at ``BACKOFF_CAP_SECONDS``.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

type Buckets = dict[str, list[dict]]


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    return (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int:
    """Hot while an incoming parcel is out for delivery near its window."""
    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES
    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling. Otherwise the naive next-due time is
    clamped forward to the next anchor whenever it would land inside the quiet
    window — including when ``now`` itself is already inside it.
    """
    if tier_minutes is None:
        return None
    if _in_quiet_window(now):
        return _next_anchor(now) - now
    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class MeestCoordinator(DataUpdateCoordinator[Buckets]):
    """Base for both sources; publishes four buckets.

    ``data`` is ``{incoming_active, incoming_delivered, outgoing_active,
    outgoing_delivered}``. A subclass implements :meth:`_async_poll` and ends
    it with :meth:`_publish`.
    """

    config_entry: ConfigEntry
    # An account inbox discovers new parcels by polling, so it never stops;
    # a code list has nothing to poll once everything is delivered.
    suspend_when_idle = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, name: str) -> None:
        """Initialise the coordinator at the hot cadence for a prompt first poll."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=name,
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self.contents_store = ContentsStore(hass, entry.entry_id)
        # Every code this entry lists, delivered ones past the retention filter
        # included: update_contents asks which entry a code belongs to.
        self.known_codes: frozenset[str] = frozenset()
        self._consecutive_429 = 0
        self._current_tier_minutes: int | None = None
        # ``None`` on the first refresh suppresses events for parcels that
        # already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        self._cached_device_id: str | None = None
        self._delivered_codes: set[str] = set()
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Codes currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

    @property
    def include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _apply_contents(self, parcel: dict) -> dict:
        """Put the user's own contents text over the carrier's, when there is one."""
        user_value = self.contents_store.get(parcel["barcode"])
        if user_value is not None:
            parcel["contents"] = user_value
        return parcel

    @callback
    def async_reapply_contents(self) -> None:
        """Re-read the contents store into the published parcels, without a poll."""
        for parcels in (self.data or {}).values():
            for parcel in parcels:
                parcel["contents"] = None
                self._apply_contents(parcel)
        self.async_update_listeners()

    async def _async_update_data(self) -> Buckets:
        try:
            return await self._async_poll()
        except Exception:
            # The base class keeps the last interval after a failure, which
            # after an overnight poll is the whole quiet window.
            now = dt_util.now()
            self.update_interval = _next_update_interval(
                now,
                _hottest_tier_minutes(
                    (self.data or {}).get("incoming_active", []), now
                ),
                self.config_entry.entry_id,
            )
            raise

    async def _async_poll(self) -> Buckets:
        raise NotImplementedError

    def _publish(self, incoming: list[dict], outgoing: list[dict]) -> Buckets:
        """Split, sort, fire events and reschedule from one cycle's parcels."""
        active = [p for p in incoming if not p["delivered"]]
        delivered_sorted = sort_parcels_by_ts(
            [p for p in incoming if p["delivered"]], "delivered_at", descending=True
        )
        incoming_active = sort_parcels_by_ts(active, "planned_from")
        out_active = sort_parcels_by_ts(
            [p for p in outgoing if not p["delivered"]], "planned_from"
        )
        out_delivered_sorted = sort_parcels_by_ts(
            [p for p in outgoing if p["delivered"]], "delivered_at", descending=True
        )

        # Events and remembered state cover every parcel; the retention option
        # only limits what the delivered sensors show.
        seen_incoming = incoming_active + delivered_sorted
        fire_incoming_change_events(
            self.hass,
            seen_incoming,
            self._known_state,
            self._known_delivery_times,
            self._device_id(),
        )
        self._known_state = snapshot_states(seen_incoming)
        self._known_delivery_times = snapshot_delivery_times(seen_incoming)
        seen_outgoing = out_active + out_delivered_sorted
        fire_outgoing_change_events(
            self.hass, seen_outgoing, self._known_outgoing_state, self._device_id()
        )
        self._known_outgoing_state = snapshot_states(seen_outgoing)

        now = dt_util.now()
        if self.suspend_when_idle and not incoming_active and not out_active:
            self._current_tier_minutes = None
        else:
            self._current_tier_minutes = _hottest_tier_minutes(incoming_active, now)
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )

        return {
            "incoming_active": incoming_active,
            "incoming_delivered": apply_delivered_filter(
                delivered_sorted, self.config_entry
            ),
            "outgoing_active": out_active,
            "outgoing_delivered": apply_delivered_filter(
                out_delivered_sorted, self.config_entry
            ),
        }

    def _stamp_success(self) -> None:
        self.last_success_time = datetime.now(UTC)

    def backoff_seconds(self, retry_afters: list[float]) -> float:
        """Count a 429 and return how long the whole poll should back off."""
        self._consecutive_429 += 1
        if retry_afters:
            return max(retry_afters)
        return min(BACKOFF_BASE_SECONDS * 2**self._consecutive_429, BACKOFF_CAP_SECONDS)


def bucket(data: Any, name: str) -> list[dict]:
    """Read one published bucket, tolerating an empty coordinator."""
    return (data or {}).get(name, [])
