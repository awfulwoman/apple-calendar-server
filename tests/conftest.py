"""A fake standing in for `apple_calendar_server.eventkit_client` — in-memory, no
TCC/Calendar dependency, so the store/http tests run anywhere. Dates flowing in
and out are real Foundation NSDate objects (they need no permission), so the
window/date logic is exercised for real."""
from __future__ import annotations
import os
import time
import uuid
import pytest


class FakeCalendar:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title

    def allowsContentModifications(self):
        return True


class FakeEvent:
    def __init__(self, identifier):
        self._identifier = identifier
        self._title = ""
        self._notes = None
        self._location = None
        self._all_day = False
        self._start = None
        self._end = None
        self._url = None
        self._calendar = None
        self._creation_date = None
        self._last_modified = None
        self._removed = False

    def calendarItemIdentifier(self):
        return self._identifier

    def title(self):
        return self._title

    def setTitle_(self, v):
        self._title = v

    def notes(self):
        return self._notes

    def setNotes_(self, v):
        self._notes = v

    def location(self):
        return self._location

    def setLocation_(self, v):
        self._location = v

    def isAllDay(self):
        return self._all_day

    def setAllDay_(self, v):
        self._all_day = v

    def startDate(self):
        return self._start

    def setStartDate_(self, v):
        self._start = v

    def endDate(self):
        return self._end

    def setEndDate_(self, v):
        self._end = v

    def URL(self):
        return self._url

    def setURL_(self, v):
        self._url = v

    def calendar(self):
        return self._calendar

    def setCalendar_(self, v):
        self._calendar = v

    def creationDate(self):
        return self._creation_date

    def lastModifiedDate(self):
        return self._last_modified


class FakeEventKit:
    """Implements the same functions as `eventkit_client`, backed by an in-memory dict."""

    def __init__(self):
        self.events: dict[str, FakeEvent] = {}
        self.calendars: dict[str, FakeCalendar] = {}
        self.refresh_calls = 0

    def new_store(self):
        return self

    def request_access(self, store):
        pass

    def refresh_sources(self, store):
        self.refresh_calls += 1

    def fetch_events(self, store, start_date, end_date, calendars=None):
        """Real EventKit's predicate matches events that *overlap* [start, end], not
        only ones starting inside it — an event already in progress at window-open
        is still returned."""
        ws, we = start_date.timeIntervalSince1970(), end_date.timeIntervalSince1970()
        out = []
        for e in self.events.values():
            if e._removed or e._start is None:
                continue
            event_start = e._start.timeIntervalSince1970()
            event_end = e._end.timeIntervalSince1970() if e._end is not None else event_start
            if event_start <= we and event_end >= ws:
                out.append(e)
        return out

    def event_by_identifier(self, store, ek_identifier):
        e = self.events.get(ek_identifier)
        return e if e and not e._removed else None

    def writable_calendars(self, store):
        return list(self.calendars.values())

    def get_or_create_calendar(self, store, name):
        if name not in self.calendars:
            self.calendars[name] = FakeCalendar(name)
        return self.calendars[name]

    def new_event(self, store):
        return FakeEvent(str(uuid.uuid4()))

    def save_event(self, store, event):
        if event._creation_date is None:
            event._creation_date = self._clock()
        event._last_modified = self._clock()
        self.events[event._identifier] = event

    def remove_event(self, store, event):
        event._removed = True

    def _clock(self):
        from Foundation import NSDate
        return NSDate.date()


@pytest.fixture(params=["Europe/Berlin", "America/New_York"])
def tz(request):
    """Pins the process timezone to one either side of UTC. All-day events are
    anchored to *local* midnight, so any UTC/local confusion shifts their date in
    opposite directions on the two — and neither would show up on a UTC runner."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def fake_ek():
    return FakeEventKit()


@pytest.fixture
def store_module(fake_ek, tmp_path, monkeypatch):
    from apple_calendar_server import store as store_mod
    from apple_calendar_server.config import Config

    monkeypatch.setattr(store_mod, "ek", fake_ek)
    config = Config(bearer_tokens=["test-token"], db_path=str(tmp_path / "meta.db"))
    store_mod.init(config)
    return store_mod


@pytest.fixture
def app(store_module):
    from apple_calendar_server import http

    http.init(store_module._config)
    return http.create_app()
