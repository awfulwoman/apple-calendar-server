from starlette.testclient import TestClient

AUTH = {"Authorization": "Bearer test-token"}


def _body(store_module, **overrides):
    now = store_module.now_utc()
    b = {
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
    b.update(overrides)
    return b


def test_requires_bearer_token(app):
    client = TestClient(app)
    assert client.get("/events").status_code == 401


def test_rejects_wrong_token(app):
    client = TestClient(app)
    assert client.get("/events", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_non_ascii_token_is_rejected_rather_than_crashing(app):
    """Headers travel as latin-1, so Starlette can hand the handler a token with
    non-ASCII characters — and `hmac.compare_digest` refuses to compare those,
    raising TypeError out of an endpoint any unauthenticated caller can reach.
    Sent as raw bytes because an httpx `str` header would fail encoding client-side
    and never exercise the server at all."""
    client = TestClient(app)
    resp = client.get("/events", headers={b"Authorization": b"Bearer t\xf6k\xe9n"})
    assert resp.status_code == 401


def test_put_get_list_roundtrip(app, store_module):
    client = TestClient(app)
    id = store_module.new_id()
    put_resp = client.put(f"/events/{id}", json=_body(store_module, title="Standup"), headers=AUTH)
    assert put_resp.status_code == 200
    assert put_resp.json()["title"] == "Standup"

    get_resp = client.get(f"/events/{id}", headers=AUTH)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == id

    list_resp = client.get("/events", headers=AUTH)
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert "server_time" in payload
    assert any(r["id"] == id for r in payload["events"])


def test_put_stale_returns_409(app, store_module):
    client = TestClient(app)
    id = store_module.new_id()
    body = _body(store_module, title="Standup")
    client.put(f"/events/{id}", json=body, headers=AUTH)

    # Same updated_at again — not strictly newer.
    resp = client.put(f"/events/{id}", json={**body, "title": "Standup v2"}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["current"]["title"] == "Standup"


def test_put_rejects_malformed_updated_at(app, store_module):
    """The boundary a real client actually hits. Accepting this stores a timestamp
    that sorts above every ISO one, locking the event out of all later writes."""
    client = TestClient(app)
    id = store_module.new_id()
    resp = client.put(
        f"/events/{id}", json=_body(store_module, updated_at="not-a-timestamp"), headers=AUTH
    )
    assert resp.status_code == 400


def test_get_missing_returns_404(app):
    client = TestClient(app)
    assert client.get("/events/no-such-id", headers=AUTH).status_code == 404


def test_validation_error_returns_400(app, store_module):
    client = TestClient(app)
    id = store_module.new_id()
    now = store_module.now_utc()
    resp = client.put(f"/events/{id}", json={"title": "No dates", "created_at": now, "updated_at": now}, headers=AUTH)
    assert resp.status_code == 400


def test_delete_roundtrip(app, store_module):
    client = TestClient(app)
    id = store_module.new_id()
    client.put(f"/events/{id}", json=_body(store_module), headers=AUTH)

    del_resp = client.delete(f"/events/{id}", headers=AUTH)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    assert client.delete(f"/events/{id}", headers=AUTH).status_code == 404


def test_calendars_endpoint(app, store_module):
    client = TestClient(app)
    client.put(f"/events/{store_module.new_id()}", json=_body(store_module, calendar="Work"), headers=AUTH)
    resp = client.get("/calendars", headers=AUTH)
    assert resp.status_code == 200
    assert "Work" in resp.json()["calendars"]


def test_gc_tombstones_endpoint(app, store_module):
    client = TestClient(app)
    id = store_module.new_id()
    client.put(f"/events/{id}", json=_body(store_module), headers=AUTH)
    tombstone = client.delete(f"/events/{id}", headers=AUTH).json()
    store_module._sidecar.put(tombstone["id"], None, "2000-01-01T00:00:00Z", deleted=True, content=tombstone)

    resp = client.post("/admin/gc_tombstones", json={"older_than_days": 30}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1


def _make_tombstone(client, store_module) -> str:
    id = store_module.new_id()
    client.put(f"/events/{id}", json=_body(store_module), headers=AUTH)
    client.delete(f"/events/{id}", headers=AUTH)
    return id


def test_gc_rejects_negative_older_than_days(app, store_module):
    """A negative age puts the cutoff in the future, so `updated_at < cutoff` matches
    every tombstone — one request wipes the delete-propagation state of every client
    still syncing."""
    client = TestClient(app)
    id = _make_tombstone(client, store_module)

    resp = client.post("/admin/gc_tombstones", json={"older_than_days": -99999}, headers=AUTH)
    assert resp.status_code == 400
    assert client.get(f"/events/{id}", headers=AUTH).status_code == 200  # tombstone survives


def test_gc_rejects_non_integer_older_than_days(app, store_module):
    """`int(body.get(...))` raises straight out of the handler."""
    client = TestClient(app)
    resp = client.post("/admin/gc_tombstones", json={"older_than_days": "abc"}, headers=AUTH)
    assert resp.status_code == 400


def test_gc_rejects_malformed_json(app, store_module):
    """This handler discards the parse error the other endpoints return, so a body
    that is not JSON at all quietly runs the default prune instead of failing."""
    client = TestClient(app)
    resp = client.post(
        "/admin/gc_tombstones",
        content=b"{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
