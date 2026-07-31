"""Thin wrapper around PyObjC's EventKit bindings for calendar *events* — the only
module that imports EventKit directly, so tests can substitute a fake without
touching the real Calendar data.

Selectors confirmed against the installed pyobjc-framework-EventKit bindings by
introspection (`dir(EK.EKEvent)`, `hasattr(EK.EKEventStore, ...)`) rather than
from memory/docs. Unlike reminders, events are fetched over a bounded date window
(`predicateForEventsWithStartDate:endDate:calendars:`) and the fetch itself is
synchronous (`eventsMatchingPredicate:`) — only the TCC access request is async.
"""
from __future__ import annotations
import threading
import EventKit as EK

ENTITY_TYPE_EVENT = EK.EKEntityTypeEvent
SPAN_THIS_EVENT = EK.EKSpanThisEvent


class AccessDenied(Exception):
    pass


class SaveFailed(Exception):
    pass


def _run_sync(start):
    """Bridge an EventKit completion-handler call to a blocking call. `start` is
    given a callback and must invoke it exactly once; returns the callback's args."""
    done = threading.Event()
    result: list = []

    def callback(*args):
        result.extend(args)
        done.set()

    start(callback)
    done.wait()
    return tuple(result)


def new_store() -> EK.EKEventStore:
    return EK.EKEventStore.alloc().init()


def request_access(store: EK.EKEventStore) -> None:
    """Blocks on the TCC prompt the first time it's called for this binary. Must run
    in a GUI session — a LaunchAgent (gui/<uid>), not a LaunchDaemon (no TCC identity)."""
    status = EK.EKEventStore.authorizationStatusForEntityType_(ENTITY_TYPE_EVENT)
    if status == EK.EKAuthorizationStatusFullAccess:
        return
    granted, error = _run_sync(lambda cb: store.requestFullAccessToEventsWithCompletion_(cb))
    if not granted:
        raise AccessDenied(f"Calendar access not granted: {error}")


def fetch_events(store: EK.EKEventStore, start_date, end_date, calendars=None) -> list:
    """Events overlapping [start_date, end_date]. EventKit has no all-events query and
    caps the span at ~4 years; `calendars=None` means every calendar."""
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(start_date, end_date, calendars)
    return list(store.eventsMatchingPredicate_(predicate) or [])


def event_by_identifier(store: EK.EKEventStore, ek_identifier: str):
    return store.calendarItemWithIdentifier_(ek_identifier)


def writable_calendars(store: EK.EKEventStore) -> list:
    calendars = store.calendarsForEntityType_(ENTITY_TYPE_EVENT) or []
    return [c for c in calendars if c.allowsContentModifications()]


def get_or_create_calendar(store: EK.EKEventStore, name: str):
    for c in writable_calendars(store):
        if str(c.title()) == name:
            return c
    cal = EK.EKCalendar.calendarForEntityType_eventStore_(ENTITY_TYPE_EVENT, store)
    cal.setTitle_(name)
    cal.setSource_(store.defaultCalendarForNewEvents().source())
    ok, error = store.saveCalendar_commit_error_(cal, True, None)
    if not ok:
        raise SaveFailed(f"could not create calendar {name!r}: {error}")
    return cal


def new_event(store: EK.EKEventStore) -> EK.EKEvent:
    return EK.EKEvent.eventWithEventStore_(store)


def save_event(store: EK.EKEventStore, event: EK.EKEvent) -> None:
    ok, error = store.saveEvent_span_commit_error_(event, SPAN_THIS_EVENT, True, None)
    if not ok:
        raise SaveFailed(str(error))


def remove_event(store: EK.EKEventStore, event: EK.EKEvent) -> None:
    ok, error = store.removeEvent_span_commit_error_(event, SPAN_THIS_EVENT, True, None)
    if not ok:
        raise SaveFailed(str(error))
