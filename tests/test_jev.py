import json
import os

import pytest
from fastapi.testclient import TestClient

from verda.agency.browser import BrowserBridge
from verda.agency.jev import JevReader, JevStopped, load_env, run_worker, verify_receipt, same_target
from verda.agency.store import AgencyStore
from verda.api import create_app
from test_management import setup, search, receipt


def raw_receipt():
    value = receipt()
    return {'url':value['source_url'],'blocker':None,'listings':value['listings'],
            'total_reported':value['total_reported'],'detail':None}


def job():
    return {'tool':'sahibinden.search','arguments':{'query':'Datça'}}


def test_driver_routing_survives_restart_and_does_not_steal_old_jobs(tmp_path):
    store, registry = setup(tmp_path)
    bridge = BrowserBridge(store)
    bridge.select('jev')
    task, engine = search(store,registry)
    bridge.attach('codex-test',now=3)
    assert bridge.claim('codex-test',now=4) is None
    with pytest.raises(ValueError,match='Bekleyen'):
        bridge.select('codex')
    reopened = AgencyStore(store.path,store.config);reopened.initialize()
    bridge = BrowserBridge(reopened)
    assert bridge.overview(now=4)['selected_driver'] == 'jev'
    assert not bridge.overview(now=4)['active']
    bridge.attach('jev-test',driver='jev',now=3)
    owned = bridge.claim('jev-test',now=4)
    bridge.progress(owned,'jev-test','observed',now=5)
    with pytest.raises(ValueError):bridge.progress(owned,'other','observed',now=5)
    bridge.finish(owned['id'],'jev-test',owned['token'],result=receipt(),now=6)
    assert engine.step(now=7)
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='complete'
    observation=store.record_detail('observation',view['records']['observations'][0]['id'])
    assert observation['payload']['browser_driver']=='jev_ultrafast'


def test_v6_upgrade_preserves_browser_jobs(tmp_path):
    store,registry=setup(tmp_path);search(store,registry)
    with store.transaction() as con:
        con.execute('ALTER TABLE browser_jobs DROP COLUMN driver')
        con.execute('ALTER TABLE browser_sessions DROP COLUMN driver')
        con.execute('DROP TABLE browser_config')
        con.execute('UPDATE agency_meta SET version=6')
    store.initialize()
    assert BrowserBridge(store).overview()['jobs'][0]['driver']=='codex'


def test_worker_setup_failure_does_not_claim_or_attach(tmp_path):
    store,registry=setup(tmp_path);BrowserBridge(store).select('jev');search(store,registry)
    class Missing:
        def ready(self):return 'typesafe_key_missing'
    result=run_worker(store,reader=Missing())
    assert result['state']=='waiting_setup'
    status=BrowserBridge(store).overview()
    assert not status['active'] and status['jobs'][0]['state']=='queued'
    assert status['jev']['last_check']['reason']=='typesafe_key_missing'


def test_worker_receipt_reaches_common_records(tmp_path):
    store,registry=setup(tmp_path);bridge=BrowserBridge(store);bridge.select('jev')
    # Real-time task clocks are needed by the standalone worker.
    task=store.submit(agent='sahibinden',objective='Fixture read',inputs={'query':'Datça'},request_key='jev-test',executor_tool='sahibinden.search')
    from verda.agency.engine import AgentEngine
    engine=AgentEngine(store,registry,providers={});assert engine.step()
    class Reader:
        def ready(self):return None
        def read(self,url,job,report):
            assert url.startswith('https://www.sahibinden.com/satilik-arsa/mugla-datca?')
            report('observed');report('DONE');report('verified');return receipt()
    assert run_worker(store,reader=Reader())['completed']==1
    assert engine.step()
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==task)['state']=='complete'
    assert not bridge.overview()['active']
    assert any(e['kind']=='browser_progress' for e in view['events'])


class Browser:
    def __init__(self,url,raw=None):
        self.url=url;self.raw=raw or raw_receipt();self.closed=False;self.acts=[]
    def observe(self,**kwargs):
        return {'url':self.url,'title':'Fixture','text':'One land listing','actions':[
            {'id':'send','kind':'click','label':'Send message'},
            {'id':'wait','kind':'wait','label':'Wait'},
            {'id':'scroll_down','kind':'scroll','label':'Scroll','delta':560}]}
    def evaluate(self,script):return self.raw
    def fresh(self,page):return True
    def act(self,action,page):self.acts.append(action)
    def close(self):self.closed=True


def test_jev_done_independently_verified_and_only_read_actions_offered():
    pytest.importorskip('jev_ultrafast')
    b=Browser(receipt()['source_url']);calls=[];progress=[]
    def choose(page,goal,history):
        calls.append(page)
        assert {a['kind'] for a in page['actions']}=={'scroll','wait'}
        return {'choice':'DONE'}
    result=JevReader(lambda url:b,choose).read(b.url,job(),progress.append)
    assert result['listings'][0]['listing_id']=='9000000001'
    assert calls and progress[-1]=='verified' and b.closed and not b.acts


@pytest.mark.parametrize('raw,choice,code',[
    ({**raw_receipt(),'blocker':'captcha'},'DONE','captcha'),
    ({**raw_receipt(),'total_reported':30,'listings':[]},'DONE',None),
    (raw_receipt(),'send','read_failed'),
])
def test_jev_blocks_challenges_false_done_and_out_of_scope_actions(raw,choice,code):
    pytest.importorskip('jev_ultrafast')
    b=Browser(receipt()['source_url'],raw);calls=[]
    def choose(*args):calls.append(1);return {'choice':choice}
    with pytest.raises((JevStopped,ValueError)) as error:
        JevReader(lambda url:b,choose).read(b.url,job(),lambda phase:None)
    if code:assert str(error.value)==code
    if code=='captcha':assert not calls
    assert b.closed and not b.acts


def test_receipt_rejects_wrong_filters_duplicate_rows_or_wrong_listing():
    raw=raw_receipt()
    for modified in [{**raw,'url':raw['url'].replace('15000000','999999')},
                     {**raw,'listings':raw['listings']*2,'total_reported':2},
                     {**raw,'url':'https://example.com/'}]:
        with pytest.raises(ValueError):verify_receipt(job(),modified)
    with pytest.raises(ValueError):
        verify_receipt({'tool':'sahibinden.read_listing','arguments':{'listing_id':'9999999999'}},
                       {**raw,'url':raw['listings'][0]['url'],'detail':raw['listings'][0]})


def test_target_accepts_filter_reordering_but_not_another_page():
    expected=receipt()['source_url']
    assert same_target('https://www.sahibinden.com/satilik-arsa/mugla-datca?a507_min=1000.0&price_max=15000000',expected)
    assert not same_target(expected+'&pagingOffset=20',expected)
    assert not same_target(expected.replace('15000000','999'),expected)
    assert not same_target(expected.replace('mugla-datca','mugla-bodrum'),expected)


def test_env_file_only_reads_expected_settings_without_shell_execution(tmp_path,monkeypatch):
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    path=tmp_path/'credentials.env'
    path.write_text('TYPESAFE_API_KEY="fixture-private-key"\nBU_CDP_URL=https://evil.example\nIGNORED=$(touch surprise)\n')
    previous=os.environ.get('BU_CDP_URL')
    load_env(path)
    assert os.environ['TYPESAFE_API_KEY']=='fixture-private-key'
    assert os.environ.get('BU_CDP_URL')==previous
    assert not (tmp_path/'surprise').exists()


def test_browser_selection_requires_existing_editor_auth(engine,tmp_path):
    store,_=setup(tmp_path)
    with TestClient(create_app(engine,'long-local-api-token-with-32-characters',viewer_nonce='open',agency_store=store)) as client:
        path='/control/private/browser'
        assert client.put(path,json={'driver':'jev'}).status_code==401
        client.get('/control/unlock/open')
        data=client.get('/control/private/agency').json()
        assert client.put(path,json={'driver':'jev'}).status_code==403
        headers={'Origin':'http://testserver','X-Verda-CSRF':data['csrf_token']}
        assert client.put(path,json={'driver':'jev','command':'evil'},headers=headers).status_code==422
        result=client.put(path,json={'driver':'jev'},headers=headers)
        assert result.status_code==200 and result.json()['selected_driver']=='jev'
