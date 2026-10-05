"""Evidence persistence, durable result delivery and scoped reads use synthetic data."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from test_agency import config, decision, Provider
from verda.agency.store import AgencyStore
from verda.agency.registry import Registry
from verda.agency.engine import AgentEngine
from verda.api import create_app


def setup(tmp_path, payload=None, supervisor='manager'):
    store=AgencyStore(tmp_path/'records.sqlite',config(),supervisor_agent=supervisor)
    store.initialize()
    registry=Registry(store.config,{'test.read':lambda args: payload or {'price_tl':8000000,'area_m2':2000,'parcel_key':'DEMO-1/1'}})
    return store,registry


def perform(store,registry,mode='local',key='read',now=0):
    task=store.submit(agent='operator',objective='Read synthetic fixture',inputs={'id':'one'},listing_ref='DEMO',
                      request_key=key,source='timer:morning',mode=mode,executor_tool='test.read',now=now)
    engine=AgentEngine(store,registry,providers={})
    engine.step(mode=mode,now=now+1);engine.step(mode=mode,now=now+2)
    return task


def test_direct_timer_result_persists_and_reaches_manager_after_restart(tmp_path):
    store,registry=setup(tmp_path)
    origin=perform(store,registry)
    records=store.overview()['records']
    assert records['pending_count']==1 and records['observation_count']==1
    restored=AgencyStore(store.path,store.config);restored.initialize()
    targets=restored.dispatch_record_events(mode='local',now=3)
    assert len(targets)==1 and restored.dispatch_record_events(mode='local',now=4)==[]
    tasks={t['id']:t for t in restored.overview()['tasks']}
    review=tasks[targets[0]]
    assert review['trace_id']==tasks[origin]['trace_id']
    assert review['caused_by_task_id']==origin and review['parent_id'] is None
    assert review['agent']=='manager' and review['mode']=='local' and review['listing_ref']=='DEMO'
    context=restored.context({**review,'inputs':json.dumps(review['inputs'])})['received_records']
    assert context[0]['observations'][0]['fields']['area_m2']=={'value':2000,'unit':'m²'}
    assert context[0]['observations'][0]['verification']=='unverified'
    provider=Provider([decision('complete')])
    assert AgentEngine(restored,registry,{'codex':provider}).step(now=5)
    assert restored.dispatch_record_events(mode='local',now=6)==[]
    assert len(restored.overview()['tasks'])==2
    assert {r['kind'] for r in restored.overview()['records']['outcomes']}=={'task_outcome','manager_decision'}
    outcomes=restored.overview()['records']['outcomes']
    assert outcomes[0]['observation_ids']==outcomes[1]['observation_ids']
    assert outcomes[0]['observation_ids']


def test_delegate_result_uses_existing_parent_and_includes_evidence(tmp_path):
    store,registry=setup(tmp_path)
    root=store.submit(agent='manager',objective='Delegate read',inputs={},request_key='parent',now=0)
    provider=Provider([decision('delegate','operator','Read',{'id':'one'}),decision('tool','test.read',args={'id':'one'}),decision('complete'),decision('complete')])
    engine=AgentEngine(store,registry,{'codex':provider})
    for n in range(4):engine.step(now=n+1)
    data=store.overview()
    assert len(data['tasks'])==2 and all(t['state']=='complete' for t in data['tasks'])
    delivery=data['records']['deliveries'][0]
    assert delivery['route']=='parent' and delivery['target_task_id']==root and delivery['state']=='delivered'
    assert provider.calls[-1]['context']['received_records'][0]['observations'][0]['payload']['price_tl']==8000000


def test_concurrent_outbox_dispatch_creates_only_one_review(tmp_path):
    store,registry=setup(tmp_path);perform(store,registry)
    with ThreadPoolExecutor(4) as pool:
        targets=list(pool.map(lambda _:store.dispatch_record_events(mode='local',now=3),range(4)))
    assert sum(map(len,targets))==1
    assert len(store.overview()['tasks'])==2


def test_dispatch_failure_rolls_back_new_task_and_can_retry_without_tool_replay(tmp_path,monkeypatch):
    store,registry=setup(tmp_path);perform(store,registry)
    original=store._event
    def fail(con,task,kind,data,now):
        if kind=='record_delivered':raise RuntimeError('crash after insert')
        return original(con,task,kind,data,now)
    monkeypatch.setattr(store,'_event',fail)
    assert store.dispatch_record_events(mode='local',now=3)==[]
    data=store.overview();assert len(data['tasks'])==1
    delivery=data['records']['deliveries'][0]
    assert delivery['state']=='blocked' and delivery['attempts']==1
    monkeypatch.setattr(store,'_event',original)
    store.retry_record_delivery(delivery['id'])
    assert len(store.dispatch_record_events(mode='local',now=4))==1
    assert store.overview()['records']['observation_count']==1
    assert store.overview()['records']['deliveries'][0]['attempts']==2


def test_missing_supervisor_retains_record_until_explicit_retry(tmp_path):
    store,registry=setup(tmp_path,supervisor='future-manager');perform(store,registry)
    store.dispatch_record_events(mode='local',now=3)
    delivery=store.overview()['records']['deliveries'][0]
    assert delivery['state']=='blocked' and len(store.overview()['tasks'])==1
    cfg=store.config.model_copy(update={'agents':store.config.agents+(store.config.agents[0].model_copy(update={'key':'future-manager'}),)})
    restored=AgencyStore(store.path,cfg,supervisor_agent='future-manager')
    restored.retry_record_delivery(delivery['id'])
    assert len(restored.dispatch_record_events(mode='local',now=4))==1


@pytest.mark.parametrize('state',['blocked','waiting_user','uncertain','cancelled','failed'])
def test_attention_outcomes_are_delivered_with_reason(tmp_path,state):
    store,registry=setup(tmp_path)
    store.submit(agent='operator',objective='Read',inputs={},request_key='one',now=0)
    task=store.claim(mode='local',now=1);store.transition(task,state,reason='test_reason',now=2)
    store.dispatch_record_events(mode='local',now=3)
    review=store.claim(mode='local',now=4)
    result=store.context(review)['received_records'][0]
    assert result['state']==state and result['reason']=='test_reason' and result['observations']==[]


def test_lost_worker_reports_uncertain_execution_without_fabricating_evidence(tmp_path):
    store,registry=setup(tmp_path)
    store.submit(agent='operator',objective='Read',inputs={},request_key='one',now=0)
    task=store.claim(mode='local',now=1);store.begin_tool(task,registry.tools['test.read'],{'id':'one'},now=2)
    assert store.claim(mode='local',now=602) is None
    store.dispatch_record_events(mode='local',now=603)
    data=store.overview()
    assert data['records']['observation_count']==0
    assert data['records']['outcomes'][0]['state']=='uncertain'
    assert data['records']['deliveries'][0]['state']=='delivered'
    assert data['resources'][0]['halted']=='uncertain_execution'


def test_receipt_and_observation_are_atomic_and_observations_are_immutable(tmp_path,monkeypatch):
    store,registry=setup(tmp_path)
    store.submit(agent='operator',objective='Read',inputs={},request_key='one',now=0)
    task=store.claim(mode='local',now=1);tool=registry.tools['test.read'];call=store.begin_tool(task,tool,{'id':'one'},now=2)
    original=store.records.observe
    def fail(*args):original(*args);raise RuntimeError('crash after evidence insert')
    monkeypatch.setattr(store.records,'observe',fail)
    with pytest.raises(RuntimeError):store.finish_tool(task,tool,call,result={'area_m2':2000},now=3)
    assert store.overview()['records']['observation_count']==0
    with store.transaction() as con:assert con.execute('SELECT state FROM tool_calls WHERE id=?',(call,)).fetchone()[0]=='started'
    monkeypatch.setattr(store.records,'observe',original)
    store.finish_tool(task,tool,call,result={'area_m2':2000},now=4)
    record=store.overview()['records']['observations'][0]
    with store.transaction() as con:
        callrow=con.execute('SELECT * FROM tool_calls WHERE id=?',(call,)).fetchone()
        assert store.records.observe(con,task,tool,callrow,{'area_m2':2000},5)==record['id']
        with pytest.raises(ValueError):store.records.observe(con,task,tool,callrow,{'area_m2':9999},5)
        with pytest.raises(sqlite3.IntegrityError):con.execute('UPDATE observations SET fields=? WHERE id=?',('{}',record['id']))
        with pytest.raises(sqlite3.IntegrityError):con.execute('DELETE FROM observations WHERE id=?',(record['id'],))


def test_completion_and_notification_roll_back_together(tmp_path,monkeypatch):
    store,registry=setup(tmp_path)
    store.submit(agent='operator',objective='Read',inputs={},request_key='one',now=0)
    task=store.claim(mode='local',now=1)
    original=store.records.outcome
    def fail(*args):original(*args);raise RuntimeError('crash')
    monkeypatch.setattr(store.records,'outcome',fail)
    with pytest.raises(RuntimeError):store.transition(task,'complete',result={'test':True},now=2)
    view=store.overview()
    assert view['tasks'][0]['state']=='running'
    assert view['records']['outcomes']==[] and view['records']['deliveries']==[]


def test_read_scope_and_paging_preserve_large_evidence(tmp_path):
    store,registry=setup(tmp_path,payload={'description':'a'*24000})
    perform(store,registry,mode='synthetic')
    assert store.dispatch_record_events(mode='local',now=3)==[]
    store.dispatch_record_events(mode='synthetic',now=3)
    task=store.claim(mode='synthetic',now=4)
    record=store.overview()['records']['observations'][0]
    assert store.context(task)['received_records'][0]['observations'][0]['payload_omitted']
    offset=0;text=''
    while True:
        store.read_record(task,record['id'],offset,now=5)
        task=store.claim(mode='synthetic',now=6)
        part=store.context(task)['record_reads'][0];text+=part['text']
        if part['next_offset'] is None:break
        offset=part['next_offset']
    assert json.loads(text)['payload']=={'description':'a'*24000}
    store.submit(agent='operator',objective='Unrelated',inputs={'outcome_id':record['id']},request_key='unrelated',mode='local',now=7)
    other=store.claim(mode='local',now=8)
    with pytest.raises(ValueError,match='not delivered'):store.read_record(other,record['id'],now=9)
    assert store.context(other)['received_records']==[]


def test_wait_then_complete_creates_versioned_outcomes_and_preserves_previous_values(tmp_path):
    store,registry=setup(tmp_path,supervisor=None)
    perform(store,registry)
    registry.handlers['test.read']=lambda args:{'price_tl':9000000,'area_m2':1800}
    perform(store,registry,key='second',now=20)
    observations=store.overview()['records']['observations']
    assert {o['fields']['price_tl']['value'] for o in observations}=={8000000,9000000}
    assert all(o['listing_ref']=='DEMO' for o in observations)
    store.submit(agent='operator',objective='Wait',inputs={},request_key='wait',now=40)
    task=store.claim(mode='local',now=41);store.transition(task,'waiting_user',reason='Need reply',now=42)
    store.resume(task['id'],{'answer':'yes'},now=43)
    task=store.claim(mode='local',now=44);store.transition(task,'complete',result={'ok':True},now=45)
    rows=[r for r in store.overview()['records']['outcomes'] if r['task_id']==task['id']]
    assert {(r['version'],r['state']) for r in rows}=={(1,'waiting_user'),(2,'complete')}
    with store.transaction() as con:
        with pytest.raises(sqlite3.IntegrityError):con.execute('DELETE FROM record_outcomes WHERE task_id=?',(task['id'],))


def test_records_private_api_requires_viewer_and_does_not_expose_publicly(engine,tmp_path):
    store,registry=setup(tmp_path);perform(store,registry)
    record=store.overview()['records']['observations'][0]
    url='/control/private/records/observation/'+record['id']
    with TestClient(create_app(engine,'private-token-at-least-32-characters',viewer_nonce='open',agency_store=store)) as client:
        assert client.get(url).status_code==401
        client.get('/control/unlock/open')
        response=client.get(url)
        assert response.status_code==200 and response.json()['payload']['area_m2']==2000
        assert response.headers['cache-control']=='no-store'
        assert client.get('/control/private/records/observation/missing').status_code==404


def test_v3_migration_keeps_old_tasks_without_replaying_history(tmp_path):
    store,registry=setup(tmp_path);origin=perform(store,registry)
    with store.transaction() as con:
        for table in ('record_deliveries','record_outcomes','observations'):con.execute('DROP TABLE '+table)
        for table in ('record_events','record_subscriptions','incident_tasks','source_incidents'):
            con.execute('DROP TABLE '+table)
        con.execute('ALTER TABLE inbox DROP COLUMN retry')
        con.execute('ALTER TABLE tool_calls DROP COLUMN operation_key')
        con.execute('ALTER TABLE tool_calls DROP COLUMN attempt')
        con.execute('ALTER TABLE inbox DROP COLUMN record_version')
        con.execute('ALTER TABLE inbox DROP COLUMN caused_by_task_id')
        con.execute('UPDATE agency_meta SET version=3')
    store.initialize();store.initialize()
    data=store.overview()
    assert data['tasks'][0]['id']==origin and data['tasks'][0]['state']=='complete'
    assert data['records']['outcomes']==[]
    assert store.dispatch_record_events(mode='local',now=3)==[]


def test_real_timer_ingress_to_static_tool_then_manager_without_operator_model(tmp_path):
    store,registry=setup(tmp_path)
    store.register_trigger('morning','timer',{'agent':'operator','objective':'Read synthetic data','inputs':{'id':'one'},'interval_seconds':86400,'executor_tool':'test.read'},next_at=1)
    assert len(store.tick(now=1))==1
    provider=Provider([decision('complete')])
    engine=AgentEngine(store,registry,{'codex':provider})
    for n in range(3):engine.step(now=n+2)
    data=store.overview()
    assert len(provider.calls)==1 and len(data['tasks'])==2
    assert all(t['state']=='complete' for t in data['tasks'])
    assert data['records']['deliveries'][0]['state']=='delivered'


def test_record_read_action_can_read_delivered_outcome_without_a_tool_call(tmp_path):
    store,registry=setup(tmp_path);perform(store,registry)
    outcome=store.overview()['records']['outcomes'][0]
    provider=Provider([decision('record',outcome['id'],args={'offset':0}),decision('complete')])
    engine=AgentEngine(store,registry,{'codex':provider})
    engine.step(now=3);engine.step(now=4)
    received=provider.calls[-1]['context']['record_reads'][0]
    assert received['record_kind']=='outcome'
    assert json.loads(received['text'])['id']==outcome['id']
    assert store.overview()['records']['observation_count']==1
