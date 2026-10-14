from __future__ import annotations

import re

import secrets
import sqlite3
import time
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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



class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    objective: str = Field(min_length=1, max_length=4000)
    inputs: dict = Field(default_factory=dict)
    request_key: str = Field(min_length=1, max_length=256)
    listing_ref: str | None = Field(default=None, max_length=256)
    executor_tool: str | None = None
    priority: int = Field(default=50, ge=0, le=100)


class AgentEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=128)
    payload: dict


class AgentEditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=12000)
    description: str = Field(min_length=1, max_length=1000)
    expected_revision: str = Field(min_length=64, max_length=64)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str = Field(min_length=1, max_length=128)
    listing_id: str | None = Field(default=None, max_length=100)


class ListingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=2, max_length=40)
    value: bool | float | str
    note: str = Field(min_length=3, max_length=300)


class FlagClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=300)


class SettingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=3, max_length=120)
    value: bool | float | str | None = None


class OutboxResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivered: bool
    note: str = Field(min_length=3, max_length=300)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[SettingChange] = Field(min_length=1, max_length=60)


class AttentionAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=4, max_length=40)


class BrowserDriverRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    driver: Literal['codex', 'jev']


SESSION_SECONDS = 7 * 24 * 3600      # a personal panel on this Mac: a week, then a fresh link


def create_app(engine: Engine, token: str, workflow_store: WorkflowStore | None = None, runtime=None,
               viewer_nonce: str | None = None, agency_store=None, trust_loopback: bool = False) -> FastAPI:
    if len(token) < 32:
        raise ValueError("An API token of at least 32 characters is required")
    app = FastAPI(title="Verda v2 — shadow backend", version="0.4.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "verda.localhost", "testserver"])
    assets = Path(__file__).parent / "ui"
    app.mount("/control/assets", StaticFiles(directory=assets), name="control-assets")
    viewer_cookie = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    if agency_store is not None and engine.url.get_backend_name() == 'sqlite' and engine.url.database != ':memory:':
        agency_store.archive_path = Path(engine.url.database)
    viewer_state = {"nonce": viewer_nonce, "nonce_expires": time.time() + 600, "session_expires": 0}
    viewer_lock = threading.Lock()

    @app.middleware("http")
    async def private_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/", include_in_schema=False)
    def home(request: Request):
        """The plain address: the panel when this browser is signed in, otherwise how to get a fresh link."""
        cookie = request.cookies.get("verda_viewer", "")
        local = trust_loopback and request.client and request.client.host in ("127.0.0.1", "::1")
        if local or (time.time() < viewer_state["session_expires"] and secrets.compare_digest(cookie.encode(), viewer_cookie.encode())):
            return RedirectResponse("/control", status_code=303)
        return HTMLResponse(
            '<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Verda</title><body style="font:16px/1.5 system-ui;max-width:34rem;margin:12vh auto;padding:0 1rem">'
            '<h1>Verda</h1><p>Panel çalışıyor, ama bu tarayıcıda oturumun yok.</p>'
            '<p>Giriş için proje klasöründeki <b>deploy/Verda-Paneli.command</b> dosyasına çift tıkla; '
            'taze bir giriş bağlantısı üretip tarayıcıda açar.</p>'
            '<p style="color:#666">Ya da terminalde: <code>.venv/bin/verda panel --open</code></p></body></html>')

    @app.get("/control/unlock/{nonce}")
    def unlock(nonce: str):
        with viewer_lock:
            expected = viewer_state["nonce"]
            if not expected or time.time() > viewer_state["nonce_expires"] or not secrets.compare_digest(nonce.encode(), expected.encode()):
                raise HTTPException(403, "Local viewer link expired or already used")
            viewer_state["nonce"] = None
            viewer_state["session_expires"] = time.time() + SESSION_SECONDS
        response = RedirectResponse("/control/flows", status_code=303)
        response.set_cookie("verda_viewer", viewer_cookie, httponly=True, samesite="strict", max_age=SESSION_SECONDS, path="/")
        return response

    def viewer(request: Request):
        # A personal panel on this Mac: a request that really comes from this machine (loopback) needs no link. The host-name check
        # (no DNS rebinding) and, for every change, the same-page origin and CSRF token stay in force either way.
        if trust_loopback and request.client and request.client.host in ("127.0.0.1", "::1"):
            return
        cookie = request.cookies.get("verda_viewer", "")
        if time.time() >= viewer_state["session_expires"] or not secrets.compare_digest(cookie.encode(), viewer_cookie.encode()):
            raise HTTPException(401, "Open the one-time local viewer link generated by verda serve")

    def editor(request: Request, x_verda_csrf: str = Header(default='')):
        viewer(request)
        if request.headers.get('origin') != str(request.base_url).rstrip('/') or not secrets.compare_digest(x_verda_csrf.encode(), csrf_token.encode()):
            raise HTTPException(403, 'Yerel panel oturumu ve aynı sayfadan gönderilen istek gerekli.')

    @app.put('/control/private/agents/{key}', dependencies=[Depends(editor)])
    def edit_agent(key: str, change: AgentEditRequest):
        if agency_store is None:
            raise HTTPException(503, 'Agency not configured')
        from verda.agency.settings import save_agent
        try:
            return save_agent(agency_store, key, **change.model_dump())
        except KeyError:
            raise HTTPException(404, 'Agent bulunamadı')
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get('/control/private/research', dependencies=[Depends(viewer)])
    def research_view(offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=50)):
        if agency_store is None:
            raise HTTPException(503, 'Agency not configured')
        from verda.agency.research import audit
        return audit(agency_store, offset=offset, limit=limit)

    @app.put('/control/private/browser', dependencies=[Depends(editor)])
    def select_browser(command: BrowserDriverRequest):
        if agency_store is None:
            raise HTTPException(503, 'Agency not configured')
        from verda.agency.browser import BrowserBridge
        bridge = BrowserBridge(agency_store)
        try:
            bridge.select(command.driver)
        except ValueError as error:
            raise HTTPException(409, str(error))
        return bridge.overview()

    @app.post('/control/private/research/review', dependencies=[Depends(editor)])
    def review_research(command: ReviewRequest):
        if agency_store is None or not agency_store.supervisor_agent:
            raise HTTPException(503, 'Yönetici tanımlı değil')
        from verda.agency.research import request_review
        try:
            task_id = request_review(agency_store, command.listing_id, command.request_key)
        except ValueError as error:
            raise HTTPException(409, str(error))
        return {'task_id': task_id}

    @app.get("/control/flows")
    def flow_page():
        return FileResponse(assets / "flows.html")

    @app.get("/control")
    def control_page():
        return FileResponse(assets / "agency.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/legacy-definitions")
    def old_definitions():
        return FileResponse(assets / "index.html")

    @app.get("/control/private/records/{kind}/{record_id}", dependencies=[Depends(viewer)])
    def record_view(kind: str, record_id: str):
        if agency_store is None:
            raise HTTPException(503, 'Agency not configured')
        try:
            return agency_store.record_detail(kind, record_id)
        except KeyError:
            raise HTTPException(404, 'Record not found')

    @app.get("/control/listing/{listing_id}")
    def listing_page(listing_id: str):
        if not re.fullmatch(r"[0-9]{6,14}", listing_id):
            raise HTTPException(404, "Listing not found")
        return FileResponse(assets / "listing.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/private/listing/{listing_id}", dependencies=[Depends(viewer)])
    def listing_data(listing_id: str):
        if agency_store is None or not re.fullmatch(r"[0-9]{6,14}", listing_id):
            raise HTTPException(404, "Listing not found")
        from verda.agency.research import listing_file
        return {**listing_file(agency_store, listing_id), "csrf_token": csrf_token}

    attention_cache = {"at": 0.0, "value": None}

    @app.get("/control/attention")
    def attention_page():
        return FileResponse(assets / "attention.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/private/attention", dependencies=[Depends(viewer)])
    def attention_data(fresh: bool = False):
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        if fresh or attention_cache["value"] is None or time.time() - attention_cache["at"] > 30:
            from verda.agency.attention import attention
            attention_cache.update(value=attention(agency_store), at=time.time())
        return {**attention_cache["value"], "csrf_token": csrf_token}

    @app.post("/control/private/attention/ack", dependencies=[Depends(editor)])
    def attention_ack(command: AttentionAck):
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        from verda.agency.attention import acknowledge
        try:
            acknowledge(agency_store, command.key)
        except ValueError as error:
            raise HTTPException(409, str(error))
        attention_cache["value"] = None
        return {"acknowledged": command.key}

    @app.get("/control/settings")
    def settings_page():
        return FileResponse(assets / "settings.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/operators")
    def operators_page():
        return FileResponse(assets / "operators.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/private/settings", dependencies=[Depends(viewer)])
    def settings_data():
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        from verda.agency.tuning import describe
        return {**describe(agency_store), "csrf_token": csrf_token}

    @app.put("/control/private/settings", dependencies=[Depends(editor)])
    def settings_change(command: SettingsRequest):
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        from verda.agency.tuning import apply
        try:
            return apply(agency_store, [c.model_dump() for c in command.changes])
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.post("/control/private/outbox/{outbox_id}/resolve", dependencies=[Depends(editor)])
    def outbox_resolve(outbox_id: str, command: OutboxResolve):
        """The user's word on a message whose delivery could not be proven: it went, or it did not. Nothing is sent from here."""
        if agency_store is None or not re.fullmatch(r"[0-9a-f]{32}", outbox_id):
            raise HTTPException(404, "Message not found")
        from verda.agency import message
        try:
            with agency_store.transaction() as con:
                message.resolve_unknown(con, outbox_id, command.delivered, time.time(), command.note)
        except ValueError:
            raise HTTPException(409, "Yalnız gittiği doğrulanamayan mesaj için karar verilebilir")
        attention_cache["value"] = None
        return {"status": "confirmed" if command.delivered else "cancelled"}

    @app.post("/control/private/triggers/{key}/run", dependencies=[Depends(editor)])
    def trigger_run(key: str):
        if agency_store is None or not re.fullmatch(r"[a-z0-9_-]{1,64}", key):
            raise HTTPException(404, "Trigger not found")
        from verda.agency.tuning import run_now
        try:
            return {"task_id": run_now(agency_store, key)}
        except KeyError:
            raise HTTPException(404, "Zamanlayıcı bulunamadı")
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get("/control/private/operators", dependencies=[Depends(viewer)])
    def operators_data():
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        from verda.agency.operators import operator_status
        return {**operator_status(agency_store), "csrf_token": csrf_token}

    @app.get("/control/ranking")
    def ranking_page():
        return FileResponse(assets / "ranking.html", headers={"Cache-Control": "no-store"})

    @app.get("/control/private/ranking", dependencies=[Depends(viewer)])
    def ranking_data(limit: int = Query(default=50, ge=1, le=200)):
        if agency_store is None:
            raise HTTPException(503, "Agency not configured")
        from verda.agency.score import ranking
        return ranking(agency_store, {"limit": limit}, mode="local")

    @app.post("/control/private/listing/{listing_id}/decide", dependencies=[Depends(editor)])
    def listing_decide(listing_id: str, command: ListingDecision):
        if agency_store is None or not re.fullmatch(r"[0-9]{6,14}", listing_id):
            raise HTTPException(404, "Listing not found")
        from verda.facts.flags import CLARIFIABLE
        if command.field not in CLARIFIABLE:
            raise HTTPException(409, "Bu alan buradan değiştirilemez")
        with agency_store.transaction() as con:
            result = agency_store.facts.decide(con, listing_id, "local", command.field, command.value, command.note, time.time())
        if result["status"] != "accepted":
            raise HTTPException(409, "Değer kabul edilmedi: " + str(result.get("reason")))
        attention_cache["value"] = None
        return {"status": "accepted"}

    @app.post("/control/private/flags/{flag_id}/close", dependencies=[Depends(editor)])
    def flag_close(flag_id: str, command: FlagClose):
        if agency_store is None or not re.fullmatch(r"[0-9a-f]{32}", flag_id):
            raise HTTPException(404, "Flag not found")
        from verda.facts.flags import close_flag
        try:
            with agency_store.transaction() as con:
                close_flag(con, flag_id, command.note)
        except ValueError:
            raise HTTPException(409, "İşaret zaten kapalı")
        attention_cache["value"] = None
        return {"closed": flag_id}

    @app.get("/control/private/assets/{asset_id}", dependencies=[Depends(viewer)])
    def listing_asset(asset_id: str):
        if agency_store is None or not re.fullmatch(r"[0-9a-f]{64}", asset_id):
            raise HTTPException(404, "Asset not found")
        with agency_store.transaction() as con:
            found = agency_store.assets.read(con, asset_id)
        if found is None:
            raise HTTPException(404, "Asset not found")
        mime, path = found
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=86400", "Content-Disposition": "inline"})

    @app.get("/control/private/health", dependencies=[Depends(viewer)])
    def agency_health():
        if agency_store is None:raise HTTPException(503,'Agency not configured')
        from verda.agency.health import inspect_health
        return inspect_health(agency_store)

    @app.get("/control/private/agency", dependencies=[Depends(viewer)])
    def agency_view(listing_ref: str | None = None, trace_id: str | None = None):
        if agency_store is None:
            raise HTTPException(503, "Agent inbox storage is not configured")
        from verda.agency.engine import default_registry, PROTOCOL
        from verda.agency.inspection import tool_description, connection_descriptions
        registry = default_registry(agency_store.config, agency_store)
        from verda.agency.settings import agent_view
        from verda.agency.browser import BrowserBridge
        try:overview=agency_store.overview(listing_ref=listing_ref,trace_id=trace_id)
        except sqlite3.Error:raise HTTPException(503,'Kayıt deposuna erişilemiyor; işlemler duraklatıldı.')
        with agency_store.transaction() as con:
            agents = [agent_view(con, a) for a in agency_store.config.agents]
        browser = BrowserBridge(agency_store).overview()
        connections = connection_descriptions(agency_store.config)
        for connection in connections:
            if connection['key'] == 'sahibinden':
                connection.update(session_status='connected' if browser['active'] else 'waiting_driver', note=browser['note'])
        return {**overview,
                "agents": agents, 'csrf_token': csrf_token, 'browser': browser,
                "tools": [tool_description(registry, t) for t in agency_store.config.tools],
                "connections": connections,
                "live_source_adapters": False, "protocol_prompt": PROTOCOL}

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

    @app.post("/api/viewer-link", dependencies=[Depends(authorize), Depends(local_command)])
    def new_viewer_link(request: Request):
        """A fresh one-time panel link (10 minutes). Only a local client holding the API token can ask; the panel never offers it itself."""
        with viewer_lock:
            nonce = secrets.token_urlsafe(32)
            viewer_state["nonce"] = nonce
            viewer_state["nonce_expires"] = time.time() + 600
        return {"url": str(request.base_url).rstrip("/") + "/control/unlock/" + nonce}

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

    @app.get("/control/private/cases", dependencies=[Depends(viewer)])
    def viewer_cases(search: str = Query(default="", max_length=100), offset: int = Query(default=0, ge=0),
                     session: Session = Depends(database)):
        snapshot = session.scalar(select(Snapshot).order_by(Snapshot.imported_at.desc()).limit(1))
        if snapshot is None:
            return {"snapshot_id": None, "total": 0, "cases": [], "has_more": False}
        query = select(Case).where(Case.snapshot_id == snapshot.id)
        if search:
            query = query.where(Case.title.contains(search, autoescape=True) | Case.listing_id.contains(search, autoescape=True))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.order_by(Case.lifecycle == "excluded", Case.listing_id).offset(offset).limit(25))
        return {"snapshot_id": snapshot.id, "total": total, "has_more": offset + 25 < total,
                "cases": [{"id": row.listing_id, "title": row.title, "lifecycle": row.lifecycle} for row in rows]}

    @app.get("/control/private/cases/{snapshot_id}/{listing_id}", dependencies=[Depends(viewer)])
    def viewer_case(snapshot_id: str, listing_id: str, after_id: int = Query(default=0, ge=0),
                    session: Session = Depends(database), store: WorkflowStore = Depends(workflows)):
        row = require_case(session, snapshot_id, listing_id)
        events = list(session.scalars(select(TimelineEvent).where(
            TimelineEvent.snapshot_id == snapshot_id, TimelineEvent.listing_id == listing_id,
            TimelineEvent.source_event_id > after_id).order_by(TimelineEvent.source_event_id).limit(51)))
        return {"id": row.listing_id, "title": row.title, "source_revision": row.source_revision,
                "runs": store.list_runs(case_ref=f"{snapshot_id}/{listing_id}", mode="shadow"),
                "legacy_events": [{"id": event.source_event_id, "at": event.at, "kind": event.kind,
                                   "task_id": event.source_task_id, "payload": event.payload} for event in events[:50]],
                "has_more": len(events) > 50, "next_after_id": events[min(len(events),50)-1].source_event_id if events else after_id}

    @app.get("/control/private/demos", dependencies=[Depends(viewer)])
    def viewer_demos(store: WorkflowStore = Depends(workflows)):
        return {"runs": store.list_runs(mode="demo")}

    @app.get("/control/private/runs/{run_id}", dependencies=[Depends(viewer)])
    def viewer_run(run_id: str, after_id: int = Query(default=0, ge=0), store: WorkflowStore = Depends(workflows)):
        try:
            return store.view(run_id, after_event_id=after_id, limit=100)
        except KeyError as error:
            raise HTTPException(404, "Workflow not found") from error

    def agency():
        if agency_store is None:
            raise HTTPException(503, "Agent inbox storage is not configured")
        return agency_store

    @app.post("/api/agency/tasks", dependencies=[Depends(authorize), Depends(local_command)])
    def submit_agent_task(command: AgentTaskRequest):
        try:
            return {"task_id": agency().submit(**command.model_dump()), "state": "queued"}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/agency/events", dependencies=[Depends(authorize), Depends(local_command)])
    def publish_agent_event(command: AgentEventRequest):
        try:
            return {"tasks": agency().publish(command.request_key, command.event_type, command.payload)}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/agency/tasks/{task_id}/reply", dependencies=[Depends(authorize), Depends(local_command)])
    def reply_to_agent(task_id: str, reply: dict):
        try:
            agency().resume(task_id, reply)
            return {"task_id": task_id, "state": "queued"}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

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
