"""Constants for the Meest parcel tracker integration."""

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "meest"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"  # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"  # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"  # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"  # Ready to collect at a pickup location
    DELIVERED = "delivered"  # Handed over
    RETURNING = "returning"  # Failed delivery, going back to sender
    PROBLEM = "problem"  # Carrier reports an exception/issue
    UNKNOWN = "unknown"  # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {
        "weight",
        "dimensions",
        "delivery_window",
        "pickup_point",
        "url",
        "history",
        "contents",
    }
)

# The tracking lookup carries scans only: no ETA, weight, dimensions or pickup
# point. The account source has never been seen against a real account, so
# everything it might add is awaiting data rather than claimed.
CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset({"url", "history"}),
    "Account": frozenset({"url", "history"}),
}

# Fields not confirmed yet — the docs site shows them as "awaiting data".
# Move a field into the declaration above once a real parcel shows it.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"weight", "dimensions", "delivery_window", "pickup_point"}),
}
CAPABILITIES = CAPABILITIES_BY_VARIANT["Tracking"]

CONF_SOURCE = "source"
SOURCE_ACCOUNT = "account"
SOURCE_TRACKING = "tracking"

# Tracking source: a keyless lookup; both requests go to the same host.
TRACKER_PAGE_URL = "https://t.meest-group.com/n/{code}"
TRACKER_LOOKUP_URL = (
    "https://t.meest-group.com/get.php?what=tracking&test&number={code}"
    "&lang=uk&ext_track=&chk={chk}"
)
TRACKING_URL = "https://t.meest-group.com/n/{code}"
# How long one page read is reused before the page is read again.
PAGE_READ_TTL_SECONDS = 6 * 3600
MAX_BODY_BYTES = 1_000_000

# Account source: the user's own Meest login. The tokens, never the password,
# are stored in the config entry.
ACCOUNT_API_URL = "https://api.meest.com/v3.0/"
CONF_LOGIN = "login"
CONF_TOKEN = "token"
CONF_REFRESH_TOKEN = "refresh_token"
ACCOUNT_APP_VERSION = "1.5.2"
# Tried on 2026-11-11: claiming a different platform than the phone's app does
# not buy a second session, so this stays honest about what it imitates.
ACCOUNT_PLATFORM = "Android"

# Tracked codes live in the entry options as ``{tracking_code, direction}``
# dicts. The lookup cannot tell a parcel you sent from one you are receiving,
# so the user declares the direction; a missing key means incoming, so entries
# stored before the option existed need no migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"
# The user-set parcel contents, and the optional entry an update is limited to.
CONF_CONTENTS = "contents"
CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_DIRECTION = "direction"
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"
DEFAULT_DIRECTION = DIRECTION_INCOMING

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active parcel
# is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from (or has
# no planned_from at all); mid = anything else still in flight.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
