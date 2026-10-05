from fastapi.testclient import TestClient

from verda.api import create_app
from verda.legacy import import_snapshot
from verda.workflow import WorkflowStore, research_plan
from verda.worker import step

TOKEN = "test-flow-token-of-at-least-thirty-two-characters"


def test_viewer_requires_one_time_link_and_cannot_write(source, engine, tmp_path):
    snapshot = import_snapshot(source, engine)["snapshot_id"]
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    store.initialize()
    with TestClient(create_app(engine, TOKEN, store, viewer_nonce="one-use")) as client:
        assert client.get("/control/flows").status_code == 200
        assert client.get("/control/private/cases").status_code == 401
        assert client.get("/control/unlock/wrong").status_code == 403
        unlocked = client.get("/control/unlock/one-use", follow_redirects=False)
        assert unlocked.status_code == 303
        assert "HttpOnly" in unlocked.headers["set-cookie"]
        assert "SameSite=strict" in unlocked.headers["set-cookie"]
        assert client.get("/control/unlock/one-use").status_code == 403
        rows = client.get("/control/private/cases").json()
        assert rows["total"] == 2
        assert client.get("/control/private/cases?search=9000000001").json()["total"] == 1
        response = client.get(f"/control/private/cases/{snapshot}/9000000001")
        data = response.json()
        assert len(data["legacy_events"]) == 3 and not data["runs"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert TOKEN not in response.text
        assert client.post(f"/api/workflows/from-case/{snapshot}/9000000001", json={"request_key":"forbidden"}).status_code == 401
        assert client.get("/api/snapshots").status_code == 401
    with TestClient(create_app(engine, TOKEN, store)) as outsider:
        assert outsider.get("/control/private/demos").status_code == 401


def test_handoff_records_actual_claim_input_not_current_projection(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    store.initialize()
    run = store.create_run(research_plan("demo", 1, "demo"), "trace", now=0)
    step(store, "worker", now=1)
    claim = store.claim("next-worker", "demo", now=2)
    view = store.view(run)
    task = next(t for t in view["tasks"] if t["id"] == claim.task_id)
    assert task["last_claim"]["input_snapshot"] == claim.inputs
    assert task["last_claim"]["worker_id"] == "next-worker"
    assert task["last_claim"]["input_snapshot"]["listing"]["demo"] is True
    assert view["tasks"][2]["last_claim"] is None


def test_demo_runs_are_not_attached_to_real_case(tmp_path, source, engine):
    snapshot = import_snapshot(source, engine)["snapshot_id"]
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    store.initialize()
    case_ref = f"{snapshot}/9000000001"
    demo = store.create_run(research_plan(case_ref, 3, "demo"), "demo", now=1)
    shadow = store.create_run(research_plan(case_ref, 3, "shadow"), "shadow", now=2)
    with TestClient(create_app(engine, TOKEN, store, viewer_nonce="open")) as client:
        client.get("/control/unlock/open")
        data = client.get(f"/control/private/cases/{case_ref}").json()
        assert [r["id"] for r in data["runs"]] == [shadow]
        assert [r["id"] for r in client.get("/control/private/demos").json()["runs"]] == [demo]
