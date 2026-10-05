"""Local durable workflow kernel. No browser, model, network or sending tools.

One SQLite file coordinates local workers using short BEGIN IMMEDIATE
transactions. External work happens outside the transaction under a lease.
The legacy archive remains separate and immutable.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from verda.agents import AGENTS
from verda.legacy import canonical, digest

CAPABILITIES = {agent.key: agent for agent in AGENTS}
SPATIAL_ACTIONS = {"natural_sit", "archaeological_sit", "research_access", "route", "elevation"}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    agent: str
    action: str
    depends_on: tuple[str, ...] = ()
    max_attempts: int = Field(default=3, ge=1, le=5)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_ref: str = Field(min_length=1, max_length=256)
    source_revision: int = Field(ge=0)
    mode: Literal["demo", "shadow"]
    steps: tuple[Step, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_graph(self):
        previous, ancestors = {}, {}
        for step in self.steps:
            if step.key in previous:
                raise ValueError("Duplicate step key")
            spec = CAPABILITIES.get(step.agent)
            if not spec or step.agent == "manager" or step.action not in spec.actions:
                raise ValueError("Action is outside this specialist's capabilities")
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("Duplicate dependency")
            if any(key not in previous for key in step.depends_on):
                raise ValueError("Dependencies must refer to earlier steps (no cycles)")
            chain = set(step.depends_on)
            for dependency in step.depends_on:
                chain.update(ancestors[dependency])
            if step.action in SPATIAL_ACTIONS and not any(
                previous[key].action == "read_official_parcel" for key in chain
            ):
                raise ValueError("Spatial work requires an official parcel dependency")
            previous[step.key], ancestors[step.key] = step, chain
        return self


def research_plan(case_ref: str, source_revision: int, mode: Literal["demo", "shadow"]) -> Plan:
    """The standard manager plan. Strategic/model-driven planning follows later."""
    return Plan(case_ref=case_ref, source_revision=source_revision, mode=mode, steps=(
        Step(key="listing", agent="sahibinden", action="read_listing"),
        Step(key="identity", agent="parcel", action="resolve_parcel", depends_on=("listing",)),
        Step(key="parcel", agent="parcel", action="read_official_parcel", depends_on=("identity",)),
        Step(key="natural_sit", agent="protection", action="natural_sit", depends_on=("parcel",)),
        Step(key="archaeological_sit", agent="protection", action="archaeological_sit", depends_on=("parcel",)),
        Step(key="access", agent="access", action="research_access", depends_on=("parcel",)),
        Step(key="route", agent="geography", action="route", depends_on=("parcel",)),
        Step(key="elevation", agent="geography", action="elevation", depends_on=("parcel",)),
        Step(key="assessment", agent="assessment", action="assess",
             depends_on=("natural_sit", "archaeological_sit", "access", "route", "elevation")),
    ))


class LeaseLost(RuntimeError):
    """A cancelled, expired or reassigned job cannot accept this worker's result."""


@dataclass(frozen=True)
class Claim:
    task_id: str
    run_id: str
    step_key: str
    agent: str
    action: str
    mode: str
    lease_token: str
    attempt: int
    inputs: dict


SCHEMA = (
    "CREATE TABLE workflow_meta (version INTEGER PRIMARY KEY CHECK (version=1))",
    "INSERT INTO workflow_meta VALUES (1)",
    """CREATE TABLE workflow_runs (
        id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, plan_hash TEXT NOT NULL,
        case_ref TEXT NOT NULL, source_revision INTEGER NOT NULL, mode TEXT NOT NULL,
        created_at REAL NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE workflow_tasks (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(id),
        ordinal INTEGER NOT NULL, step_key TEXT NOT NULL, agent TEXT NOT NULL,
        action TEXT NOT NULL, resource TEXT, dependencies TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
        available_at REAL NOT NULL, lease_token TEXT, lease_until REAL, worker_id TEXT,
        reason TEXT, result TEXT, UNIQUE(run_id, step_key))""",
    "CREATE INDEX task_queue ON workflow_tasks (state, available_at)",
    """CREATE TABLE workflow_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES workflow_runs(id),
        task_id TEXT, at REAL NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL)""",
    "CREATE INDEX workflow_timeline ON workflow_events (run_id, id)",
)


class WorkflowStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    @contextmanager
    def _transaction(self):
        # Runtime operations must never silently create a missing database.
        con = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self._transaction() as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if "workflow_meta" not in tables or [row[0] for row in con.execute(
                    "SELECT version FROM workflow_meta"
                )] != [1]:
                    raise ValueError("Refusing to initialize a foreign workflow database")
                return
            for statement in SCHEMA:
                con.execute(statement)

    @staticmethod
    def _event(con, run_id, task_id, now, kind, details):
        con.execute("INSERT INTO workflow_events(run_id,task_id,at,kind,details) VALUES(?,?,?,?,?)",
                    (run_id, task_id, now, kind, canonical(details)))

    def create_run(self, plan: Plan, request_key: str, *, now: float | None = None) -> str:
        if not request_key.strip() or len(request_key) > 128:
            raise ValueError("A request key of 1–128 characters is required")
        now = time.time() if now is None else now
        plan_hash = digest(plan.model_dump(mode="json"))
        with self._transaction() as con:
            previous = con.execute("SELECT id,plan_hash FROM workflow_runs WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["plan_hash"] != plan_hash:
                    raise ValueError("Request key already belongs to a different plan")
                return previous["id"]
            run_id = uuid4().hex
            con.execute("INSERT INTO workflow_runs(id,request_key,plan_hash,case_ref,source_revision,mode,created_at) VALUES(?,?,?,?,?,?,?)",
                        (run_id, request_key, plan_hash, plan.case_ref, plan.source_revision, plan.mode, now))
            for ordinal, step in enumerate(plan.steps):
                con.execute("""INSERT INTO workflow_tasks
                    (id,run_id,ordinal,step_key,agent,action,resource,dependencies,state,max_attempts,available_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid4().hex, run_id, ordinal, step.key, step.agent, step.action,
                     CAPABILITIES[step.agent].resource, canonical(step.depends_on), "queued", step.max_attempts, now))
            self._event(con, run_id, None, now, "run_created", {"mode": plan.mode, "steps": len(plan.steps)})
            return run_id

    def _expire(self, con, now):
        for task in con.execute("SELECT * FROM workflow_tasks WHERE state='running' AND lease_until<=?", (now,)).fetchall():
            state = "retry_wait" if task["attempts"] < task["max_attempts"] else "failed"
            delay = min(300, 2 ** task["attempts"])
            con.execute("""UPDATE workflow_tasks SET state=?,reason='lease_expired',available_at=?,
                lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE id=?""", (state, now + delay, task["id"]))
            self._event(con, task["run_id"], task["id"], now, "lease_expired", {"next_state": state})

    def claim(self, worker_id: str, mode: Literal["demo", "shadow"], *, lease_seconds: int = 60,
              now: float | None = None) -> Claim | None:
        if not worker_id.strip() or len(worker_id) > 128 or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid worker or lease duration")
        if mode not in {"demo", "shadow"}:
            raise ValueError("Unsupported execution mode")
        now = time.time() if now is None else now
        with self._transaction() as con:
            self._expire(con, now)
            tasks = con.execute("""SELECT t.* FROM workflow_tasks t JOIN workflow_runs r ON t.run_id=r.id
                WHERE r.cancelled=0 AND r.mode=? AND t.state IN ('queued','retry_wait') AND t.available_at<=?
                ORDER BY r.created_at,r.id,t.ordinal""", (mode, now)).fetchall()
            for task in tasks:
                siblings = {r["step_key"]: r for r in con.execute("SELECT * FROM workflow_tasks WHERE run_id=?", (task["run_id"],))}
                dependencies = [siblings[key] for key in json.loads(task["dependencies"])]
                if any(row["state"] != "succeeded" for row in dependencies):
                    continue
                if task["resource"] and con.execute("""SELECT 1 FROM workflow_tasks
                    WHERE resource=? AND state='running' LIMIT 1""", (task["resource"],)).fetchone():
                    continue
                token = uuid4().hex
                con.execute("""UPDATE workflow_tasks SET state='running',attempts=attempts+1,
                    lease_token=?,lease_until=?,worker_id=?,reason=NULL WHERE id=?""",
                    (token, now + lease_seconds, worker_id, task["id"]))
                self._event(con, task["run_id"], task["id"], now, "task_claimed",
                            {"step": task["step_key"], "attempt": task["attempts"] + 1,
                             "worker_id": worker_id,
                             "input_snapshot": {row["step_key"]: json.loads(row["result"]) for row in dependencies}})
                return Claim(task["id"], task["run_id"], task["step_key"], task["agent"], task["action"], mode,
                             token, task["attempts"] + 1,
                             {row["step_key"]: json.loads(row["result"]) for row in dependencies})
            return None

    @staticmethod
    def _owned(con, claim, now):
        task = con.execute("SELECT * FROM workflow_tasks WHERE id=?", (claim.task_id,)).fetchone()
        if not task or task["state"] != "running" or task["lease_token"] != claim.lease_token or task["lease_until"] <= now:
            raise LeaseLost("Task lease expired, was cancelled or belongs to another worker")
        return task

    def heartbeat(self, claim: Claim, *, lease_seconds: int = 60, now: float | None = None):
        if not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid lease duration")
        now = time.time() if now is None else now
        with self._transaction() as con:
            self._owned(con, claim, now)
            con.execute("UPDATE workflow_tasks SET lease_until=? WHERE id=?", (now + lease_seconds, claim.task_id))

    def complete_demo(self, claim: Claim, data: dict, *, now: float | None = None):
        """Fixture completion only. Live evidence acceptance is not implemented."""
        now = time.time() if now is None else now
        payload = canonical({"demo": True, "data": data})
        if len(payload.encode()) > 65536:
            raise ValueError("Task output too large")
        with self._transaction() as con:
            task = self._owned(con, claim, now)
            run = con.execute("SELECT mode FROM workflow_runs WHERE id=?", (task["run_id"],)).fetchone()
            if run["mode"] != "demo":
                raise ValueError("Demo output cannot complete a shadow/live task")
            if task["action"] == "read_official_parcel" and (
                data.get("identity_matched") is not True or data.get("geometry_validated") is not True
            ):
                raise ValueError("Parcel identity and geometry must pass before spatial work")
            con.execute("""UPDATE workflow_tasks SET state='succeeded',result=?,lease_token=NULL,
                lease_until=NULL,worker_id=NULL WHERE id=?""", (payload, task["id"]))
            self._event(con, task["run_id"], task["id"], now, "task_succeeded", {"step": task["step_key"], "demo": True})

    def fail(self, claim: Claim, reason: str, *, retryable: bool = False, blocked: bool = False,
             now: float | None = None):
        if not reason.strip() or len(reason) > 500 or (retryable and blocked):
            raise ValueError("Invalid failure reason or state")
        now = time.time() if now is None else now
        with self._transaction() as con:
            task = self._owned(con, claim, now)
            state = "blocked" if blocked else "retry_wait" if retryable and task["attempts"] < task["max_attempts"] else "failed"
            con.execute("""UPDATE workflow_tasks SET state=?,reason=?,available_at=?,lease_token=NULL,
                lease_until=NULL,worker_id=NULL WHERE id=?""",
                (state, reason, now + min(300, 2 ** task["attempts"]), task["id"]))
            self._event(con, task["run_id"], task["id"], now, "task_" + state,
                        {"step": task["step_key"], "reason": reason})

    def cancel(self, run_id: str, *, now: float | None = None):
        now = time.time() if now is None else now
        with self._transaction() as con:
            row = con.execute("SELECT cancelled FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["cancelled"]:
                return True
            active = con.execute("""SELECT 1 FROM workflow_tasks WHERE run_id=?
                AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1""", (run_id,)).fetchone()
            if active is None:
                return False
            con.execute("UPDATE workflow_runs SET cancelled=1 WHERE id=?", (run_id,))
            con.execute("""UPDATE workflow_tasks SET state='cancelled',reason='run_cancelled',
                lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled')""", (run_id,))
            self._event(con, run_id, None, now, "run_cancelled", {})
            return True

    def view(self, run_id: str, *, after_event_id: int = 0, limit: int = 100) -> dict:
        if after_event_id < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid event cursor or page size")
        # Readers do not reserve the writer lock.
        con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            run = con.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            tasks = [dict(row) for row in con.execute("SELECT * FROM workflow_tasks WHERE run_id=? ORDER BY ordinal", (run_id,))]
            claims = {}
            for row in con.execute("SELECT task_id,at,details FROM workflow_events WHERE run_id=? AND kind='task_claimed' ORDER BY id", (run_id,)):
                claims[row["task_id"]] = {"at": row["at"], **json.loads(row["details"])}
            by_key = {t["step_key"]: t for t in tasks}
            for task in tasks:
                task["dependencies"] = json.loads(task["dependencies"])
                task["waiting_on"] = [key for key in task["dependencies"] if by_key[key]["state"] != "succeeded"]
                task["result"] = json.loads(task["result"]) if task["result"] else None
                task["last_claim"] = claims.get(task["id"])
                del task["lease_token"]
            states = {task["state"] for task in tasks}
            status = ("cancelled" if run["cancelled"] else "failed" if "failed" in states else
                      "complete" if states == {"succeeded"} else "running" if "running" in states else
                      "blocked" if "blocked" in states else "pending")
            rows = con.execute("SELECT * FROM workflow_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                               (run_id, after_event_id, limit + 1)).fetchall()
            events = [{**dict(row), "details": json.loads(row["details"])} for row in rows[:limit]]
            return {"id": run_id, "case_ref": run["case_ref"], "source_revision": run["source_revision"],
                    "mode": run["mode"], "status": status, "tasks": tasks, "events": events,
                    "has_more_events": len(rows) > limit,
                    "next_after_event_id": events[-1]["id"] if events else after_event_id}
        finally:
            con.close()

    def list_runs(self, *, case_ref: str | None = None, mode: str | None = None, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 100 or mode not in {None, "demo", "shadow"}:
            raise ValueError("Invalid run query")
        con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            where, values = [], []
            if case_ref is not None:
                where.append("case_ref=?")
                values.append(case_ref)
            if mode is not None:
                where.append("mode=?")
                values.append(mode)
            sql = "SELECT id,case_ref,source_revision,mode,created_at FROM workflow_runs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            return [dict(row) for row in con.execute(sql + " ORDER BY created_at DESC,id DESC LIMIT ?", (*values, limit))]
        finally:
            con.close()
