"""Normaliser for the account source.

The record shapes here are reconstructed, not yet seen on a real account, so
everything is read defensively and the first record's structure is logged once.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..const import HISTORY_MAX_EVENTS, ParcelStatus
from ..parcels import build_parcel, parse_iso
from ..status import (
    NEW_ISSUE_URL,
    NON_MOVEMENT_CODES,
    clean_code,
    lookup,
    map_app_code,
    warn_unmapped,
)

_LOGGER = logging.getLogger(__name__)

# The list call takes one of the app's own filters, not a role name: "in" and
# "out" are what it understands, and an unknown value answers 200 with nothing.
MODE_RECEIVER = "in"
MODE_SENDER = "out"

_TIME_OF_DAY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

_first_sighting_warned = False
# ``direction`` int -> the modes that returned it. Which int means which way is
# unconfirmed, so a value seen under both modes is the sign they disagree.
_direction_modes: dict[Any, set[str]] = {}
_direction_warned = False


def _block(record: dict[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> str | None:
    """Return a string timestamp when it parses as ISO, else ``None``."""
    if isinstance(value, str) and parse_iso(value.strip()) is not None:
        return value.strip()
    return None


def _combine(day: str | None, clock: Any) -> str | None:
    """Join a date and a time-of-day into one ISO timestamp, if both are usable."""
    if not day or not isinstance(clock, str):
        return None
    match = _TIME_OF_DAY.match(clock.strip())
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return f"{day[:10]}T{int(hours):02d}:{minutes}:{seconds or '00'}"


def _window(
    block: dict[str, Any], fallback_day: str | None
) -> tuple[str | None, str | None]:
    """Read ``timeFrom``/``timeTo``: full timestamps, or times of day on a date."""
    day = _timestamp(block.get("date")) or fallback_day
    bounds: list[str | None] = []
    for key in ("timeFrom", "timeTo"):
        value = block.get(key)
        bounds.append(_timestamp(value) or _combine(day, value))
    planned_from, planned_to = bounds
    if planned_from and planned_to and parse_iso(planned_from) == parse_iso(planned_to):
        planned_to = None
    return planned_from, planned_to


def _branch_name(block: dict[str, Any]) -> str | None:
    branch = block.get("branch")
    if isinstance(branch, str):
        return branch.strip() or None
    if isinstance(branch, dict):
        for key in ("name", "title"):
            if isinstance(branch.get(key), str) and branch[key].strip():
                return branch[key].strip()
    return None


def _status_from_events(events: list[dict[str, Any]]) -> ParcelStatus | None:
    """Newest event that maps, preferring its fine code over the coarse ordinal.

    A newer ``registered`` loses to an older event that got further, for the
    same reason as in the tracking source.
    """
    newest: ParcelStatus | None = None
    for event in reversed(_sorted_events(events)):
        mapped = None
        code = clean_code(event.get("statusCode"))
        if code is not None and code not in NON_MOVEMENT_CODES:
            mapped = lookup(code)
        if mapped is None:
            mapped = map_app_code(event.get("appCode"))
        if mapped is None:
            continue
        if mapped is not ParcelStatus.REGISTERED:
            return mapped
        if newest is None:
            newest = mapped
    return newest


def _event_time(event: dict[str, Any]) -> str | None:
    return _timestamp(event.get("eventDateTime"))


def _sorted_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dated = [e for e in events if _event_time(e)]
    # the list is already filtered to events whose timestamp parses
    dated.sort(key=lambda e: parse_iso(_event_time(e)))  # type: ignore[arg-type, return-value]
    return dated


def _build_history(events: list[dict[str, Any]]) -> list[dict]:
    history: list[dict] = []
    for event in _sorted_events(events):
        code = clean_code(event.get("statusCode"))
        mapped = lookup(code) if code not in NON_MOVEMENT_CODES else None
        if mapped is None:
            mapped = map_app_code(event.get("appCode"))
        place = ", ".join(
            str(event[k]).strip()
            for k in ("city", "country")
            if isinstance(event.get(k), str) and event[k].strip()
        )
        wording = event.get("messages")
        wording = wording.strip() if isinstance(wording, str) else None
        history.append(
            {
                "timestamp": _event_time(event),
                "status": mapped,
                "raw_status": f"{wording} ({place})" if wording and place else wording,
            }
        )
    return history[-HISTORY_MAX_EVENTS:]


def _warn_first_sighting(record: dict[str, Any]) -> None:
    """Log the structure of the first record, so a real account can confirm it."""
    global _first_sighting_warned  # noqa: PLW0603
    if _first_sighting_warned:
        return
    _first_sighting_warned = True
    nested = {
        key: sorted(record[key])
        for key in ("delivery", "sending")
        if isinstance(record.get(key), dict)
    }
    _LOGGER.warning(
        "Meest account parcels have not been confirmed against a real account "
        "yet. If something looks wrong, open an issue and paste this line: "
        "%s\n  keys=%s nested=%s",
        NEW_ISSUE_URL,
        sorted(record),
        nested,
    )


def check_direction(record: dict[str, Any], mode: str) -> None:
    """Corroborate the mode with the record's own ``direction`` int, once."""
    global _direction_warned  # noqa: PLW0603
    value = record.get("direction")
    if value is None or isinstance(value, bool):
        return
    modes = _direction_modes.setdefault(value, set())
    modes.add(mode)
    if len(modes) > 1 and not _direction_warned:
        _direction_warned = True
        _LOGGER.warning(
            "Meest account parcels disagree about direction: the same value is "
            "returned for both receiving and sending. Open an issue and paste "
            "this line: %s\n  direction=%s",
            NEW_ISSUE_URL,
            value,
        )


def record_status_unresolved(record: dict[str, Any]) -> bool:
    """Whether the record's own fields leave its status unknown.

    The contract's ladder falls back to the newest mappable history event, so
    an unresolved record needs its history fetched even when the user did not
    ask for the history attribute.
    """
    code = clean_code(record.get("statusCode"))
    if code is not None and code not in NON_MOVEMENT_CODES and lookup(code) is not None:
        return False
    return map_app_code(record.get("appCode")) is None


def normalize_account_parcel(
    record: dict[str, Any],
    *,
    outgoing: bool,
    events: list[dict[str, Any]] | None = None,
    include_history: bool = False,
) -> dict:
    """Return the canonical parcel for one account record.

    The direction comes from the mode that returned the record, which picks
    whether the delivery or the sending block fills the planned window.
    ``weight`` and ``dimensions`` stay ``None`` until their units are confirmed.
    """
    _warn_first_sighting(record)
    events = events or []
    barcode = (
        clean_code(record.get("parcelNumber"))
        or clean_code(record.get("barCode"))
        or clean_code(record.get("parcelID"))
    )

    code = clean_code(record.get("statusCode"))
    status: ParcelStatus | None = None
    if code is not None and code not in NON_MOVEMENT_CODES:
        status = lookup(code)
        if status is None:
            warn_unmapped(code, source="account")
    if status is None:
        status = map_app_code(record.get("appCode"))
    from_events = _status_from_events(events)
    if status is None:
        status = from_events
    elif status is ParcelStatus.REGISTERED and from_events not in (
        None,
        ParcelStatus.REGISTERED,
    ):
        # A backfilled registration must not undo a journey here either: the
        # record's own code is no more trustworthy than a history event.
        status = from_events
    if status is None:
        status = ParcelStatus.UNKNOWN

    block = _block(record, "sending" if outgoing else "delivery")
    estimated = _timestamp(record.get("estimatedDeliveryDate"))
    planned_from, planned_to = _window(block, estimated)
    if planned_from is None and not outgoing:
        planned_from = estimated

    name = record.get("statusName")
    raw_status = name.strip() if isinstance(name, str) and name.strip() else code

    return build_parcel(
        barcode=barcode,
        status=status,
        raw_status=raw_status,
        delivered_at=_timestamp(record.get("factDeliveryDate")),
        sender=_party(record.get("sender")),
        receiver=_party(record.get("receiver")),
        planned_from=planned_from,
        planned_to=planned_to,
        pickup_point=None if outgoing else _branch_name(block),
        history=_build_history(events) if include_history else None,
        raw=record,
    )


def _party(value: Any) -> str | None:
    """A party as a display string: a plain string, or a dict's name fields."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = " ".join(
            str(value[k]).strip()
            for k in ("name", "firstName", "lastName")
            if isinstance(value.get(k), str) and value[k].strip()
        )
        return name or None
    return None


__all__ = [
    "MODE_RECEIVER",
    "MODE_SENDER",
    "check_direction",
    "normalize_account_parcel",
    "record_status_unresolved",
]
