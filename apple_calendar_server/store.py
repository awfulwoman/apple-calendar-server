"""Events store backed by real EventKit content plus the `sidecar` bookkeeping
table. Public interface mirrors the reminders server's store (get/list/upsert/
soft_delete/gc_tombstones/now_utc/now_after/new_id/Stale) so Gateway can treat it
as the same kind of LWW-with-tombstones backend.

Events created outside this service — the Calendar app, another device — are
"adopted" on first sight: the first list/get call that observes an EKEvent with no
sidecar row mints one.

Key difference from reminders: EventKit has no "all events" query, only a bounded
date-window predicate. So `list_events` always works over a window, and deletion
reconciliation (sidecar row exists but the event is no longer live) is applied
ONLY to rows whose cached start falls inside that window — an event outside the
window is out of range, not deleted. `get(id)` looks an event up by identifier
regardless of date, so its self-heal-into-tombstone is always safe.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from Foundation import NSDate, NSURL
from apple_calendar_server import eventkit_client as ek
from apple_calendar_server.config import Config
from apple_calendar_server.sidecar import Sidecar

_config: Config | None = None
_store = None
_sidecar: Sidecar | None = None


class Stale(Exception):
    def __init__(self, current: dict):
        super().__init__(f"stale write for event {current.get('id')!r}")
        self.current = current


def init(config: Config) -> None:
    global _config, _store, _sidecar
    _config = config
    _store = ek.new_store()
    ek.request_access(_store)
    _sidecar = Sidecar(config.db_path)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_after(previous: str) -> str:
    """A timestamp strictly after `previous` — avoids spurious LWW rejections when
    two writes land within the same wall-clock second."""
    now = now_utc()
    if now > previous:
        return now
    dt = datetime.strptime(previous, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return str(uuid.uuid4())


def _default_window() -> tuple[str, str]:
    past = _config.default_window_past_days if _config else 30
    future = _config.default_window_future_days if _config else 365
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=past)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=future)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def _parse_iso(value: str) -> datetime:
    """Accepts a date-only 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DDThh:mm:ssZ' instant,
    always as UTC."""
    if len(value) == 10:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _iso_to_nsdate(value: str) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(_parse_iso(value).timestamp())


def _nsdate_to_iso(nsdate) -> str | None:
    if nsdate is None:
        return None
    ts = nsdate.timeIntervalSince1970()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nsdate_to_date(nsdate) -> str | None:
    if nsdate is None:
        return None
    ts = nsdate.timeIntervalSince1970()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _event_to_dict(event, id: str, updated_at: str) -> dict:
    all_day = bool(event.isAllDay())
    fmt = _nsdate_to_date if all_day else _nsdate_to_iso
    return {
        "id": id,
        "title": str(event.title() or ""),
        "notes": str(event.notes()) if event.notes() else None,
        "location": str(event.location()) if event.location() else None,
        "all_day": all_day,
        "start": fmt(event.startDate()),
        "end": fmt(event.endDate()),
        "calendar": str(event.calendar().title()) if event.calendar() else (_config.default_calendar if _config else "Calendar"),
        "url": str(event.URL().absoluteString()) if event.URL() else None,
        "created_at": _nsdate_to_iso(event.creationDate()) or updated_at,
        "updated_at": updated_at,
        "deleted": False,
    }


def _adopt(event) -> dict:
    """A live EKEvent with no sidecar row yet — created natively. Mint an id, cache
    the current content, and persist the mapping."""
    ek_identifier = event.calendarItemIdentifier()
    updated_at = _nsdate_to_iso(event.lastModifiedDate()) or _nsdate_to_iso(event.creationDate()) or now_utc()
    id = new_id()
    d = _event_to_dict(event, id, updated_at)
    _sidecar.put(id, ek_identifier, updated_at, deleted=False, content=d)
    return d


def _observe(event, row: dict) -> dict:
    """A live EKEvent with a known sidecar row — refresh the cached content, keeping
    the sidecar's app-authoritative updated_at (not EventKit's own clock)."""
    d = _event_to_dict(event, row["id"], row["updated_at"])
    _sidecar.put(row["id"], row["ek_identifier"], row["updated_at"], deleted=False, content=d)
    return d


def _tombstone_dict(row: dict) -> dict:
    d = dict(row["content"]) if row["content"] else {"id": row["id"], "title": ""}
    d["id"] = row["id"]
    d["updated_at"] = row["updated_at"]
    d["deleted"] = True
    return d


def _content_start_in_window(content: dict | None, start_ts: float, end_ts: float) -> bool:
    """True only if we can confirm the cached event's start is inside the window.
    Unknown/unparseable starts return False so out-of-range events are never
    falsely tombstoned by a windowed list."""
    if not content or not content.get("start"):
        return False
    try:
        ts = _parse_iso(content["start"]).timestamp()
    except ValueError:
        return False
    return start_ts <= ts <= end_ts


def get(id: str) -> dict | None:
    row = _sidecar.by_id(id)
    if row is None:
        return None
    if row["deleted"]:
        return _tombstone_dict(row)
    event = ek.event_by_identifier(_store, row["ek_identifier"]) if row["ek_identifier"] else None
    if event is None:
        # Deleted natively (outside soft_delete) since we last saw it — self-heal into a tombstone.
        updated_at = now_after(row["updated_at"])
        _sidecar.put(id, None, updated_at, deleted=True)
        return _tombstone_dict({**row, "ek_identifier": None, "updated_at": updated_at})
    return _observe(event, row)


def list_events(
    start: str | None = None,
    end: str | None = None,
    calendar_name: str | None = None,
    since: str | None = None,
    include_deleted: bool = True,
) -> list[dict]:
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    start_ns = _iso_to_nsdate(start)
    end_ns = _iso_to_nsdate(end)

    live = ek.fetch_events(_store, start_ns, end_ns, None)
    seen_ek_identifiers = set()
    results = []

    for event in live:
        ek_identifier = event.calendarItemIdentifier()
        seen_ek_identifiers.add(ek_identifier)
        row = _sidecar.by_ek_identifier(ek_identifier)
        d = _adopt(event) if row is None else _observe(event, row)
        results.append(d)

    window_start, window_end = start_ns.timeIntervalSince1970(), end_ns.timeIntervalSince1970()
    for row in _sidecar.all():
        if row["deleted"]:
            # Already a tombstone — include as-is so a `since` sync sees it.
            results.append(_tombstone_dict(row))
        elif row["ek_identifier"] not in seen_ek_identifiers and _content_start_in_window(row["content"], window_start, window_end):
            # Sidecar knows this event, its start is inside the window, yet it wasn't
            # returned live — deleted natively. (Rows outside the window are skipped:
            # they're merely out of range.)
            updated_at = now_after(row["updated_at"])
            _sidecar.put(row["id"], None, updated_at, deleted=True)
            results.append(_tombstone_dict({**row, "ek_identifier": None, "updated_at": updated_at}))

    if calendar_name:
        results = [r for r in results if r.get("calendar") == calendar_name]
    if since:
        results = [r for r in results if r["updated_at"] > since]
    if not include_deleted:
        results = [r for r in results if not r["deleted"]]
    results.sort(key=lambda r: r["updated_at"])
    return results


def list_calendars() -> list[str]:
    return sorted(str(c.title()) for c in ek.writable_calendars(_store))


def upsert(event: dict) -> dict:
    if not (event.get("title") or "").strip():
        raise ValueError("title is required and must be non-empty")
    if not event.get("start"):
        raise ValueError("start is required")
    if not event.get("end"):
        raise ValueError("end is required")
    if _parse_iso(event["end"]) < _parse_iso(event["start"]):
        raise ValueError("end must not be before start")

    id = event["id"]
    row = _sidecar.by_id(id)
    existing = get(id) if row is not None else None
    if existing is not None and event["updated_at"] <= existing["updated_at"]:
        raise Stale(existing)

    ek_event = ek.event_by_identifier(_store, row["ek_identifier"]) if (row and row["ek_identifier"]) else None
    if ek_event is None:
        ek_event = ek.new_event(_store)

    target_calendar = event.get("calendar") or (_config.default_calendar if _config else "Calendar")
    ek_event.setCalendar_(ek.get_or_create_calendar(_store, target_calendar))
    ek_event.setTitle_(event["title"])
    ek_event.setNotes_(event.get("notes") or None)
    ek_event.setLocation_(event.get("location") or None)
    ek_event.setAllDay_(bool(event.get("all_day")))
    ek_event.setStartDate_(_iso_to_nsdate(event["start"]))
    ek_event.setEndDate_(_iso_to_nsdate(event["end"]))
    url = event.get("url")
    ek_event.setURL_(NSURL.URLWithString_(url) if url else None)

    ek.save_event(_store, ek_event)
    ek_identifier = ek_event.calendarItemIdentifier()
    _sidecar.put(id, ek_identifier, event["updated_at"], deleted=False)
    return _observe(ek_event, {"id": id, "ek_identifier": ek_identifier, "updated_at": event["updated_at"]})


def soft_delete(id: str, updated_at: str | None = None) -> dict:
    row = _sidecar.by_id(id)
    if row is None:
        raise KeyError(f"no event with id {id!r}")
    existing = get(id)
    if existing is None or existing.get("deleted"):
        raise KeyError(f"no event with id {id!r}")

    new_updated_at = updated_at or now_after(existing["updated_at"])
    if new_updated_at <= existing["updated_at"]:
        raise Stale(existing)
    ek_event = ek.event_by_identifier(_store, row["ek_identifier"]) if row["ek_identifier"] else None
    if ek_event is not None:
        ek.remove_event(_store, ek_event)

    tombstone = dict(existing)
    tombstone["deleted"] = True
    tombstone["updated_at"] = new_updated_at
    _sidecar.put(id, None, new_updated_at, deleted=True, content=tombstone)
    return _tombstone_dict(_sidecar.by_id(id))


def gc_tombstones(older_than_days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = _sidecar.tombstones_older_than(cutoff)
    for row in old:
        _sidecar.delete_row(row["id"])
    return len(old)
