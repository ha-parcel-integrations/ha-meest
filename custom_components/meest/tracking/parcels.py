"""Normaliser for the tracking source: scan rows into the canonical parcel."""

from __future__ import annotations

import re
from typing import Any

from ..const import HISTORY_MAX_EVENTS, ParcelStatus
from ..parcels import build_parcel
from ..status import (
    NON_MOVEMENT_CODES,
    clean_code,
    lookup,
    map_scan_code,
    warn_unmapped,
)

# Rows stamped at these clock times mean "a day without a time", not midnight.
_TIMELESS = frozenset({"00:00:00", "00:00:01"})
_CLOCK = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})$")

# Fields that can carry the parcel's own number, and the one that only carries
# a partner's reference for it.
_OWN_FIELDS = ("ShipmentNumber", "ShipmentNumberSender", "Barcode")
_TRANSIT_FIELD = "ShipmentNumberTransit"


def canonical_code(value: object) -> str:
    """Compare-form of a parcel number: no spaces or hyphens, upper-case."""
    return re.sub(r"[\s-]+", "", str(value or "").upper())


def match_rows(
    rows: list[dict[str, str]], code: str
) -> tuple[list[dict[str, str]], bool]:
    """Return the de-duplicated rows naming ``code`` and whether any is its own.

    A parcel changes number mid-journey: later rows can leave ``ShipmentNumber``
    empty and carry a different ``Barcode``, so a row is accepted when the
    entered code equals *any* of the identifying fields. If only the transit
    reference names it, the rows belong to a partner's parcel.
    """
    wanted = canonical_code(code)
    accepted: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    own = False
    for row in rows:
        row_own = any(canonical_code(row.get(f)) == wanted for f in _OWN_FIELDS)
        if not row_own and canonical_code(row.get(_TRANSIT_FIELD)) != wanted:
            continue
        own = own or row_own
        # Two feeds can report the same scan twice.
        key = (
            row.get("DateTimeAction", ""),
            row.get("StatusCode", ""),
            row.get("ActionMessages_EN") or row.get("ActionMessages", ""),
            row.get("City_EN") or row.get("City", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        accepted.append(row)
    return accepted, own


def clock_to_iso(value: str | None) -> str | None:
    """Turn a wall-clock stamp into ISO without inventing a time zone.

    Foreign legs carry the foreign office's clock, so no single offset fits
    the history; a timeless day comes out as a bare date.
    """
    match = _CLOCK.match((value or "").strip())
    if match is None:
        return None
    day, clock = match.groups()
    return day if clock in _TIMELESS else f"{day}T{clock}"


def _wording(row: dict[str, str]) -> str | None:
    return (
        (row.get("ActionMessages_EN") or "").strip()
        or (row.get("ActionMessages") or "").strip()
        or clean_code(row.get("StatusCode"))
    )


def _place(row: dict[str, str]) -> str:
    parts = [
        (row.get("City_EN") or row.get("City") or "").strip(),
        (row.get("Country_EN") or row.get("Country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def _sorted_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    # Stable: rows with an equal timestamp keep the order of the reply.
    return sorted(rows, key=lambda row: row.get("DateTimeAction", ""))


def _build_history(rows: list[dict[str, str]]) -> list[dict]:
    history: list[dict] = []
    for row in rows:
        timestamp = clock_to_iso(row.get("DateTimeAction"))
        if timestamp is None:
            continue
        wording = _wording(row)
        place = _place(row)
        history.append(
            {
                "timestamp": timestamp,
                "status": map_scan_code(clean_code(row.get("StatusCode"))),
                "raw_status": f"{wording} ({place})" if wording and place else wording,
            }
        )
    return history[-HISTORY_MAX_EVENTS:]


def _newest_mappable(
    rows: list[dict[str, str]],
) -> tuple[ParcelStatus, dict[str, str]] | None:
    """Newest row whose code maps, warning once for each newer one that does not.

    A partner feed backfills its own registration after the parcel has already
    moved, so the newest row can read ``registered`` while the parcel is in
    transit. Registration cannot happen twice: an older row that got further
    wins. Every other step back (a return, a second transit leg) is real and
    stands.
    """
    newest: tuple[ParcelStatus, dict[str, str]] | None = None
    for row in reversed(rows):
        code = clean_code(row.get("StatusCode"))
        if code is None or code in NON_MOVEMENT_CODES:
            continue
        mapped = lookup(code)
        if mapped is None:
            if newest is None:
                warn_unmapped(code, source="tracking")
            continue
        if mapped is not ParcelStatus.REGISTERED:
            return mapped, row
        if newest is None:
            newest = mapped, row
    return newest


def normalize_tracking_parcel(
    raw: dict[str, Any], *, code: str, include_history: bool = False
) -> dict:
    """Return the canonical parcel for one entered code.

    ``barcode`` is always the entered code, never a row field. ``raw`` is
    ``{}`` for a parcel the carrier has no scans for (unknown, or aged out),
    which renders as a pending ``unknown`` rather than an error.
    """
    rows = _sorted_rows(raw.get("rows") or [])
    found = _newest_mappable(rows)
    if found is not None:
        status, row = found
    else:
        status = ParcelStatus.UNKNOWN
        row = rows[-1] if rows else {}
    return build_parcel(
        barcode=code,
        status=status,
        raw_status=_wording(row) if row else None,
        delivered_at=clock_to_iso(row.get("DateTimeAction")) if row else None,
        history=_build_history(rows) if include_history else None,
        raw=raw,
    )
