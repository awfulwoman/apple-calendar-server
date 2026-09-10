# apple-calendar-server

A small authorised REST API in front of the real macOS Calendar app (via
`EventKit`), so client applications can read and write the Calendar the user
actually uses — on any device, via Siri, the widget, or the Calendar UI —
instead of a synthetic store nothing else syncs with. It's the sibling of
[`apple-reminders-server`](https://github.com/awfulwoman/apple-reminders-server) and
follows the same principles (thin EventKit wrapper, sidecar SQLite for bookkeeping,
LWW + tombstones, native-edit adoption, stable code-signed interpreter).

**Multiple calendars** are first-class: events carry a `calendar`, `GET /events`
can be filtered by it, writes create/target a named calendar, and `GET /calendars`
lists the writable ones.

## Why a sidecar database

EventKit has no equivalent of CalDAV's `STATUS:CANCELLED` tombstone, no custom
properties for an app-authoritative `updated_at`, and no client-assignable UID (its
`calendarItemIdentifier` is system-assigned). `apple_calendar_server/sidecar.py`
keeps a small local SQLite table — `id ↔ calendarItemIdentifier`, `updated_at`,
`deleted`/last-known content — purely for that bookkeeping. The event *content*
always lives in the real Calendar.app; the sidecar is never authoritative for it.

Events created outside the API (Siri, another device) are transparently **adopted**
the first time they're observed — see the `store.py` module docstring.

## Events are fetched over a window

Unlike reminders, EventKit has no "all events" query — events are fetched over a
bounded date range (`predicateForEventsWithStartDate:endDate:calendars:`, capped at
~4 years). So `GET /events` always works over a window (`start`/`end`, defaulting to
`now − 30d … now + 365d`). One consequence: `GET /events` only reconciles *native
deletions* for events whose start falls inside the queried window — an event outside
the window is out of range, not deleted. `GET /events/{id}` looks up by identifier
regardless of date, so it always sees a genuine deletion.

## Event shape

```json
{
  "id": "…", "title": "Standup", "notes": null, "location": "Zoom",
  "all_day": false,
  "start": "2026-08-01T09:00:00Z", "end": "2026-08-01T09:30:00Z",
  "calendar": "Work", "url": null,
  "created_at": "…", "updated_at": "…", "deleted": false
}
```

For `all_day` events `start`/`end` are date-only (`YYYY-MM-DD`); otherwise they're
UTC instants (`YYYY-MM-DDThh:mm:ssZ`). Recurrence rules and attendees are not yet
modelled (a recurring series is seen through its occurrences within the window).

A date-only value is the day **in the server's local timezone**, because that is
where EventKit anchors an all-day event: its `startDate` is local midnight and its
`endDate` is 23:59:59 that evening. Reading those instants as UTC instead names the
day before east of Greenwich — an event on the 15th reported as the 14th — and it
also makes the cached start disagree with the real one, so a window that stops short
of the event looks like a native deletion and tombstones it.

## Remote accounts are refreshed on every read

EventKit only ever reads Calendar.app's *local* store, and Calendar.app polls remote
accounts on its own schedule (~15 minutes for Google/CalDAV). Reads therefore call
`refreshSourcesIfNecessary()` first, which bounds staleness by the request rate rather
than by that timer. The refresh is asynchronous, so a just-created event still tends
to appear on the next request rather than the one that triggered the refresh. Note
that no health check can detect this class of problem: the service is up and returns
`200 OK` the whole time it is serving stale data.

## Running locally

```bash
uv sync
cp .env.example .env   # set CALENDAR_SERVER_BEARER_TOKENS at minimum
uv run apple-calendar-server
```

First run triggers a macOS permission dialog for Calendar access — approve it once.

## Installing as a LaunchAgent

```bash
./scripts/install_service.sh    # ./scripts/uninstall_service.sh to remove
```

Must run as a **LaunchAgent** (`gui/<uid>` domain), not a LaunchDaemon — Calendar's
TCC permission is granted per logged-in user in a GUI session; a root-owned system
daemon never sees the prompt. In practice that means running it on a Mac that stays
logged into a GUI session — a headless box can never hold the grant.

### Why the interpreter is code-signed (permission stability)

macOS TCC grants Calendar access to a **code identity**. An unsigned or
ad-hoc-signed Python interpreter is identified only by its `cdhash`, which changes
on every `uv sync`, venv rebuild, or Python patch bump — so an interpreter signed
that way would have its grant silently revert to *denied* on the next rebuild, and
a headless LaunchAgent can never re-prompt to recover it.

To keep the grant stable across rebuilds (same approach as apple-reminders-server):

- The LaunchAgent execs the venv interpreter directly
  (`.venv/bin/python3 -m apple_calendar_server.main`), not via `uv run` — `uv run`
  re-resolves the venv and can swap the interpreter out from under it.
- The interpreter is signed with a **SHARED stable identity**.
  `apple-reminders-server`, `apple-contacts-server` and this service all pin the
  same `.python-version`, so uv hard-links them to one CPython file on disk.
  `codesign --force` replaces the whole signature, so if each service signed
  with its own identifier the last deploy would win and silently break the other
  two TCC grants (noticed only on their next restart). All three therefore sign
  that one file with **one** identity, `com.awfulwoman.apple-reminders-server` —
  the Reminders, Calendars and Contacts grants are all pinned to it.
  `apple-reminders-server` owns provisioning + trusting the cert
  (`scripts/setup_signing_cert.sh`, its infra role deploys first); here
  `scripts/sign_runtime.sh` just re-signs with that identity on every deploy.
  Override `SIGNING_IDENTITY_CN` / `SIGNING_BUNDLE_ID` only for a deliberate
  migration (`tccutil reset` all three services, then re-approve).
- `.python-version` is pinned to an exact patch so uv doesn't swap the interpreter
  underneath the grant.

You still approve Calendar access **once** at first launch
(System Settings › Privacy & Security › Calendars). After that it persists.

## API

All endpoints require `Authorization: Bearer <token>` (one of
`CALENDAR_SERVER_BEARER_TOKENS`).

| Method | Path | |
|---|---|---|
| GET | `/events?start=&end=&calendar=&since=` | List events in a window (optionally filtered) |
| GET | `/events/{id}` | Fetch one |
| PUT | `/events/{id}` | Upsert (last-write-wins on `updated_at`, `409` + current on stale) |
| DELETE | `/events/{id}` | Soft delete (tombstone) |
| GET | `/calendars` | List writable calendars |
| POST | `/admin/gc_tombstones` | Prune tombstones older than `older_than_days` (default 30) |

## Testing

```bash
uv run pytest
```

EventKit calls are mocked (`tests/conftest.py`'s `FakeEventKit`) — no permission
prompt, no real Calendar data touched. `apple_calendar_server/eventkit_client.py`
is the only module that imports EventKit directly, kept thin so it's the one thing
that needs occasional live verification against real Calendar.app.
