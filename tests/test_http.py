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
