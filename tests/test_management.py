import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore
from verda.agency.engine import AgentEngine, default_registry
from verda.agency.settings import agent_view, save_agent
from verda.agency.browser import BrowserBridge
from verda.agency.research import audit, listing, request_review
from verda.api import create_app
from verda.legacy import import_snapshot
from test_agency import Provider, decision


def setup(tmp_path):
    config = AgencyConfig.load()
    store = AgencyStore(tmp_path/'agency.sqlite', config)
    store.initialize()
    return store, default_registry(config, store)


def search(store, registry):
    task = store.submit(agent='sahibinden', objective='Search first page', inputs={'query':'Datça'},
                        request_key='search', executor_tool='sahibinden.search', now=1)
    engine = AgentEngine(store, registry, providers={})
    assert engine.step(now=2)
    assert store.overview()['tasks'][0]['state'] == 'waiting_browser'
    return task, engine


def receipt():
    return {'source_url':'https://www.sahibinden.com/satilik-arsa/mugla-datca?price_max=15000000&a507_min=1000',
            'total_reported':1, 'page':1, 'has_more':False,
            'filters':{'max_price_tl':15000000,'min_area_m2':1000},
            'listings':[{'listing_id':'9000000001','title':'Synthetic test listing',
                         'url':'https://www.sahibinden.com/ilan/test-9000000001/detay',
                         'price_tl':8000000.0,'area_m2':2000.0}]}


def test_prompt_edits_persist_and_old_tasks_execute_their_saved_prompt(tmp_path):
    store, registry = setup(tmp_path)
    original = store.agents['manager'].prompt
    first = store.submit(agent='manager',objective='Complete fixture',inputs={},request_key='old',now=1)
    with store.transaction() as con:
        before = agent_view(con,store.agents['manager'])
    saved = save_agent(store,'manager',prompt='New fixture prompt',description='Updated role',expected_revision=before['edit_revision'],now=2)
    with pytest.raises(ValueError,match='başka bir'):
        save_agent(store,'manager',prompt='Stale',description='Stale',expected_revision=before['edit_revision'])
    restored = AgencyStore(store.path,AgencyConfig.load());restored.initialize()
    second = restored.submit(agent='manager',objective='Complete fixture',inputs={},request_key='new',now=3)
    provider=Provider([decision('complete'),decision('complete')])
    engine=AgentEngine(restored,default_registry(restored.config,restored),{'codex':provider})
    assert engine.step(now=4) and engine.step(now=5)
    assert provider.calls[0]['instructions'].startswith(original)
    assert provider.calls[1]['instructions'].startswith('New fixture prompt')
    assert {t['id'] for t in store.overview()['tasks']} == {first,second}
    assert saved['version'] != before['version']


def test_edit_and_review_require_viewer_same_origin_csrf_and_closed_fields(engine,tmp_path):
    store,registry=setup(tmp_path)
    with TestClient(create_app(engine,'token-of-at-least-thirty-two-characters',viewer_nonce='open',agency_store=store)) as client:
        url='/control/private/agents/manager'
        assert client.put(url,json={}).status_code == 401
        client.get('/control/unlock/open')
        data=client.get('/control/private/agency').json()
        agent=next(a for a in data['agents'] if a['key']=='manager')
        body={'prompt':'Updated local prompt','description':'Updated local role','expected_revision':agent['edit_revision']}
        assert client.put(url,json=body).status_code == 403
        headers={'Origin':'http://evil.example','X-Verda-CSRF':data['csrf_token']}
        assert client.put(url,json=body,headers=headers).status_code == 403
        headers['Origin']='http://testserver'
        assert client.put(url,json={**body,'tools':['evil']},headers=headers).status_code == 422
        assert client.put(url,json=body,headers=headers).status_code == 200
        assert client.put(url,json=body,headers=headers).status_code == 409
        one=client.post('/control/private/research/review',json={'request_key':'one','listing_id':'9000000001'},headers=headers)
        two=client.post('/control/private/research/review',json={'request_key':'two','listing_id':'9000000001'},headers=headers)
        assert one.status_code==two.status_code==200
        assert one.json()==two.json()


def test_browser_queue_round_trip_is_atomic_idempotent_and_delivers_to_manager(tmp_path):
    store,registry=setup(tmp_path);task,engine=search(store,registry)
    bridge=BrowserBridge(store);bridge.attach('test',now=3)
    job=bridge.claim('test',now=4)
    assert job['task_id']==task and job['arguments']=={'query':'Datça'}
    assert bridge.claim('test',now=5) is None
    with pytest.raises(ValueError,match='not owned'):
        bridge.finish(job['id'],'other',job['token'],result=receipt(),now=6)
    bridge.finish(job['id'],'test',job['token'],result=receipt(),now=6)
    bridge.finish(job['id'],'test',job['token'],result=receipt(),now=7)
    assert engine.step(now=8)
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='complete'
    assert len(view['records']['observations'])==1
    assert view['records']['deliveries']
    provider=Provider([decision('complete')]);engine.providers={'codex':provider}
    assert engine.step(now=9)
    assert provider.calls[0]['context']['received_records'][0]['observations'][0]['payload']['listings'][0]['listing_id']=='9000000001'
    assert audit(store)['listings'][0]['missing']==['parcel_key','natural_sit','archaeological_sit','route_minutes','access']
    assert audit(store,mode='synthetic')['listings']==[]
    assert '9000000001' in listing(store,'9000000001')['text']


@pytest.mark.parametrize('change',[{'source_url':'https://evil.example/'},{'filters':{'max_price_tl':999,'min_area_m2':1}}, {'listings':[{'listing_id':'9000000001','title':'Bad','url':'javascript:alert(1)'}]}])
def test_browser_rejects_wrong_site_or_filters_without_committing_a_receipt(tmp_path,change):
    store,registry=setup(tmp_path);search(store,registry)
    bridge=BrowserBridge(store);bridge.attach('test',now=3);job=bridge.claim('test',now=4)
    with pytest.raises(ValueError):bridge.finish(job['id'],'test',job['token'],result={**receipt(),**change},now=5)
    assert store.overview()['records']['observations']==[]
    assert bridge.overview(now=5)['jobs'][0]['state']=='running'


def test_browser_captcha_stops_shared_source_and_notifies_manager(tmp_path):
    store,registry=setup(tmp_path);task,_=search(store,registry)
    bridge=BrowserBridge(store);bridge.attach('test',now=3);job=bridge.claim('test',now=4)
    bridge.finish(job['id'],'test',job['token'],error='captcha',now=5)
    view=store.overview()
    assert view['resources'][0]['halted']=='captcha'
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='blocked'
    assert len(view['incidents'])==1
    assert any(e['type']=='source.blocked' for e in view['records']['events'])


def test_browser_disconnect_after_claim_is_never_silently_replayed(tmp_path):
    store,registry=setup(tmp_path);task,_=search(store,registry)
    bridge=BrowserBridge(store);bridge.attach('test',now=3);job=bridge.claim('test',now=4)
    bridge.expire(now=605)
    assert bridge.overview(now=605)['jobs'][0]['state']=='uncertain'
    assert next(t for t in store.overview()['tasks'] if t['id']==task)['state']=='uncertain'
    with pytest.raises(ValueError):bridge.finish(job['id'],'test',job['token'],result=receipt(),now=606)
    bridge.attach('test',now=607)
    assert bridge.claim('test',now=608) is None


def test_archive_evidence_not_treated_as_empty_or_copied_to_synthetic_mode(engine,source,tmp_path):
    import_snapshot(source,engine)
    store,registry=setup(tmp_path);store.archive_path=Path(engine.url.database)
    report=audit(store)
    assert report['total']==1
    assert report['listings'][0]['archive_review_required'] is True
    assert report['listings'][0]['missing']==[]
    history=listing(store,'9000000001')
    assert 'verified_by_statement' in history['text'] and 'delivery_unknown' in history['text']
    assert audit(store,mode='synthetic')['total']==0
    assert listing(store,'9000000001',mode='synthetic')['text']=='[]'
    with pytest.raises(ValueError,match='elenmiş'):
        request_review(store,'9000000002','excluded')


def test_review_followup_budget_is_enforced_by_store(tmp_path):
    store,_=setup(tmp_path)
    key=request_review(store,'9000000001','one')
    task=store.claim(mode='local')
    assert task['id']==key
    store.decision(task,decision('dispatch','sahibinden').model_dump(),{})
    child=store.delegate(task,'sahibinden','Read',{'listing_id':'9000000001'},'9000000001',wait=False)
    # Parent wins tie by creation time and can decide again, but cannot exceed its scope.
    parent=store.claim(mode='local')
    assert parent['id']==key
    store.decision(parent,decision('dispatch','sahibinden').model_dump(),{})
    with pytest.raises(ValueError,match='budget'):
        store.delegate(parent,'sahibinden','Read another',{'listing_id':'9000000002'},'9000000002')
