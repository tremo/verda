from fastapi.testclient import TestClient

from verda.api import create_app
from verda.legacy import import_snapshot

TOKEN = "test-only-token-that-is-longer-than-thirty-two-characters"
AUTH = {"Authorization": "Bearer " + TOKEN}


def test_api_rejects_unauthenticated_data_access(engine):
    with TestClient(create_app(engine, TOKEN)) as client:
        assert client.get("/health").json()["external_actions_enabled"] is False
        assert client.get("/api/snapshots").status_code == 401
        assert client.get("/api/snapshots", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/policy/preview", json={}).status_code == 401
        assert client.get("/openapi.json").status_code == 404


def test_case_filter_and_timeline_cursor(source, engine):
    snapshot = import_snapshot(source, engine)["snapshot_id"]
    with TestClient(create_app(engine, TOKEN), headers=AUTH) as client:
        root = f"/api/snapshots/{snapshot}"
        data = client.get(root + "/cases?lifecycle=verifying").json()
        assert data["total"] == 1
        assert data["cases"][0]["next_action"] == "migration_review"
        assert client.get(root + "/cases?limit=99999").status_code == 422
        url = root + "/cases/9000000001/events"
        page1 = client.get(url + "?limit=2").json()
        assert [row["id"] for row in page1["events"]] == [1, 2]
        assert page1["has_more"] is True
        page2 = client.get(url + f'?after_id={page1["next_after_id"]}&limit=2').json()
        assert [row["id"] for row in page2["events"]] == [3]
        assert page2["has_more"] is False
        details = client.get(root + "/cases/9000000001").json()
        assert details["history"]["listing_feedback"][0]["note"] == "Sentetik kullanıcı notu"
        assert details["historical_evidence_not_reverified"] is True
        assert client.get(root + "/cases/missing/events").status_code == 404


def test_catalog_does_not_claim_live_agents_or_grant_send_tools(engine):
    with TestClient(create_app(engine, TOKEN), headers=AUTH) as client:
        agents = client.get("/api/agents").json()["agents"]
        assert len(agents) == 7
        assert all(agent["implementation"] == "contract_only" for agent in agents)
        assert not any("send_message" in agent["actions"] for agent in agents)
        assert client.post("/api/messages/send", json={}).status_code == 404
