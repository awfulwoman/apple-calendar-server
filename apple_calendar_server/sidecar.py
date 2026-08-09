"""Local bookkeeping EventKit can't hold itself: the mapping from a caller's
client-chosen `id` to EventKit's system-assigned `calendarItemIdentifier`, the
app-authoritative `updated_at` used for LWW (EventKit's lastModifiedDate is
system-set, not caller-set), and tombstones (EventKit has no CalDAV-style
`STATUS:CANCELLED` — a removed event is just gone, so `content_json` caches the
last-known dict to still answer `deleted:true` sync entries with real content)."""
from __future__ import annotations
import json
import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ek_identifier TEXT UNIQUE,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    content_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ek_identifier ON events(ek_identifier);
"""


class Sidecar:
    def __init__(self, db_path: str):
        path = os.path.expanduser(db_path)
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def put(self, id: str, ek_identifier: str | None, updated_at: str, deleted: bool = False, content: dict | None = None) -> None:
        content_json = json.dumps(content) if content is not None else None
        self._conn.execute(
            "INSERT INTO events (id, ek_identifier, updated_at, deleted, content_json) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ek_identifier=excluded.ek_identifier, "
            "updated_at=excluded.updated_at, deleted=excluded.deleted, "
            "content_json=COALESCE(excluded.content_json, events.content_json)",
            (id, ek_identifier, updated_at, int(deleted), content_json),
        )
        self._conn.commit()

    def by_id(self, id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, ek_identifier, updated_at, deleted, content_json FROM events WHERE id = ?", (id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def by_ek_identifier(self, ek_identifier: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, ek_identifier, updated_at, deleted, content_json FROM events WHERE ek_identifier = ?",
            (ek_identifier,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def all(self) -> list[dict]:
        rows = self._conn.execute("SELECT id, ek_identifier, updated_at, deleted, content_json FROM events").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def tombstones_older_than(self, cutoff: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, ek_identifier, updated_at, deleted, content_json FROM events WHERE deleted = 1 AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_row(self, id: str) -> None:
        self._conn.execute("DELETE FROM events WHERE id = ?", (id,))
        self._conn.commit()

    @staticmethod
    def _row_to_dict(row) -> dict:
        id, ek_identifier, updated_at, deleted, content_json = row
        return {
            "id": id,
            "ek_identifier": ek_identifier,
            "updated_at": updated_at,
            "deleted": bool(deleted),
            "content": json.loads(content_json) if content_json else None,
        }
