from fastapi.testclient import TestClient

from verda.api import create_app
from verda.legacy import import_snapshot
from verda.workflow import WorkflowStore

TOKEN = "synthetic-workflow-token-of-at-least-thirty-two-characters"
AUTH = {"Authorization": "Bearer " + TOKEN}


def test_workflow_api_retains_source_revision_and_enforces_auth(source, engine, tmp_path):
    snapshot = import_snapshot(source, engine)["snapshot_id"]
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    store.initialize()
    app = create_app(engine, TOKEN, store)
    url = f"/api/workflows/from-case/{snapshot}/9000000001"
    body = {"request_key": "test-run"}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json=body, headers={**AUTH, "Origin": "https://untrusted.example"}).status_code == 403
        response = client.post(url, json=body, headers=AUTH)
        assert response.status_code == 200
        result = response.json()
        assert result["mode"] == "shadow" and result["external_actions_enabled"] is False
        assert client.post(url, json=body, headers=AUTH).json()["run_id"] == result["run_id"]
        run_url = "/api/workflows/" + result["run_id"]
        assert client.get(run_url).status_code == 401
        view = client.get(run_url, headers=AUTH).json()
        assert view["source_revision"] == 3 and len(view["tasks"]) == 9
        assert client.post(run_url + "/cancel", headers=AUTH).json()["cancelled"] is True
        assert client.get(run_url, headers=AUTH).json()["status"] == "cancelled"
        assert client.post(f"/api/workflows/from-case/{snapshot}/9000000002", json={"request_key": "excluded"}, headers=AUTH).status_code == 409
        assert client.get("/api/workflows/unknown", headers=AUTH).status_code == 404
