# Working in this repository

Home Assistant custom integration for **Meest** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. A field you could not verify (no real parcel yet) goes in `PENDING_CAPABILITIES` instead of being left out — the site then says "awaiting data" rather than claiming the API never exposes it; move it into `CAPABILITIES` once confirmed |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **Events and `_known_state` are built from the unfiltered delivered list.**
  The "keep N days / N parcels" option only trims `coordinator.delivered`; a
  parcel it leaves out must still fire `_parcel_delivered`.
- **`_async_options_updated` compares `entry.options` with the snapshot on
  `runtime_data` before refreshing — don't drop that.** HA notifies the update
  listener on every `async_update_entry`, so without the comparison a rotated
  account token would turn into a poll.

## Carrier-specific notes

**API mechanics live in `carrier-research/meest/api/` (private research
repo)** — the endpoint URLs, the request parameters, the full response
envelopes and the status vocabulary this build was reconstructed from. Do not
duplicate them here; this section is integration-level decisions only.

**Pre-1.0 until the account source is confirmed.** Tracking by code is
confirmed on real parcels; the account source's record shapes are
reconstructed and have never been seen on a real account. The one-shot
WARNING net must stay active: an unmapped scan code, an unfamiliar lookup
result code, an account `statusCode` the shared map does not know, an
out-of-range account `appCode`, an unfamiliar list/login shape, a `direction`
value returned under both modes, and the structure (keys only, never values)
of the first account record. **Do not claim the account source works in the
README or release notes** until a real account has confirmed it, and move
`weight`, `dimensions`, `delivery_window` and `pickup_point` from
`PENDING_CAPABILITIES_BY_VARIANT["Account"]` into `CAPABILITIES_BY_VARIANT`
only field by field, as a real parcel shows each one.

**Two sources, one domain — `tracking/` and `account/`.** Each owns its client,
coordinator and normaliser. What is shared sits at the package root:
`status.py` (the single status map), `parcels.py` (canonical shape, sort,
retention), `coordinator.py` (the base class: scheduling, the four published
buckets, events), `events.py` (the bus contract). Both coordinators publish
`{incoming_active, incoming_delivered, outgoing_active, outgoing_delivered}`.
An entry's source is `entry.data[CONF_SOURCE]`; a missing key means tracking.
No letters module: Meest delivers parcels and lockers only.

**One status vocabulary, keyed on the numeric scan code.** Never map on
wording (one code arrives with several phrasings) and never on the coarser
action id. Decisions worth keeping:
- Customs stays `in_transit`; collection (`1724`) and doorstep delivery
  (`1622`) are both `delivered`; a parcel at a branch or locker is
  `at_pickup_point`, not `out_for_delivery`.
- `2533` records a correction, not a movement: it never sets a status and
  does not warn (`NON_MOVEMENT_CODES`).
- **A newer `registered` never wins over an older further step.** Partner
  feeds backfill their own registration (`100`) after the parcel has left the
  country, and a correction event's coarse ordinal `0` reads "created"; both
  would otherwise walk a moving parcel back to Registered and fire a status
  event twice. Registration cannot happen twice, so the older row stands. The
  rule is deliberately limited to `registered`: a return or a second transit
  leg is a real step back and must still show.
- `324` is mapped to `in_transit` although Meest's published list omits it.
  Other unpublished codes will turn up; an unmapped newest code falls back to
  the newest mappable scan, then `unknown`, and warns once.
- The account source tries the record's `statusCode` against the same map,
  then the six-value `appCode` ordinal **range-checked** (the app itself
  silently falls back to "created" out of range), then the newest mappable
  history event. Do not cross-call the tracking lookup per account parcel.

**Tracking row handling — the traps that lose data silently.**
- The user's entered code is the canonical `barcode`, never a row field: a
  parcel changes number mid-journey, later rows leave `ShipmentNumber` empty
  and carry a different `Barcode`. A row is accepted when the entered code
  equals *any* of the four identifying fields; if only the partner's transit
  reference names it the rows belong to someone else's parcel and the lookup
  is inconclusive (keep last-good, show no history). Duplicate scans from two
  feeds are collapsed on timestamp + code + wording + place.
- Timestamps are the carrier's wall clock with no offset: never localise or
  stamp a zone. `00:00:00` / `00:00:01` mean "a day without a time" and are
  emitted as a bare date.
- An empty result means unknown **or** aged out: show a pending `unknown`,
  never tell the user the code is wrong. A non-`000` result code, a
  non-XML body, a redirect, `403`/`5xx` and a second empty `400` are all
  inconclusive: the code keeps its last good data. An empty `400` is retried
  exactly once after re-reading the page; the page read is cached for hours
  and shared between concurrent lookups.
- The page read is fail-closed and narrow: one anchored pattern, nothing else
  on the page is ever parsed, and no parcel data is taken from it. Responses
  are capped at 1 MB and any markup declaration is refused before parsing.
- The tracking lookup carries no ETA, weight, dimensions, pickup point,
  sender or receiver, and never will: those live behind a recipient-identity
  gate that is deliberately not used.
- Code validation accepts every non-empty code, per the suite convention: a
  guessed shape would lock a user out of a code the carrier resolves, while a
  bad code just comes back "not found". The aggregator must not claim codes by
  shape either.

**The account only ever holds parcels the account owns.** The carrier's app
shows more than the account does: a parcel the user merely follows by number
is kept on their phone, not on the account. Confirmed on 2026-11-11 against a
real account that listed two parcels in the app while the account itself
reported none. So an empty account source is usually correct, not a bug, and a
followed parcel can only be tracked by code. Do not try to reach the phone.

**One credential per account.** Signing in anywhere ends the previous
session, in both directions: setting up an entry signs the carrier's app out,
and signing in on the app disconnects Home Assistant. Claiming to be a
different client was tried on 2026-11-11 and bought no second session, so the
integration stays honest about what it identifies as. The config flow and the
README both warn about this, in the words ha-ppl-cz uses for the same
behaviour.

**The parcel list is selected by a filter, not by a role.** `MODE_RECEIVER`
and `MODE_SENDER` hold the two values the carrier understands; the obvious
role names are not among them, and a value it does not know answers `200` with
an empty list rather than an error, which is how a real account looked empty
for an evening. Never ask for the combined filter: it would merge the two
directions this integration has to keep apart. The vocabulary itself lives in
the research notes.

**Direction.** Tracking is account-less and the lookup cannot reveal it, so
the user files each code under incoming or outgoing (`CONF_DIRECTION`, default
incoming, so older entries need no migration; re-filing moves a code). The
account source reads both directions explicitly, never the combined filter,
derives direction from the mode that returned the record, de-duplicates on
`parcelID` keeping the receiver-side record, and logs once when the same
`direction` value shows up under both modes. Outgoing parcels fire only the
outgoing event pair and get no registered or delivery-time events. Per-parcel
sensors exist for both directions; `awaiting_pickup`, `next_delivery` and the
calendar stay incoming-only.

**No `en_route_to_pickup_point` sensor.** The tracking lookup only learns of a
pickup point at arrival, so the sensor would sit at zero; `awaiting_pickup` is
the pickup summary.

**The account's `login` is a phone number, not an e-mail**, in international
form. Sign-up runs on that same number, so an account created on another Meest
brand's website is a different account and is simply refused here. A rejected
login is a config-flow form error, not a log line — that is correct HA
behaviour, so "no error in the logs" is expected and not a symptom.

**A rejected credential comes back as a `400`, not only a `401`.** Confirmed
live on 2026-11-11, when a dead token pair was refused that way by the refresh
call. Any call that presents a credential — login and refresh both — therefore
passes `credential=True`, so `400`/`401`/`403` and an error envelope all mean
"rejected". Without that, a dead session retried forever as a generic
`ConfigEntryNotReady` instead of prompting for reauth. A session may well be
single-use per account; the README warns that signing in to the app can sign
Home Assistant out.

**Account credentials: tokens are stored, the password never is.** The login
is exchanged once; the token pair is written back to the entry whenever it
rotates, a rejected read is refreshed and retried at most twice, and a
rejected refresh raises `ConfigEntryAuthFailed` (translated in the
coordinator — a bare client exception would never prompt). The same `401`
answers an unknown parcel and a dead session, so it is **never** read as
"not found". Reauth keeps the entry's unique id (`account:<lowercased login>`)
and re-asks only the password. The account's `weight` and `dimensions` stay
`None` on purpose until their units are confirmed.

**Do not build:** the recipient phone/one-time-code flow behind the tracker's
ETA and storage deadline; any write operation of the app (create, return,
redirect, pickup registration, sender-side changes, locker insert/remove);
the other app hosts; the US iframe tracker; chasing a partner's leg when a
parcel leaves Meest's network.

## Running tests

```
python -m pytest tests/ --cov=custom_components.meest
```

Coverage must stay **above 95%** per source module. Fixtures in
`tests/payloads.py` are synthetic; never commit a real response.
