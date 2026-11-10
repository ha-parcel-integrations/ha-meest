"""The one status vocabulary, shared by both sources.

The scan code is the status key. Wording is never used: the same code reaches
the user with several different phrasings depending on which feed wrote the
row, and the coarser action id groups several codes together.
"""

from __future__ import annotations

import logging

from .const import ParcelStatus

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. The ``?template=`` parameter
# makes the link open the right form, so the report arrives with the version
# and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-meest/issues/new"
    "?template=unrecognised_status.yml"
)

STATUS_MAP: dict[str, ParcelStatus] = {
    "2": ParcelStatus.REGISTERED,
    "3": ParcelStatus.PROBLEM,
    "100": ParcelStatus.REGISTERED,
    "101": ParcelStatus.REGISTERED,
    "11": ParcelStatus.IN_TRANSIT,
    "12": ParcelStatus.IN_TRANSIT,
    "109": ParcelStatus.RETURNING,
    "202": ParcelStatus.IN_TRANSIT,
    "203": ParcelStatus.IN_TRANSIT,
    "303": ParcelStatus.IN_TRANSIT,
    "324": ParcelStatus.IN_TRANSIT,
    "404": ParcelStatus.IN_TRANSIT,
    "405": ParcelStatus.IN_TRANSIT,
    "505": ParcelStatus.IN_TRANSIT,
    "606": ParcelStatus.IN_TRANSIT,
    "707": ParcelStatus.RETURNING,
    "808": ParcelStatus.IN_TRANSIT,
    "909": ParcelStatus.IN_TRANSIT,
    "1011": ParcelStatus.IN_TRANSIT,
    "1100": ParcelStatus.IN_TRANSIT,
    "1200": ParcelStatus.AT_PICKUP_POINT,
    "1213": ParcelStatus.IN_TRANSIT,
    "1214": ParcelStatus.AT_PICKUP_POINT,
    "1215": ParcelStatus.IN_TRANSIT,
    "1216": ParcelStatus.AT_PICKUP_POINT,
    "1220": ParcelStatus.AT_PICKUP_POINT,
    "1315": ParcelStatus.OUT_FOR_DELIVERY,
    "1521": ParcelStatus.RETURNING,
    "1620": ParcelStatus.PROBLEM,
    "1621": ParcelStatus.PROBLEM,
    "1622": ParcelStatus.DELIVERED,
    "1623": ParcelStatus.PROBLEM,
    "1624": ParcelStatus.PROBLEM,
    "1625": ParcelStatus.PROBLEM,
    "1626": ParcelStatus.PROBLEM,
    "1627": ParcelStatus.PROBLEM,
    "1724": ParcelStatus.DELIVERED,
    "1825": ParcelStatus.RETURNING,
    "1927": ParcelStatus.PROBLEM,
    # Customs is movement, not a problem: the enum has no customs value.
    "2000": ParcelStatus.IN_TRANSIT,
    "2220": ParcelStatus.IN_TRANSIT,
    "2225": ParcelStatus.IN_TRANSIT,
    "2226": ParcelStatus.IN_TRANSIT,
    "2227": ParcelStatus.IN_TRANSIT,
    "2230": ParcelStatus.IN_TRANSIT,
    "2232": ParcelStatus.IN_TRANSIT,
    "2331": ParcelStatus.PROBLEM,
    "2432": ParcelStatus.IN_TRANSIT,
    "5051": ParcelStatus.PROBLEM,
    "8081": ParcelStatus.IN_TRANSIT,
}

# Records a correction, not a movement: it must never set a parcel's status,
# and it is not worth a warning either.
NON_MOVEMENT_CODES = frozenset({"2533"})

# The account source's own per-event enum is an ordinal, and anything out of
# range falls back to "created" in the app. It is range-checked here instead,
# so an unknown ordinal is reported rather than silently mislabelled.
APP_CODE_MAP: tuple[ParcelStatus, ...] = (
    ParcelStatus.REGISTERED,  # 0 created
    ParcelStatus.IN_TRANSIT,  # 1 sent
    ParcelStatus.DELIVERED,  # 2 delivered
    ParcelStatus.DELIVERED,  # 3 received
    ParcelStatus.PROBLEM,  # 4 lost
    ParcelStatus.RETURNING,  # 5 returned
)

# Codes already warned about, so each unmapped one is logged once per HA
# session instead of on every poll.
_warned_codes: set[str] = set()


def clean_code(value: object) -> str | None:
    """Return a scan code as a plain string, or ``None`` when absent."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def warn_unmapped(code: str, *, source: str) -> None:
    """Log an unmapped code once, with a copy-paste issue link."""
    key = f"{source}:{code}"
    if key in _warned_codes:
        return
    _warned_codes.add(key)
    _LOGGER.warning(
        "Unrecognised Meest status — help us map it. Open an issue "
        "and paste this line: %s\n  source=%s status=%s → not mapped",
        NEW_ISSUE_URL,
        source,
        code,
    )


def lookup(code: str | None) -> ParcelStatus | None:
    """Map a scan code without any side effect; ``None`` when not mapped."""
    return STATUS_MAP.get(code) if code else None


def map_scan_code(code: str | None, *, source: str = "tracking") -> ParcelStatus | None:
    """Map a scan code, warning once for a code that is neither mapped nor ignored."""
    if not code or code in NON_MOVEMENT_CODES:
        return None
    mapped = STATUS_MAP.get(code)
    if mapped is None:
        warn_unmapped(code, source=source)
    return mapped


def map_app_code(value: object) -> ParcelStatus | None:
    """Map the account source's coarse event ordinal, range-checked."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        ordinal = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= ordinal < len(APP_CODE_MAP):
        return APP_CODE_MAP[ordinal]
    warn_unmapped(f"appCode={ordinal}", source="account")
    return None
