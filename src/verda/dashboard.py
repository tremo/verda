"""Lossless boundary for the existing schemaVersion=2 dashboard projection.

This stores and validates an existing projection. It neither invents a replacement
UI nor publishes to Firestore/Pages. V2-native projection generation follows later.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy.engine import Engine

from verda.legacy import digest
from verda.storage import DashboardSnapshot, transaction, utcnow


class DashboardRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: StrictStr
    lifecycle: StrictStr


class DashboardProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    schemaVersion: StrictInt = Field(ge=2, le=2)
    sourceUpdatedAt: StrictStr
    records: list[DashboardRecord]
    status: dict

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [row.id for row in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("Dashboard contains duplicate listing IDs")
        return self


def store_projection(path: Path, engine: Engine) -> dict:
    payload = json.loads(path.read_text())
    DashboardProjection.model_validate(payload)
    projection_id = digest(payload)
    with transaction(engine) as session:
        existing = session.get(DashboardSnapshot, projection_id)
        if not existing:
            # Store original data, not model_dump: preserve unknown fields exactly.
            session.add(DashboardSnapshot(id=projection_id, imported_at=utcnow(), payload=payload))
    return {"projection_id": projection_id, "already_imported": bool(existing), "records": len(payload["records"])}
