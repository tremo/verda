from fastapi.testclient import TestClient

from verda.api import create_app
from verda.control import MANAGER_PROMPT
from verda.runtime import AgentModel, Runtime, RuntimeConfig


def test_control_catalog_exposes_definitions_not_private_data(engine):
    token = "private-api-token-must-not-appear-in-control-page"
    with TestClient(create_app(engine, token)) as client:
        page = client.get("/control")
        assert page.status_code == 200
        assert client.get("/control/assets/app.js").status_code == 200
        response = client.get("/control/catalog")
        data = response.json()
        assert len(data["agents"]) == 7
        assert data["external_actions_enabled"] is False
        assert data["browser"]["status"] == "not_connected"
        assert data["pacing"]["status"] == "proposed_not_enforced"
        assert token not in response.text and token not in page.text
        assert "case_ref" not in response.text
        assert data["agents"][0]["prompt"] == MANAGER_PROMPT
        assert all(a["prompt_status"] == "draft" for a in data["agents"][1:])
        assert client.get("/api/snapshots").status_code == 401
        assert client.post("/control/catalog", json={}).status_code == 405
        assert client.get("/control", headers={"Host": "untrusted.example"}).status_code == 400


def test_control_shows_selected_agent_model(engine):
    runtime = Runtime(RuntimeConfig(agents={"sahibinden": AgentModel(model="user-choice")}))
    with TestClient(create_app(engine, "test-token-at-least-thirty-two-characters", runtime=runtime)) as client:
        rows = client.get("/control/catalog").json()["agents"]
        assert next(a for a in rows if a["key"] == "sahibinden")["model"]["model"] == "user-choice"
