"""SQLite store (spec 5.5). One connection, autocommit per write via `with conn:`."""
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVENT_COLS = ["events_id", "position_id", "venue_name", "address", "city", "start_ts", "end_ts",
              "brand", "recap_def_id", "recap_def_name", "site_status", "refreshed_at"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events({', '.join(c + (' PRIMARY KEY' if c == 'events_id' else '') for c in EVENT_COLS)});
CREATE TABLE IF NOT EXISTS answers(events_id, instance_id, value, estimated INTEGER, updated_at,
    PRIMARY KEY(events_id, instance_id));
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY AUTOINCREMENT, events_id, slot, path,
    site_document_id, site_document_uuid, uploaded_at);
CREATE TABLE IF NOT EXISTS upload_links(token PRIMARY KEY, events_id, expires_at);
CREATE TABLE IF NOT EXISTS drafts(events_id PRIMARY KEY, partial_id, fingerprint, saved_at, last_error);
CREATE TABLE IF NOT EXISTS meta(key PRIMARY KEY, value);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript(SCHEMA)

    def _one(self, sql, *args):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def _all(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args)]

    def upsert_events(self, rows: list[dict]) -> None:
        q = f"INSERT OR REPLACE INTO events VALUES({','.join('?' * len(EVENT_COLS))})"
        with self.db:
            self.db.executemany(q, [[r.get(c) for c in EVENT_COLS] for r in rows])

    def events(self, only_pending: bool = True) -> list[dict]:
        where = "WHERE site_status IN ('needs_recap','draft_saved')" if only_pending else ""
        return self._all(f"SELECT * FROM events {where} ORDER BY start_ts DESC")

    def event(self, events_id: str) -> dict | None:
        return self._one("SELECT * FROM events WHERE events_id=?", events_id)

    def answers(self, events_id: str) -> dict[str, str]:
        return {r["instance_id"]: r["value"]
                for r in self._all("SELECT instance_id, value FROM answers WHERE events_id=?", events_id)}

    def set_answers(self, events_id: str, values: dict[str, str], estimated: set[str]) -> None:
        now = _now().isoformat()
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?)",
                [(events_id, k, v, int(k in estimated), now) for k, v in values.items()])

    def delete_answers(self, events_id: str, instance_ids: list[str]) -> None:
        with self.db:
            self.db.executemany("DELETE FROM answers WHERE events_id=? AND instance_id=?",
                                [(events_id, k) for k in instance_ids])

    def add_photo(self, events_id: str, slot: str, path: str) -> int:
        with self.db:
            return self.db.execute("INSERT INTO photos(events_id, slot, path) VALUES(?,?,?)",
                                   (events_id, slot, path)).lastrowid

    def photos(self, events_id: str, slot: str | None = None) -> list[dict]:
        if slot is None:
            return self._all("SELECT * FROM photos WHERE events_id=? ORDER BY id", events_id)
        return self._all("SELECT * FROM photos WHERE events_id=? AND slot=? ORDER BY id", events_id, slot)

    def mark_uploaded(self, photo_id: int, doc_id: str, uuid: str | None) -> None:
        with self.db:
            self.db.execute("UPDATE photos SET site_document_id=?, site_document_uuid=?, uploaded_at=? WHERE id=?",
                            (str(doc_id), uuid, _now().isoformat(), photo_id))

    def delete_photo(self, photo_id: int) -> dict:
        row = self._one("SELECT * FROM photos WHERE id=?", photo_id)
        if row is None:
            raise KeyError(photo_id)
        with self.db:
            self.db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        return row

    def is_our_document(self, doc_id: str) -> bool:
        return self._one("SELECT 1 FROM photos WHERE site_document_id=?", str(doc_id)) is not None

    def new_link(self, events_id: str, minutes: int = 60) -> str:
        token = secrets.token_urlsafe(16)
        with self.db:
            self.db.execute("INSERT INTO upload_links VALUES(?,?,?)",
                            (token, events_id, (_now() + timedelta(minutes=minutes)).isoformat()))
        return token

    def link_event(self, token: str) -> str | None:
        r = self._one("SELECT events_id, expires_at FROM upload_links WHERE token=?", token)
        if r and datetime.fromisoformat(r["expires_at"]) > _now():
            return r["events_id"]
        return None

    def set_draft(self, events_id: str, partial_id: str | None, fingerprint: str | None, error: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?,?,?)",
                            (events_id, partial_id, fingerprint, _now().isoformat(), error))

    def draft(self, events_id: str) -> dict | None:
        return self._one("SELECT * FROM drafts WHERE events_id=?", events_id)

    def meta_get(self, key: str) -> str | None:
        r = self._one("SELECT value FROM meta WHERE key=?", key)
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
