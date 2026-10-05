from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from verda.agents import catalog
from verda.policy import Policy, ScreeningFacts, evaluate
from verda.storage import Case, DashboardSnapshot, Snapshot, SourceRow, TimelineEvent
from verda.workflow import WorkflowStore, research_plan


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=128, pattern=r"\S")


def create_app(engine: Engine, token: str, workflow_store: WorkflowStore | None = None, runtime=None) -> FastAPI:
    if len(token) < 32:
        raise ValueError("An API token of at least 32 characters is required")
    app = FastAPI(title="Verda v2 — shadow backend", version="0.3.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    assets = Path(__file__).parent / "ui"
    app.mount("/control/assets", StaticFiles(directory=assets), name="control-assets")

    @app.get("/control")
    def control_page():
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/catalog")
    def control_data():
        # Read-only definitions only. No listing data, credentials or model calls.
        from verda.control import control_catalog
        from verda.runtime import Runtime
        return control_catalog(runtime or Runtime())

    def authorize(authorization: str | None = Header(default=None)):
        expected = "Bearer " + token
        if not authorization or not secrets.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Authentication required")

    def local_command(origin: str | None = Header(default=None)):
        # This API has no browser writer yet. Only token-authenticated local
        # clients without an Origin header may create/cancel workflow runs.
        if origin is not None:
            raise HTTPException(403, "Browser-origin commands are not enabled")

    def workflows() -> WorkflowStore:
        if workflow_store is None:
            raise HTTPException(503, "Workflow storage is not configured")
        return workflow_store

    def database():
        with Session(engine) as session:
            yield session

    def require_snapshot(session: Session, snapshot_id: str):
        snapshot = session.get(Snapshot, snapshot_id)
        if not snapshot:
            raise HTTPException(404, "Snapshot not found")
        return snapshot

    def require_case(session: Session, snapshot_id: str, listing_id: str):
        row = session.get(Case, (snapshot_id, listing_id))
        if not row:
            raise HTTPException(404, "Case not found")
        return row

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "shadow", "external_actions_enabled": False}

    @app.get("/api/agents", dependencies=[Depends(authorize)])
    def agents():
        return {"agents": catalog()}

    @app.get("/api/policy", dependencies=[Depends(authorize)])
    def policy():
        return Policy()

    @app.post("/api/policy/preview", dependencies=[Depends(authorize)])
    def preview(facts: ScreeningFacts):
        return {"preview_only": True, "evidence_existence_checked": False, "decision": evaluate(facts)}

    @app.get("/api/snapshots", dependencies=[Depends(authorize)])
    def snapshots(session: Session = Depends(database)):
        return [{"id": row.id, "imported_at": row.imported_at, "counts": row.counts,
                 "warnings": row.warnings, "mode": row.mode}
                for row in session.scalars(select(Snapshot).order_by(Snapshot.imported_at.desc()))]

    @app.get("/api/snapshots/{snapshot_id}/cases", dependencies=[Depends(authorize)])
    def cases(snapshot_id: str, lifecycle: str | None = None,
              offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
              session: Session = Depends(database)):
        require_snapshot(session, snapshot_id)
        query = select(Case).where(Case.snapshot_id == snapshot_id)
        if lifecycle is not None:
            query = query.where(Case.lifecycle == lifecycle)
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.order_by(Case.listing_id).offset(offset).limit(limit))
        return {"total": total, "offset": offset, "limit": limit,
                "cases": [{"listing_id": row.listing_id, "title": row.title,
                           "lifecycle": row.lifecycle, "parcel_key": row.parcel_key,
                           "source_revision": row.source_revision, "next_action": row.next_action,
                           "execution_mode": "shadow", "updated_at": row.updated_at} for row in rows]}

    @app.get("/api/snapshots/{snapshot_id}/cases/{listing_id}", dependencies=[Depends(authorize)])
    def case(snapshot_id: str, listing_id: str, session: Session = Depends(database)):
        row = require_case(session, snapshot_id, listing_id)
        source_rows = session.scalars(select(SourceRow).where(
            SourceRow.snapshot_id == snapshot_id, SourceRow.subject_id == listing_id,
            SourceRow.table_name.in_(["checks", "tasks", "gates", "outbox", "listing_feedback"])))
        grouped = {}
        for source_row in source_rows:
            grouped.setdefault(source_row.table_name, []).append(source_row.payload)
        return {"listing_id": row.listing_id, "lifecycle": row.lifecycle, "next_action": row.next_action,
                "execution_mode": "shadow", "historical_evidence_not_reverified": True,
                "history": grouped}

    @app.get("/api/snapshots/{snapshot_id}/cases/{listing_id}/events", dependencies=[Depends(authorize)])
    def events(snapshot_id: str, listing_id: str, after_id: int = Query(default=0, ge=0),
               limit: int = Query(default=50, ge=1, le=200), session: Session = Depends(database)):
        require_case(session, snapshot_id, listing_id)
        query = select(TimelineEvent).where(
            TimelineEvent.snapshot_id == snapshot_id, TimelineEvent.listing_id == listing_id,
            TimelineEvent.source_event_id > after_id).order_by(TimelineEvent.source_event_id).limit(limit + 1)
        rows = list(session.scalars(query))
        result = [{"id": row.source_event_id, "at": row.at, "kind": row.kind,
                   "task_id": row.source_task_id, "payload": row.payload} for row in rows[:limit]]
        return {"events": result, "has_more": len(rows) > limit,
                "next_after_id": result[-1]["id"] if result else after_id}

    @app.get("/api/dashboard/{projection_id}", dependencies=[Depends(authorize)])
    def dashboard(projection_id: str, session: Session = Depends(database)):
        row = session.get(DashboardSnapshot, projection_id)
        if not row:
            raise HTTPException(404, "Dashboard projection not found")
        return row.payload

    @app.post("/api/workflows/from-case/{snapshot_id}/{listing_id}",
              dependencies=[Depends(authorize), Depends(local_command)])
    def start_workflow(snapshot_id: str, listing_id: str, command: WorkflowRequest,
                       session: Session = Depends(database), store: WorkflowStore = Depends(workflows)):
        row = require_case(session, snapshot_id, listing_id)
        if row.lifecycle == "excluded":
            raise HTTPException(409, "Excluded case requires a separate reconsideration decision")
        plan = research_plan(f"{snapshot_id}/{listing_id}", row.source_revision, "shadow")
        try:
            run_id = store.create_run(plan, command.request_key)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"run_id": run_id, "mode": "shadow", "external_actions_enabled": False}

    @app.get("/api/workflows/{run_id}", dependencies=[Depends(authorize)])
    def workflow(run_id: str, after_event_id: int = Query(default=0, ge=0),
                 limit: int = Query(default=100, ge=1, le=200), store: WorkflowStore = Depends(workflows)):
        try:
            return store.view(run_id, after_event_id=after_event_id, limit=limit)
        except KeyError as error:
            raise HTTPException(404, "Workflow not found") from error

    @app.post("/api/workflows/{run_id}/cancel", dependencies=[Depends(authorize), Depends(local_command)])
    def cancel_workflow(run_id: str, store: WorkflowStore = Depends(workflows)):
        try:
            return {"run_id": run_id, "cancelled": store.cancel(run_id)}
        except KeyError as error:
            raise HTTPException(404, "Workflow not found") from error

    return app
