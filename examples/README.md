# Examples

Ready-to-paste Home Assistant snippets for the Meest integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |
| [`dashboards/`](dashboards/) | A Lovelace card that starts tracking a parcel from a text box. |

The entity IDs assume the **Tracking codes** entry. An **Account** entry
carries the account login in its entity IDs
(`sensor.meest_<login>_incoming_parcels`), so adjust them to match.

**Feeding Meest from e-mail:** a **Tracking codes** entry follows only the
parcels you register with it. [`automations/track_parcels_from_email.yaml`](automations/track_parcels_from_email.yaml)
extracts codes from incoming shipping mails (core IMAP integration + regex,
with an optional AI fallback) and registers them automatically; setup guide,
and which Meest number shapes a regex can safely match, in
[`automations/track_parcels_from_email.md`](automations/track_parcels_from_email.md).

## Services

| Service | Description |
|---|---|
| `meest.track_parcel` | Start tracking a parcel (`tracking_code`, optional `direction`). |
| `meest.untrack_parcel` | Stop tracking a parcel (`tracking_code`). |

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `meest_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `meest_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `meest_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |
| `meest_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |
| `meest_outgoing_parcel_status_changed` | An outgoing parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `meest_outgoing_parcel_delivered` | An outgoing parcel is delivered | The full normalised parcel dict |

Every payload also carries the entry's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
