# Meest Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-meest.svg)](https://github.com/ha-parcel-integrations/ha-meest/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-meest/total.svg)](https://github.com/ha-parcel-integrations/ha-meest/releases)
[![Marketplace](https://img.shields.io/badge/Marketplace-Custom-41BDF5.svg)](https://hacs.xyz)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Meest](https://meest.com) parcels in **Ukraine** — both domestic shipments and parcels Meest brings in from abroad, once they are on Meest's own network. Choose either tracking codes, which need no account, or your own Meest account.

> **Pre-release.** Tracking by code has been confirmed on real parcels. The account source has **not** been confirmed against a real account yet, so treat it as experimental: it may show fewer fields than listed below, or fail on a response it has not seen. Unfamiliar answers are logged as warnings with a link to report them.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Tracking codes** — track any number of Meest parcels by tracking number, incoming or outgoing; no account needed
- **Account** *(unconfirmed)* — sign in with your own Meest login and the parcels **your account owns** are imported automatically; only rotating tokens are stored, never your password. Parcels you merely follow by number in the Meest app are kept on the phone and are not part of the account, so they do not appear here — track those with **Tracking codes**
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `at_pickup_point` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, en route to a pickup point, awaiting pickup, recently delivered parcels, plus outgoing and delivered outgoing parcels
- Read-only **Deliveries** calendar with expected delivery windows (account source, once confirmed)
- `meest.track_parcel` / `meest.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, plus outgoing status changed and delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- For **Tracking codes**: nothing but the tracking numbers
- For **Account**: a Meest account (the one you use on the Meest website or app)

## Installation

### Marketplace (recommended)

Click the button below to open this repository in the Marketplace, then install **Meest**:

[![Open your Home Assistant instance and open a repository inside the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-meest&category=integration)

Or add it by hand: in the Marketplace, choose the three-dot menu → **Custom repositories**, add `https://github.com/ha-parcel-integrations/ha-meest` as an **Integration** and install **Meest**.

Restart Home Assistant if it asks you to.

### Manual

Copy `custom_components/meest` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meest)

Add the integration via **Settings → Devices & Services → Add Integration → Meest** and choose a source:

- **Tracking codes** creates the hub straight away. Add parcels through the integration's **Configure** dialog, the [`meest.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). Only one tracking hub can exist.
- **Account** asks for your Meest login and password once, then stores only the tokens Meest returns, never the password. Sign in with the **phone number** the account was created with, in international form (for example `+380671234567`); an account from another Meest brand's website is a different account and will be refused. You can add more than one account. **A Meest account can only be signed in one place at a time** — Meest issues a single credential per account and replaces it on every sign-in, so connecting Home Assistant here signs the Meest app out, and signing back in on the app disconnects Home Assistant until you reconnect it. This is how Meest's login works; the integration cannot work around it. If you'd rather avoid it entirely, use **Tracking codes** instead: it needs no account, so nothing gets signed out anywhere.

A tracking number alone cannot tell a parcel you sent from one you are receiving, so the **Configure** dialog has one list for **Incoming parcels** and one for **Outgoing parcels**. File each code in the list it belongs to; entering a code in the other list moves it. An account entry works out the direction itself.

Meest tracking numbers come in several shapes (consolidation numbers ending in `G`, webshop numbers, postal S10 numbers and plain digit strings). Spaces are ignored and letters are upper-cased.

## Options

Open **Configure** on the integration entry:

| Option | Where | Default | Description |
|---|---|---|---|
| Incoming parcels / Outgoing parcels | Tracking entry | — | Manage the tracked codes. Changes apply immediately, no restart. |
| Delivered parcels: filter by / amount | Both | last 7 days | How long delivered parcels stay visible on the delivered sensors. |
| Include status history | Both | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while an incoming parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Never fully stops (account)** — with nothing hot or in transit, polling keeps
  running at the normal cadence, since that's also how a new shipment on your
  account gets discovered.
- **Stops when idle (tracking codes)** — with nothing tracked, or every tracked
  parcel delivered, polling pauses until you add a parcel.
- A small, fixed per-hub offset is added on top, so not every Meest
  hub out there polls at exactly the same second.

The tracking lookup has no delivery window, so an out-for-delivery parcel
always counts as hot until it is delivered.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Meest → ⋮ → Delete**. Nothing is stored on Meest's side. Removing an account entry does not sign you out of the Meest app.

## Sensors

| Entity | Description |
|---|---|
| `sensor.meest_incoming_parcels` | Number of active incoming parcels, full list under the `parcels` attribute |
| `sensor.meest_parcel_<code>` | One per active parcel, incoming or outgoing; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.meest_next_delivery` | Earliest expected delivery moment across active incoming parcels (account source only: the tracking lookup has no estimate) |
| `sensor.meest_en_route_to_pickup_point` | Incoming parcels on their way to a pickup point |
| `sensor.meest_awaiting_pickup` | Incoming parcels that are ready to collect at a pickup point |
| `sensor.meest_delivered_parcels` | Recently delivered incoming parcels (see the retention option) |
| `sensor.meest_outgoing_parcels` | Active outgoing parcels |
| `sensor.meest_outgoing_delivered_parcels` | Recently delivered outgoing parcels |
| `calendar.meest_deliveries` | Read-only calendar of expected deliveries, no extra API calls |
| `sensor.meest_last_successful_update` | Diagnostic: when Meest was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically. An account entry's entity IDs carry the account login.

### What each source provides

| Field | Tracking codes | Account |
|---|---|---|
| Status, Meest's own status text, tracking link | Yes | Yes |
| Status history (opt-in) | Yes | Yes |
| Expected delivery window | No | Awaiting data |
| Pickup point name | No | Awaiting data |
| Weight, dimensions | No | Awaiting data |
| Sender, receiver | No | Awaiting data |

"Awaiting data" means the integration tries to read the field but it has not been seen on a real account yet, so it is not promised.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced by the sender, or registered with Meest |
| `in_transit` | On its way, including international sorting and customs clearance |
| `out_for_delivery` | With the courier today |
| `at_pickup_point` | Waiting for you at a branch, parcelshop or locker |
| `delivered` | Delivered, or collected by the recipient |
| `returning` | Going back to the sender |
| `problem` | A failed delivery attempt, a suspended delivery or a lost parcel |
| `unknown` | Not scanned yet or no longer on Meest's system, or a status we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Meest device):

| Event | When |
|---|---|
| `meest_parcel_registered` | A new parcel appears in the active list |
| `meest_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `meest_parcel_delivered` | A parcel is delivered |
| `meest_parcel_delivery_time_changed` | The expected delivery window changes |
| `meest_outgoing_parcel_status_changed` | An outgoing parcel's status changes (not fired for a parcel's first appearance) |
| `meest_outgoing_parcel_delivered` | An outgoing parcel is delivered |

Every payload is the full normalised parcel plus the entry's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `meest.track_parcel` | `tracking_code`, `direction` (`incoming` default, or `outgoing`) | Start tracking a parcel; tracking it again with the other direction moves it |
| `meest.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

The services act on the tracking-codes entry; an account entry has no code list.

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/ha-parcel-card](https://github.com/jonisnet/ha-parcel-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.meest: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Meest has no scans for the number yet, or its history has expired from Meest's system (an old number looks exactly like a wrong one), or the code is wrong. It picks up automatically once Meest has scans.
- **Tracking lookups fail for a while** — Meest sometimes refuses lookups for a short time. The integration retries once on its own and keeps the last known data meanwhile; if it does not recover, reload the integration or try again later, and [open an issue](https://github.com/ha-parcel-integrations/ha-meest/issues) with the log line.
- **Account sign-in keeps being requested** — the stored sign-in was refused, most often because the account was signed in somewhere else: a Meest account can only be signed in one place at a time, and the Meest app replaces the credential Home Assistant holds. Follow the prompt and enter the account password again — which signs the app out in turn. To avoid the tug-of-war entirely, use a tracking-codes hub instead.
- **An account hub shows no parcels while the Meest app lists some** — the app's list mixes parcels your account owns with parcels you followed by number, and the followed ones live on the phone, not on the account: Meest's own API reports the account as empty. Add those by tracking code instead. If the app shows parcels your account really does own and Home Assistant still shows none, turn on debug logging for `custom_components.meest` and [open an issue](https://github.com/ha-parcel-integrations/ha-meest/issues) with the line that says how many records came back.
- **A status logs "Unrecognised Meest status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-meest/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended. Meest uses scan codes that are not in its published list, so expect a few.
- **A log line says Meest account parcels have not been confirmed** — expected until the account source has been seen on a real account; if something looks wrong, open an issue and paste that line.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Meest, Home Assistant, or any other third party referenced in this project. Please don't contact Meest for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Meest's terms. Data is sent only to Meest's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

Tracking by code uses the same public tracker as the Meest consumer websites; the account source uses your own Meest login.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
