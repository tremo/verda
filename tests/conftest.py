from __future__ import annotations

import json
import sqlite3

import pytest

from verda.storage import connect, initialize


@pytest.fixture
def engine(tmp_path):
    engine = connect(f"sqlite:///{tmp_path / 'v2.sqlite'}")
    initialize(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE items(id TEXT PRIMARY KEY, lifecycle TEXT, revision INTEGER,
                           legacy_json TEXT, updated_at TEXT, parcel_key TEXT);
        CREATE TABLE checks(item_id TEXT, kind TEXT, status TEXT, evidence_json TEXT);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY, item_id TEXT, action TEXT, state TEXT);
        CREATE TABLE outbox(id INTEGER PRIMARY KEY, item_id TEXT, state TEXT, message_text TEXT,
                            sender_account TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY, at TEXT, item_id TEXT, task_id INTEGER,
                            event TEXT, payload_json TEXT);
        CREATE TABLE listing_feedback(listing_id TEXT, note TEXT);
    """)
    con.executemany("INSERT INTO items VALUES(?,?,?,?,?,?)", [
        ("9000000001", "verifying", 3, json.dumps({"title": "Sentetik aday", "price_tl": 8_000_000}), "2026-10-04T10:00:00+03:00", None),
        ("9000000002", "excluded", 1, json.dumps({"title": "Sentetik elenmiş ilan"}), "2026-10-04T11:00:00+03:00", "DEMO:1/2"),
    ])
    con.execute("INSERT INTO checks VALUES(?,?,?,?)", ("9000000001", "natural_sit", "verified_by_statement", '["not-copied.json"]'))
    con.execute("INSERT INTO tasks VALUES(1,'9000000001','final_review','succeeded')")
    con.executemany("INSERT INTO outbox VALUES(?,?,?,?,?)", [
        (1, "9000000001", "confirmed", "Sentetik gönderilmiş mesaj", "demo-account-a"),
        (2, "9000000001", "prepared", "Sentetik taslak", "demo-account-a"),
        (3, "9000000001", "delivery_unknown", "Sentetik belirsiz mesaj", "demo-account-a"),
    ])
    con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", [
        (1, "2026-10-04T08:00:00Z", "9000000001", None, "listing_read", '{"demo":true}'),
        (2, "2026-10-04T09:00:00Z", "9000000001", 1, "task_succeeded", '{"decision":"conditional"}'),
        (3, "2026-10-04T09:00:00Z", "9000000001", None, "delivery_unknown", '{"outbox_id":3}'),
        (4, "2026-10-04T09:00:00Z", None, None, "project_event", '{}'),
    ])
    con.execute("INSERT INTO listing_feedback VALUES('9000000001','Sentetik kullanıcı notu')")
    con.commit()
    con.close()
    return path


@pytest.fixture
def projection():
    return {
        "schemaVersion": 2,
        "sourceUpdatedAt": "2026-10-04T10:00:00Z",
        "generatedAt": "2026-10-04T10:01:00Z",
        "records": [{"id": "9000000001", "lifecycle": "verifying", "price": 8_000_000,
                     "routeMin": 41, "routeMax": 48, "score": None,
                     "futureNestedField": {"keep": [None, True, "Türkçe"]}}],
        "status": {"funnel": {"positive": 0}, "pending": 1},
        "evidenceAssets": {"demo": {"mime": "image/png", "chunks": []}},
        "unknownRootField": "preserved",
    }
