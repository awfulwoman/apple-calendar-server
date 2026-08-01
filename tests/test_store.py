import pytest


def _new_event(store_module, **overrides):
    now = store_module.now_utc()
    e = {
        "id": store_module.new_id(),
        "title": "Standup",
        "notes": None,
        "location": None,
        "all_day": False,
        "start": now,
        "end": now,
        "calendar": "Calendar",
        "url": None,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    e.update(overrides)
    return e


def test_upsert_and_get_roundtrip(store_module):
    e = _new_event(store_module, title="Standup", notes="daily", location="Zoom")
    stored = store_module.upsert(e)
    assert stored["title"] == "Standup"
    assert stored["notes"] == "daily"
    assert stored["location"] == "Zoom"
    assert stored["calendar"] == "Calendar"

    fetched = store_module.get(e["id"])
    assert fetched["title"] == "Standup"
    assert fetched["id"] == e["id"]


def test_upsert_all_day_event(store_module):
    e = _new_event(store_module, all_day=True, start="2026-08-02", end="2026-08-02")
    stored = store_module.upsert(e)
    assert stored["all_day"] is True
    assert stored["start"] == "2026-08-02"
    assert stored["end"] == "2026-08-02"


def test_upsert_with_url(store_module):
    e = _new_event(store_module, url="https://example.com/meet")
    stored = store_module.upsert(e)
    assert stored["url"] == "https://example.com/meet"


def test_upsert_rejects_stale_write(store_module):
    e = _new_event(store_module)
    stored = store_module.upsert(e)

    stale = dict(stored)
    stale["title"] = "Standup v2"
    # Same updated_at as the stored version — not strictly newer, so it's stale.
    with pytest.raises(store_module.Stale) as exc:
        store_module.upsert(stale)
    assert exc.value.current["title"] == "Standup"


def test_upsert_requires_title(store_module):
    e = _new_event(store_module, title="  ")
    with pytest.raises(ValueError):
        store_module.upsert(e)


def test_upsert_requires_start(store_module):
    e = _new_event(store_module, start=None)
    with pytest.raises(ValueError):
        store_module.upsert(e)


def test_upsert_rejects_end_before_start(store_module):
    e = _new_event(store_module, start="2026-08-02T10:00:00Z", end="2026-08-02T09:00:00Z")
    with pytest.raises(ValueError):
        store_module.upsert(e)


def test_soft_delete_produces_tombstone(store_module):
    e = _new_event(store_module, title="Standup")
    store_module.upsert(e)

    tombstone = store_module.soft_delete(e["id"])
    assert tombstone["deleted"] is True
    assert tombstone["title"] == "Standup"  # content preserved on the tombstone

    assert store_module.get(e["id"])["deleted"] is True


def test_soft_delete_rejects_stale_updated_at(store_module):
    e = _new_event(store_module, title="Standup")
    stored = store_module.upsert(e)
    with pytest.raises(store_module.Stale) as exc:
        store_module.soft_delete(e["id"], updated_at=stored["updated_at"])
    assert exc.value.current["title"] == "Standup"
    assert store_module.get(e["id"])["deleted"] is False


def test_soft_delete_unknown_id_raises(store_module):
    with pytest.raises(KeyError):
        store_module.soft_delete("no-such-id")


def test_list_events_adopts_natively_created(store_module, fake_ek):
    from Foundation import NSDate

    native = fake_ek.new_event(None)
    native.setTitle_("Native meeting")
    native.setStartDate_(NSDate.date())
    native.setEndDate_(NSDate.date())
    native.setCalendar_(fake_ek.get_or_create_calendar(None, "Calendar"))
    fake_ek.save_event(None, native)

    results = store_module.list_events()
    assert len(results) == 1
    assert results[0]["title"] == "Native meeting"
    assert results[0]["id"] != native.calendarItemIdentifier()  # our own id, not EventKit's

    # Second call reuses the same adopted id rather than adopting again.
    again = store_module.list_events()
    assert again[0]["id"] == results[0]["id"]


def test_list_events_self_heals_native_deletion(store_module, fake_ek):
    e = _new_event(store_module, title="Standup")
    store_module.upsert(e)
    assert len(store_module.list_events(include_deleted=False)) == 1

    # Deleted directly in Calendar.app, bypassing our soft_delete.
    ek_identifier = store_module._sidecar.by_id(e["id"])["ek_identifier"]
    fake_ek.events[ek_identifier]._removed = True

    live = store_module.list_events(include_deleted=False)
    assert live == []
    all_results = store_module.list_events(include_deleted=True)
    assert all_results[0]["deleted"] is True
    assert all_results[0]["title"] == "Standup"


def test_list_events_does_not_tombstone_out_of_window(store_module):
    # Event far in the future, outside the narrow window we'll query.
    e = _new_event(store_module, title="Far future", start="2030-01-01T10:00:00Z", end="2030-01-01T11:00:00Z")
    store_module.upsert(e)

    results = store_module.list_events(
        start="2026-07-01T00:00:00Z", end="2026-07-31T00:00:00Z", include_deleted=True
    )
    # Out of range: not returned, and crucially NOT falsely tombstoned.
    assert all(r["id"] != e["id"] for r in results)
    assert store_module.get(e["id"])["deleted"] is False


def test_list_events_since_filters_unchanged(store_module):
    e1 = _new_event(store_module, title="First")
    store_module.upsert(e1)
    cursor = store_module.now_after(e1["updated_at"])
    e2 = _new_event(store_module, title="Second", updated_at=cursor)
    store_module.upsert(e2)

    results = store_module.list_events(since=e1["updated_at"], include_deleted=False)
    assert {r["title"] for r in results} == {"Second"}


def test_list_events_filters_by_calendar(store_module):
    store_module.upsert(_new_event(store_module, title="Work thing", calendar="Work"))
    store_module.upsert(_new_event(store_module, title="Home thing", calendar="Home"))

    work = store_module.list_events(calendar_name="Work", include_deleted=False)
    assert {r["title"] for r in work} == {"Work thing"}


def test_list_calendars(store_module):
    store_module.upsert(_new_event(store_module, calendar="Work"))
    store_module.upsert(_new_event(store_module, calendar="Home"))
    assert store_module.list_calendars() == ["Home", "Work"]


def test_gc_tombstones_removes_old_only(store_module):
    e = _new_event(store_module, title="Old event")
    store_module.upsert(e)
    tombstone = store_module.soft_delete(e["id"])

    # Not old enough yet.
    assert store_module.gc_tombstones(older_than_days=30) == 0

    # Force the tombstone to look old by rewriting its updated_at directly.
    store_module._sidecar.put(
        tombstone["id"], None, "2000-01-01T00:00:00Z", deleted=True, content=tombstone
    )
    assert store_module.gc_tombstones(older_than_days=30) == 1
    assert store_module.get(tombstone["id"]) is None
