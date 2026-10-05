from __future__ import annotations

import hashlib
import sqlite3

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from verda.legacy import import_snapshot
from verda.storage import Case, Snapshot, SourceRow, TimelineEvent


def test_import_preserves_source_and_history(source, engine):
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = import_snapshot(source, engine)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["counts"]["items"] == 2
    assert result["counts"]["events"] == 4
    assert result["warnings"]["prepared_messages_quarantined"] == 1
    assert result["warnings"]["delivery_unknown_preserved"] == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TimelineEvent)) == 4
        cases = list(session.scalars(select(Case).order_by(Case.listing_id)))
        assert cases[0].lifecycle == "verifying"  # a final task's success is not approval
        assert cases[0].next_action == "migration_review"
        assert cases[1].next_action == "none"
        messages = list(session.scalars(select(SourceRow).where(SourceRow.table_name == "outbox")))
        assert {row.payload["state"] for row in messages} == {"confirmed", "prepared", "delivery_unknown"}
        assert all(row.payload["sender_account"] == "demo-account-a" for row in messages)
        check = session.scalar(select(SourceRow).where(SourceRow.table_name == "checks"))
        assert check.payload["status"] == "verified_by_statement"


def test_identical_import_does_not_duplicate(source, engine):
    first = import_snapshot(source, engine)
    second = import_snapshot(source, engine)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["already_imported"] is True
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 1
        assert session.scalar(select(func.count()).select_from(Case)) == 2


def test_changed_source_creates_new_snapshot_without_overwriting(source, engine):
    first = import_snapshot(source, engine)
    with sqlite3.connect(source) as con:
        con.execute("UPDATE items SET revision=4 WHERE id='9000000001'")
    second = import_snapshot(source, engine)
    assert first["snapshot_id"] != second["snapshot_id"]
    with Session(engine) as session:
        assert session.get(Case, (first["snapshot_id"], "9000000001")).source_revision == 3
        assert session.get(Case, (second["snapshot_id"], "9000000001")).source_revision == 4


def test_malformed_prose_preserved_without_becoming_a_fact(source, engine):
    with sqlite3.connect(source) as con:
        con.execute("UPDATE items SET legacy_json='not json' WHERE id='9000000001'")
        con.execute("UPDATE events SET payload_json='not json' WHERE id=2")
    result = import_snapshot(source, engine)
    assert result["warnings"]["malformed_listing_payloads"] == 1
    assert result["warnings"]["malformed_event_payloads"] == 1
    with Session(engine) as session:
        assert session.get(Case, (result["snapshot_id"], "9000000001")).title is None
        assert session.get(TimelineEvent, (result["snapshot_id"], 2)).payload == "not json"


def test_invalid_schema_imports_nothing(source, engine):
    with sqlite3.connect(source) as con:
        con.execute("ALTER TABLE outbox RENAME TO removed_outbox")
    with pytest.raises(ValueError, match="required legacy tables"):
        import_snapshot(source, engine)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 0


def test_same_database_rejected(engine):
    from pathlib import Path
    with pytest.raises(ValueError, match="different databases"):
        import_snapshot(Path(engine.url.database), engine)


def test_read_source_sees_wal_without_modifying_it(source, engine):
    con = sqlite3.connect(source)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("UPDATE items SET revision=9 WHERE id='9000000001'")
    con.commit()
    before = source.read_bytes()
    try:
        result = import_snapshot(source, engine)
        with Session(engine) as session:
            assert session.get(Case, (result["snapshot_id"], "9000000001")).source_revision == 9
        assert source.read_bytes() == before
    finally:
        con.close()
