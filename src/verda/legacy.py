"""Import an immutable historical snapshot. Never call the old controller."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from sqlalchemy.engine import Engine

from verda.storage import Case, Snapshot, SourceRow, TimelineEvent, transaction, utcnow


TABLES = (
    "items", "checks", "tasks", "outbox", "events", "gates", "resources",
    "project_tasks", "listing_feedback", "meta", "runs",
)
REQUIRED = {"items", "checks", "tasks", "outbox", "events"}
REQUIRED_COLUMNS = {
    "items": {"id", "lifecycle", "revision", "legacy_json", "updated_at"},
    "checks": {"item_id", "kind", "status", "evidence_json"},
    "tasks": {"id", "item_id", "action", "state"},
    "outbox": {"id", "item_id", "state"},
    "events": {"id", "at", "item_id", "task_id", "event", "payload_json"},
}


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decoded(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def read_source(source: Path) -> tuple[dict, dict]:
    source = source.expanduser().resolve(strict=True)
    # mode=ro handles WAL correctly. Do not use immutable=1 for a live WAL DB.
    con = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        if [row[0] for row in con.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("Source integrity check failed")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Source foreign key check failed")
        found = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED <= found:
            raise ValueError("Source lacks required legacy tables")
        rows, schema = {}, {}
        for name in TABLES:
            if name not in found:
                continue
            # name comes exclusively from the closed TABLES tuple above.
            columns = [row[1] for row in con.execute(f'PRAGMA table_info("{name}")')]
            if not REQUIRED_COLUMNS.get(name, set()) <= set(columns):
                raise ValueError(f"Source table {name} lacks required columns")
            schema[name] = columns
            rows[name] = sorted(
                (dict(row) for row in con.execute(f'SELECT * FROM "{name}"')),
                key=canonical,
            )
        return rows, schema
    finally:
        con.close()


def import_snapshot(source: Path, engine: Engine) -> dict:
    if engine.url.get_backend_name() == "sqlite" and engine.url.database != ":memory:":
        if source.resolve() == Path(engine.url.database).resolve():
            raise ValueError("Source and destination must be different databases")
    rows, schema = read_source(source)
    snapshot_id = digest({"schema": schema, "rows": rows})
    counts = {name: len(data) for name, data in rows.items()}
    counts["items_by_lifecycle"] = dict(Counter(row["lifecycle"] for row in rows["items"]))
    warnings = {
        "historical_evidence_not_reverified": True,
        "prepared_messages_quarantined": sum(row["state"] == "prepared" for row in rows["outbox"]),
        "delivery_unknown_preserved": sum(row["state"] == "delivery_unknown" for row in rows["outbox"]),
        "malformed_listing_payloads": 0,
        "malformed_event_payloads": 0,
    }
    with transaction(engine) as session:
        if session.get(Snapshot, snapshot_id):
            return {"snapshot_id": snapshot_id, "already_imported": True, "counts": counts}
        snapshot = Snapshot(id=snapshot_id, imported_at=utcnow(), source_schema=schema,
                            counts=counts, warnings={}, mode="shadow")
        session.add(snapshot)
        session.flush()
        for name, data in rows.items():
            for ordinal, row in enumerate(data):
                subject = row.get("item_id", row.get("listing_id"))
                if name == "items":
                    subject = row["id"]
                session.add(SourceRow(snapshot_id=snapshot_id, table_name=name,
                                      row_key=digest([ordinal, row]),
                                      subject_id=str(subject) if subject is not None else None,
                                      payload=row))
        for row in rows["items"]:
            record = decoded(row["legacy_json"])
            if not isinstance(record, dict):
                warnings["malformed_listing_payloads"] += 1
                record = {}
            session.add(Case(snapshot_id=snapshot_id, listing_id=str(row["id"]),
                             lifecycle=row["lifecycle"], source_revision=row["revision"],
                             title=record.get("title") if isinstance(record.get("title"), str) else None,
                             parcel_key=row.get("parcel_key"), updated_at=row["updated_at"],
                             next_action="none" if row["lifecycle"] == "excluded" else "migration_review"))
        for row in rows["events"]:
            payload = decoded(row["payload_json"])
            if isinstance(payload, str):
                warnings["malformed_event_payloads"] += 1
            session.add(TimelineEvent(snapshot_id=snapshot_id, source_event_id=row["id"],
                                      listing_id=str(row["item_id"]) if row["item_id"] is not None else None,
                                      at=row["at"], kind=row["event"], source_task_id=row["task_id"],
                                      payload=payload))
        snapshot.warnings = warnings
    return {"snapshot_id": snapshot_id, "already_imported": False, "counts": counts, "warnings": warnings}
