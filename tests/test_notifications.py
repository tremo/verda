import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from test_agency import config, Provider, decision
from test_records import setup, perform
from verda.agency.engine import AgentEngine
from verda.agency.store import AgencyStore
from verda.agency.registry import Registry, TransientToolError, ResourceBlocked
from verda.agency.health import inspect_health
from verda.api import create_app


def fanout_setup(tmp_path):
    cfg=config();research=cfg.agents[0].model_copy(update={'key':'research','label':'Research'})
    cfg=cfg.model_copy(update={'agents':cfg.agents+(research,)})
    store=AgencyStore(tmp_path/'events.sqlite',cfg);store.initialize()
    return store,Registry(cfg,{'test.read':lambda a:{'area_m2':2000}})


def test_nonmanager_parent_and_supervisor_receive_independently(tmp_path):
    store,registry=fanout_setup(tmp_path)
    root=store.submit(agent='research',objective='Research',inputs={},request_key='root',now=0)
    parent=store.claim(mode='local',now=1)
    child=store.delegate(parent,'operator','Read',{},'DEMO',now=2)
    task=store.claim(mode='local',now=3)
    store.transition(task,'complete',result={'area_m2':2000},now=4)
    with ThreadPoolExecutor(3) as pool:results=list(pool.map(lambda _:store.dispatch_record_events(mode='local',now=5),range(3)))
    assert sum(map(len,results))==1
    deliveries=store.overview()['records']['deliveries']
    assert {d['target_agent'] for d in deliveries}=={'research','manager'}
    assert all(d['state']=='delivered' for d in deliveries)
    assert next(d for d in deliveries if d['target_agent']=='research')['target_task_id']==root
    assert len(store.overview()['tasks'])==3


def test_routine_observation_delivery_does_not_invoke_model(tmp_path):
    store,registry=setup(tmp_path)
    store.submit(agent='operator',objective='Read',inputs={'id':'one'},request_key='read',executor_tool='test.read',now=0)
    engine=AgentEngine(store,registry,providers={});engine.step(now=1)
    assert store.dispatch_record_events(mode='local',now=2)==[]
    data=store.overview();delivery=data['records']['deliveries'][0]
    assert delivery['state']=='delivered' and delivery['processing_state']=='observed' and not delivery['wake']
    assert len(data['tasks'])==1
    engine.step(now=3)
    assert len(store.dispatch_record_events(mode='local',now=4))==1


def test_subscription_filters_modes_and_prevents_recursive_evaluation(tmp_path):
    store,registry=fanout_setup(tmp_path)
    store.register_subscription(key='review',target_agent='research',event_types=['task.complete'],mode='synthetic',wake=True)
    store.register_subscription(key='manager-audit',target_agent='operator',event_types=['task.complete'],source_agents=['manager'],mode='synthetic',wake=True)
    perform(store,registry,mode='local')
    store.dispatch_record_events(mode='local',now=3)
    assert not any(d['target_agent']=='research' for d in store.overview()['records']['deliveries'])
    perform(store,registry,mode='synthetic',key='synthetic',now=20)
    jobs=store.dispatch_record_events(mode='synthetic',now=23)
    assert len(jobs)==2
    for n in range(2):
        task=store.claim(mode='synthetic',now=24+n)
        store.transition(task,'complete',result={'reviewed':True},now=24+n)
    assert store.dispatch_record_events(mode='synthetic',now=27)==[]
    audit=[d for d in store.overview()['records']['deliveries'] if d['target_agent']=='operator']
    assert len(audit)==1 and not audit[0]['wake']


def test_delivery_start_and_processing_ack_are_separate(tmp_path):
    store,registry=setup(tmp_path);perform(store,registry)
    store.dispatch_record_events(mode='local',now=3)
    d=next(d for d in store.overview()['records']['deliveries'] if d['wake'])
    assert d['state']=='delivered' and d['processing_state']=='waiting' and d['processed_at'] is None
    task=store.claim(mode='local',now=4)
    d=next(d for d in store.overview()['records']['deliveries'] if d['wake'])
    assert d['processing_state']=='processing' and d['started_at']==4
    store.transition(task,'waiting_user',reason='Need input',now=5)
    assert next(d for d in store.overview()['records']['deliveries'] if d['wake'])['processing_state']=='attention'
    store.resume(task['id'],{'answer':'yes'},now=6)
    task=store.claim(mode='local',now=7);store.transition(task,'complete',result={},now=8)
    d=next(d for d in store.overview()['records']['deliveries'] if d['wake'])
    assert d['processing_state']=='processed' and d['processed_at']==8 and d['started_at']==4


def test_one_failed_recipient_does_not_block_other_and_retries_are_bounded(tmp_path,monkeypatch):
    store,registry=fanout_setup(tmp_path)
    store.register_subscription(key='second',target_agent='research',event_types=['task.complete'],wake=True)
    perform(store,registry)
    original=store._submit
    def offline(*args,**kwargs):
        if kwargs['agent']=='manager':raise ConnectionError('temporary')
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'_submit',offline)
    assert len(store.dispatch_record_events(mode='local',now=3))==1
    manager=lambda:next(d for d in store.overview()['records']['deliveries'] if d['target_agent']=='manager' and d['wake'])
    assert manager()['available_at']==8 and manager()['state']=='pending'
    assert store.dispatch_record_events(mode='local',now=7)==[] and manager()['attempts']==1
    store.dispatch_record_events(mode='local',now=8)
    assert manager()['available_at']==38 and manager()['attempts']==2
    store.dispatch_record_events(mode='local',now=38)
    assert manager()['state']=='blocked' and manager()['attempts']==3
    monkeypatch.setattr(store,'_submit',original)
    store.retry_record_delivery(manager()['id'])
    assert len(store.dispatch_record_events(mode='local',now=39))==1
    assert store.overview()['records']['observation_count']==1


def test_retryable_read_reuses_decision_arguments_and_respects_retry_after(tmp_path):
    store,registry=setup(tmp_path,supervisor=None)
    calls=[]
    def read(args):
        calls.append(args)
        if len(calls)<2:raise TransientToolError(retry_after_seconds=240)
        return {'ok':True}
    registry.handlers['test.read']=read
    store.submit(agent='operator',objective='Read',inputs={'id':'same'},request_key='read',now=0)
    provider=Provider([decision('tool','test.read',args={'id':'same'}),decision('complete')])
    engine=AgentEngine(store,registry,{'codex':provider})
    engine.step(now=1)
    assert store.overview()['tasks'][0]['retry']['next_at']==241
    assert engine.step(now=240) is False
    engine.step(now=241);engine.step(now=242)
    assert calls==[{'id':'same'},{'id':'same'}] and len(provider.calls)==2
    with store.transaction() as con:
        receipts=con.execute('SELECT operation_key,attempt FROM tool_calls').fetchall()
        assert receipts[0]['operation_key']==receipts[1]['operation_key']
        assert [r['attempt'] for r in receipts]==[1,2]
    assert store.overview()['records']['observation_count']==1


def test_read_retries_survive_restart_and_exhaust_into_attention(tmp_path):
    store,registry=setup(tmp_path,supervisor=None)
    def fail(args):raise ConnectionError('private details must not be logged')
    registry.handlers['test.read']=fail
    store.submit(agent='operator',objective='Read',inputs={'id':'one'},request_key='read',executor_tool='test.read',now=0)
    engine=AgentEngine(store,registry,providers={});engine.step(now=1)
    restored=AgencyStore(store.path,store.config,supervisor_agent=None)
    engine=AgentEngine(restored,registry,providers={});engine.step(now=61);engine.step(now=361)
    t=restored.overview()['tasks'][0]
    assert t['state']=='blocked' and t['reason']=='read_retries_exhausted:ConnectionError'
    assert 'private details' not in json.dumps(restored.overview())
    assert restored.overview()['records']['observation_count']==0
    assert len(restored.overview()['records']['outcomes'])==1


def test_permanent_error_does_not_retry(tmp_path):
    store,registry=setup(tmp_path,supervisor=None)
    def fail(args):raise ValueError('bad input')
    registry.handlers['test.read']=fail
    store.submit(agent='operator',objective='Read',inputs={'id':'one'},request_key='read',executor_tool='test.read',now=0)
    engine=AgentEngine(store,registry,providers={});engine.step(now=1)
    assert engine.step(now=1000) is False
    assert store.overview()['tasks'][0]['retry'] is None


def test_source_block_groups_jobs_and_explicit_resolution_resumes_reads(tmp_path):
    store,registry=setup(tmp_path)
    def fail(args):raise ResourceBlocked('captcha')
    registry.handlers['test.read']=fail
    for n in range(3):store.submit(agent='operator',objective='Read',inputs={'id':str(n)},request_key=str(n),executor_tool='test.read',priority=90-n,now=0)
    engine=AgentEngine(store,registry,providers={})
    # Claim only operators here; delivery is independent and tested separately.
    for n in range(3):
        task=store.claim(mode='local',now=n+1)
        store.decision(task,decision('tool','test.read',args={'id':str(n)}).model_dump(),{},now=n+1)
        call=store.begin_tool(task,registry.tools['test.read'],{'id':str(n)},now=n+1)
        if call:store.finish_tool(task,registry.tools['test.read'],call,error='captcha',halt=True,now=n+1)
    data=store.overview()
    assert len(data['incidents'])==1 and len(data['incidents'][0]['tasks'])==3
    assert len(store.dispatch_record_events(mode='local',now=4))==1
    assert sum(d['wake'] for d in store.overview()['records']['deliveries'])==1
    store.resolve_source(data['incidents'][0]['id'],'User completed login and access is restored',now=5)
    assert store.overview()['resources'][0]['halted'] is None
    assert all(t['state']=='queued' for t in store.overview()['tasks'] if t['agent']=='operator')


def test_uncertain_external_write_never_retries_or_releases_source(tmp_path):
    store,registry=setup(tmp_path)
    tool=registry.tools['test.read'].model_copy(update={'effect':'write'})
    store.submit(agent='operator',objective='Synthetic write test',inputs={},request_key='write',now=0)
    task=store.claim(mode='local',now=1)
    call=store.begin_tool(task,tool,{'id':'one'},now=1)
    store.finish_tool(task,tool,call,error='timeout_after_submit',transient=True,now=2)
    data=store.overview()
    assert data['tasks'][0]['state']=='uncertain' and data['tasks'][0]['retry'] is None
    assert data['resources'][0]['halted']=='timeout_after_submit'
    with pytest.raises(ValueError,match='reconciliation'):store.resolve_source(data['incidents'][0]['id'],'Try again',now=3)


def test_storage_failure_stops_before_external_call_and_reports_outside_database(tmp_path,monkeypatch):
    store,registry=setup(tmp_path);calls=[]
    registry.handlers['test.read']=lambda args:calls.append(args)
    store.submit(agent='operator',objective='Read',inputs={'id':'one'},request_key='read',executor_tool='test.read',now=0)
    def broken(*args,**kwargs):raise sqlite3.OperationalError('disk unavailable')
    monkeypatch.setattr(store,'begin_tool',broken)
    engine=AgentEngine(store,registry,providers={})
    assert engine.step(now=1) is False and calls==[]
    health=inspect_health(store)
    assert health['workers'][0]['state']=='storage_error'
    assert health['workers'][0]['error']=='OperationalError'
    assert store.overview()['tasks'][0]['state']=='running'


def test_health_endpoint_still_responds_when_database_disappears(engine,tmp_path):
    store,registry=setup(tmp_path)
    with TestClient(create_app(engine,'private-token-at-least-32-characters',viewer_nonce='open',agency_store=store)) as client:
        assert client.get('/control/private/health').status_code==401
        client.get('/control/unlock/open');store.path.rename(store.path.with_suffix('.backup'))
        response=client.get('/control/private/health')
        assert response.status_code==200 and response.json()['storage_accessible'] is False
        assert client.get('/control/private/agency').status_code==503
        assert not store.path.exists()


def test_database_failure_after_tool_does_not_repeat_external_operation(tmp_path,monkeypatch):
    store,registry=setup(tmp_path,supervisor=None);calls=[]
    registry.handlers['test.read']=lambda args:calls.append(args) or {'ok':True}
    store.submit(agent='operator',objective='Read',inputs={'id':'one'},request_key='read',executor_tool='test.read',now=0)
    original=store.finish_tool
    def fail(*args,**kwargs):raise sqlite3.OperationalError('cannot commit')
    monkeypatch.setattr(store,'finish_tool',fail)
    engine=AgentEngine(store,registry,providers={});assert engine.step(now=1) is False
    assert len(calls)==1 and store.overview()['records']['observations']==[]
    monkeypatch.setattr(store,'finish_tool',original)
    assert engine.step(now=2) is False
    assert engine.step(now=602) is False
    assert len(calls)==1 and store.overview()['tasks'][0]['state']=='uncertain'


def test_v4_migration_preserves_pending_and_delivered_receipts_without_new_fanout(tmp_path):
    store,registry=setup(tmp_path);perform(store,registry)
    data=store.overview();outcome=data['records']['outcomes'][0]
    delivery=next(d for d in data['records']['deliveries'] if d['outcome_id'])
    with store.transaction() as con:
        con.execute('DROP TABLE record_deliveries')
        for table in ('record_events','record_subscriptions','incident_tasks','source_incidents'):con.execute('DROP TABLE '+table)
        con.execute('ALTER TABLE inbox DROP COLUMN retry')
        con.execute('ALTER TABLE tool_calls DROP COLUMN operation_key')
        con.execute('ALTER TABLE tool_calls DROP COLUMN attempt')
        con.execute('''CREATE TABLE record_deliveries(id TEXT PRIMARY KEY,outcome_id TEXT UNIQUE NOT NULL REFERENCES record_outcomes(id),
            task_id TEXT NOT NULL REFERENCES inbox(id),mode TEXT NOT NULL,route TEXT NOT NULL,target_agent TEXT NOT NULL,
            target_task_id TEXT,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
            created_at REAL NOT NULL,delivered_at REAL)''')
        con.execute('CREATE INDEX record_delivery_queue ON record_deliveries(mode,state,created_at)')
        columns=['id','outcome_id','task_id','mode','route','target_agent','target_task_id','state','attempts','error','created_at','delivered_at']
        con.execute('INSERT INTO record_deliveries VALUES('+','.join('?' for _ in columns)+')',[delivery[c] for c in columns])
        con.execute('UPDATE agency_meta SET version=4')
    store.initialize();store.initialize()
    restored=store.overview()['records']
    assert len(restored['deliveries'])==1 and restored['deliveries'][0]['id']==delivery['id']
    assert restored['outcomes'][0]['id']==outcome['id'] and restored['observation_count']==1
    assert len(store.dispatch_record_events(mode='local',now=4))==1
    assert store.dispatch_record_events(mode='local',now=5)==[]


def test_new_subscription_is_not_retroactive(tmp_path):
    store,registry=fanout_setup(tmp_path);perform(store,registry)
    store.register_subscription(key='new',target_agent='research',event_types=['task.complete'],wake=True)
    assert len(store.dispatch_record_events(mode='local',now=3))==1
    assert not any(d['target_agent']=='research' for d in store.overview()['records']['deliveries'])


def test_late_result_is_not_acknowledged_by_a_turn_that_never_saw_it(tmp_path):
    store,registry=setup(tmp_path)
    parent=store.submit(agent='manager',objective='Coordinate',inputs={},request_key='parent',now=0)
    task=store.claim(mode='local',now=1)
    child=store.delegate(task,'operator','Read',{},None,now=2,wait=False)
    running_parent=store.claim(mode='local',now=3)
    running_child=store.claim(mode='local',now=3)
    assert running_parent['id']==parent and running_child['id']==child
    store.transition(running_child,'complete',result={'fresh':True},now=4)
    store.transition(running_parent,'complete',result={'did_not_see_child':True},now=5)
    view=store.overview()
    assert next(t for t in view['tasks'] if t['id']==parent)['state']=='queued'
    delivery=view['records']['deliveries'][0]
    assert delivery['started_at'] is None and delivery['processed_at'] is None
    task=store.claim(mode='local',now=6)
    assert store.context(task)['received_records'][0]['result']=={'fresh':True}
    store.transition(task,'complete',result={'reviewed':True},now=7)
    assert store.overview()['records']['deliveries'][0]['processed_at']==7
