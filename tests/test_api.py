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


def test_a_fresh_panel_link_needs_the_api_token_and_works_once(engine):
    from fastapi.testclient import TestClient
    from verda.api import create_app
    with TestClient(create_app(engine, TOKEN, viewer_nonce='first')) as client:
        assert client.post('/api/viewer-link').status_code == 401                              # no token: no link
        assert client.post('/api/viewer-link', headers={**AUTH, 'Origin': 'http://evil.example'}).status_code == 403   # not from a web page
        url = client.post('/api/viewer-link', headers=AUTH).json()['url']
        nonce = url.rsplit('/', 1)[1]
        assert client.get('/control/unlock/first', follow_redirects=False).status_code == 403   # the old link was replaced
        assert client.get('/control/unlock/' + nonce, follow_redirects=False).status_code == 303
        assert client.get('/control/unlock/' + nonce, follow_redirects=False).status_code == 403   # once only


def test_the_panel_link_uses_the_name_it_was_asked_for_and_other_names_are_refused(engine):
    from fastapi.testclient import TestClient
    from verda.api import create_app
    with TestClient(create_app(engine, TOKEN, viewer_nonce='x'), base_url='http://verda.localhost:18771') as client:
        url = client.post('/api/viewer-link', headers=AUTH).json()['url']
        assert url.startswith('http://verda.localhost:18771/control/unlock/')
    with TestClient(create_app(engine, TOKEN, viewer_nonce='x'), base_url='http://evil.example') as client:
        assert client.post('/api/viewer-link', headers=AUTH).status_code == 400            # an unknown host name is refused outright


def test_the_plain_address_leads_to_the_panel_when_signed_in_and_explains_how_otherwise(engine):
    from fastapi.testclient import TestClient
    from verda.api import create_app
    with TestClient(create_app(engine, TOKEN, viewer_nonce='open')) as client:
        page = client.get('/', follow_redirects=False)
        assert page.status_code == 200 and 'Verda-Paneli.command' in page.text            # not a bare "Not Found"
        client.get('/control/unlock/open', follow_redirects=False)                          # sign in
        assert client.get('/', follow_redirects=False).headers['location'] == '/control'


def test_this_mac_needs_no_link_when_trusted_but_changes_still_need_the_same_page_origin_and_token(engine):
    from fastapi.testclient import TestClient
    from verda.api import create_app
    app = create_app(engine, TOKEN, viewer_nonce='x', trust_loopback=True)
    with TestClient(app, client=('127.0.0.1', 5000)) as local:
        assert local.get('/', follow_redirects=False).headers['location'] == '/control'
        assert local.get('/control/private/agents/none', follow_redirects=False).status_code != 401     # reading needs no link
        assert local.put('/control/private/browser', json={'driver': 'jev'}).status_code == 403          # a change still needs origin + token
        assert local.put('/control/private/browser', json={'driver': 'jev'}, headers={'Origin': 'http://evil.example', 'X-Verda-CSRF': 'x'}).status_code == 403
    with TestClient(app, client=('203.0.113.9', 5000)) as other:                                         # not from this machine: the link is required
        assert other.get('/control/private/research').status_code == 401
    strict = create_app(engine, TOKEN, viewer_nonce='x')
    with TestClient(strict, client=('127.0.0.1', 5000)) as local:
        assert local.get('/control/private/research').status_code == 401                                 # the old behaviour is still available
