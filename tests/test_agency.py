import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from verda.agency.registry import AgentDefinition, AgencyConfig, Registry, ResourceBlocked, ToolDefinition
from verda.agency.store import AgencyStore
from verda.agency.engine import AgentEngine, Decision
from verda.providers import Generation
from verda.api import create_app


def config():
    return AgencyConfig(agents=(
        AgentDefinition(key='manager', label='Manager', description='Coordinates', prompt='Coordinate', delegates=('operator',)),
        AgentDefinition(key='operator', label='Operator', description='Uses tools', prompt='Read', tools=('test.read',)),
    ), tools=(ToolDefinition(key='test.read', label='Read', description='Synthetic read', transport='python', resource='site',
                           min_interval_seconds=10, input_schema={'type':'object','properties':{'id':{'type':'string'}},'required':['id'],'additionalProperties':False}),))


def setup(tmp_path, cfg=None, handlers=None):
    cfg = cfg or config()
    s = AgencyStore(tmp_path / 'agency.sqlite', cfg, supervisor_agent=None)
    s.initialize()
    return s, Registry(cfg, handlers or {'test.read': lambda args: {'read': args['id']}})


def submit(s, agent='operator', **kwargs):
    return s.submit(agent=agent, objective='Read fixture', inputs={'id':'one'}, request_key=kwargs.pop('request_key','one'), **kwargs)


def decision(kind, target='', objective='', args=None):
    return Decision(kind=kind, target=target, objective=objective, arguments_json=json.dumps(args or {}), listing_ref='', summary='Synthetic operation')


class Provider:
    def __init__(self, choices): self.choices, self.calls = iter(choices), []
    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return Generation('fake',None,'invocation',1,{},next(self.choices))


def test_agent_delegates_observes_tool_result_and_resumes_parent(tmp_path):
    s,r = setup(tmp_path)
    root = submit(s, 'manager', now=0)
    provider = Provider([decision('delegate','operator','Read one',{'id':'one'}),decision('tool','test.read',args={'id':'one'}),decision('complete',args={'read':'one'}),decision('complete')])
    engine = AgentEngine(s,r,{'codex':provider})
    for n in range(4): assert engine.step(now=n+1)
    view=s.overview()
    assert all(t['state']=='complete' for t in view['tasks'])
    child=next(t for t in view['tasks'] if t['parent_id']==root)
    assert child['trace_id']==next(t for t in view['tasks'] if t['id']==root)['trace_id']
    assert provider.calls[-1]['context']['delegated_results'][0]['result']['data']=={'read':'one'}
    assert 'tool_finished' in [e['kind'] for e in view['events']]
    assert 'child_result_received' in [e['kind'] for e in view['events']]


def test_queue_merges_sources_and_priority_with_single_agent_owner(tmp_path):
    s,r=setup(tmp_path)
    low=submit(s,request_key='scheduled',source='timer:morning',priority=10,now=0)
    high=submit(s,request_key='message',source='agent:manager',priority=80,now=1)
    with ThreadPoolExecutor(2) as pool:
        claims=list(pool.map(lambda _:s.claim(mode='local',now=2), range(2)))
    owned=[c for c in claims if c]
    assert len(owned)==1 and owned[0]['id']==high
    s.transition(owned[0],'complete',result={},now=3)
    assert s.claim(mode='local',now=4)['id']==low


def test_statics_do_not_call_model(tmp_path):
    s,r=setup(tmp_path)
    submit(s,executor_tool='test.read',now=0)
    engine=AgentEngine(s,r,providers={})
    assert engine.step(now=1) and engine.step(now=2)
    assert s.overview()['tasks'][0]['result']=={'executor':'code','data':{'read':'one'}}


def test_resource_delay_is_shared_between_agents_without_repeating_model(tmp_path):
    cfg=config().model_copy(update={'agents':config().agents+(AgentDefinition(key='extra',label='Extra',description='Other',prompt='Use',tools=('test.read',)),)})
    s,r=setup(tmp_path,cfg)
    submit(s,executor_tool='test.read',now=0)
    submit(s,'extra',request_key='two',executor_tool='test.read',now=0)
    engine=AgentEngine(s,r,providers={})
    for _ in range(4): engine.step(now=1)
    queued=next(t for t in s.overview()['tasks'] if t['state']=='queued')
    assert queued['reason']=='rate_limit_wait' and queued['pending'] is not None
    assert engine.step(now=10) is False
    assert engine.step(now=11) and engine.step(now=12)
    assert all(t['state']=='complete' for t in s.overview()['tasks'])


def test_resource_blocker_stops_queue(tmp_path):
    def blocked(args): raise ResourceBlocked('captcha')
    s,r=setup(tmp_path,handlers={'test.read':blocked})
    submit(s,executor_tool='test.read',now=0)
    AgentEngine(s,r,providers={}).step(now=1)
    v=s.overview()
    assert v['resources'][0]['halted']=='captcha'
    assert v['tasks'][0]['state']=='blocked'


def test_started_tool_after_worker_loss_is_uncertain_never_replayed(tmp_path):
    s,r=setup(tmp_path)
    submit(s,now=0)
    task=s.claim(mode='local',now=1)
    s.begin_tool(task,r.tools['test.read'],{'id':'one'},now=2)
    assert s.claim(mode='local',now=602) is None
    v=s.overview()
    assert v['tasks'][0]['state']=='uncertain'
    assert v['resources'][0]['halted']=='uncertain_execution'
    with pytest.raises(RuntimeError,match='lease_lost'):
        s.transition(task,'complete',now=603)


def test_ungranted_tool_and_delegation_rejected(tmp_path):
    s,r=setup(tmp_path)
    submit(s,now=0)
    provider=Provider([decision('delegate','manager','wrong')])
    AgentEngine(s,r,{'codex':provider}).step(now=1)
    assert s.overview()['tasks'][0]['state']=='blocked'
    with pytest.raises(ValueError,match='granted'):r.validate('manager','test.read',{'id':'one'})
    with pytest.raises(Exception):r.validate('operator','test.read',{'id':2})


def test_timer_delivery_survives_restart_and_coalesces_missed_intervals(tmp_path):
    s,r=setup(tmp_path)
    spec={'agent':'operator','objective':'Morning read','inputs':{'id':'one'},'interval_seconds':100,'executor_tool':'test.read'}
    s.register_trigger('morning','timer',spec,next_at=10)
    assert len(s.tick(now=450))==1
    restored=AgencyStore(s.path,s.config)
    assert restored.tick(now=450)==[]
    assert restored.overview()['triggers'][0]['next_at']==510
    assert len(restored.tick(now=510))==1


def test_events_and_tasks_have_idempotency_keys(tmp_path):
    s,r=setup(tmp_path)
    s.register_trigger('replies','event',{'event_type':'reply.received','agent':'operator','objective':'Read reply'})
    assert len(s.publish('event1','reply.received',{'id':'one'},now=0))==1
    assert s.publish('event1','reply.received',{'id':'one'},now=1)==[]
    with pytest.raises(ValueError):s.publish('event1','reply.received',{'id':'changed'},now=1)
    first=submit(s,now=1)
    assert submit(s,now=2)==first
    with pytest.raises(ValueError):submit(s,priority=90,now=2)


def test_wait_resume_and_definition_change(tmp_path):
    s,r=setup(tmp_path)
    task_id=submit(s,now=0)
    provider=Provider([decision('wait'),decision('complete')])
    engine=AgentEngine(s,r,{'codex':provider})
    engine.step(now=1)
    s.resume(task_id,{'answer':'one'},now=2)
    engine.step(now=3)
    assert provider.calls[-1]['context']['inputs']['user_reply']=={'answer':'one'}
    submit(s,request_key='changed',now=4)
    changed=r.config.model_copy(update={'agents':(r.config.agents[0].model_copy(update={'version':'2'}),r.config.agents[1])})
    AgentEngine(s,Registry(changed),providers={}).step(now=5)
    assert next(t for t in s.overview()['tasks'] if t['request_key']=='changed')['reason']=='configuration_changed'


def test_script_extension_receives_json_without_shell(tmp_path):
    cfg=config()
    tool=cfg.tools[0].model_copy(update={'transport':'script','command':(sys.executable,'-c','import sys,json; print(json.dumps({"echo":json.load(sys.stdin)}))')})
    r=Registry(cfg.model_copy(update={'tools':(tool,)}))
    assert r.call('test.read',{'id':'$(do-not-execute)'})=={'echo':{'id':'$(do-not-execute)'}}


def test_mcp_tool_extension_against_local_test_server(tmp_path):
    file=tmp_path/'mcp_server.py'
    file.write_text('from mcp.server import MCPServer\nm=MCPServer("test")\n@m.tool()\ndef echo(id: str) -> dict:\n return {"echo":id}\nif __name__=="__main__": m.run()\n')
    cfg=config()
    tool=cfg.tools[0].model_copy(update={'transport':'mcp','command':(sys.executable,str(file)),'remote_name':'echo'})
    r=Registry(cfg.model_copy(update={'tools':(tool,)}))
    result = r.call('test.read',{'id':'synthetic'})
    assert json.loads(result['content'][0]['text']) == {'echo':'synthetic'}


def test_agency_private_view_never_becomes_public(engine,tmp_path):
    s,r=setup(tmp_path)
    submit(s)
    with TestClient(create_app(engine,'private-token-at-least-32-characters',viewer_nonce='one',agency_store=s)) as c:
        assert c.get('/control/private/agency').status_code==401
        c.get('/control/unlock/one')
        response=c.get('/control/private/agency')
        assert response.status_code==200
        assert len(response.json()['tasks'])==1
        assert 'token' not in response.json()['tasks'][0]
        assert response.headers['cache-control']=='no-store'


def test_write_tool_not_enabled_by_agent_prompt(tmp_path):
    cfg=config()
    cfg=cfg.model_copy(update={'tools':(cfg.tools[0].model_copy(update={'effect':'write'}),)})
    calls=[]
    s,r=setup(tmp_path,cfg,{'test.read':lambda a:calls.append(a)})
    submit(s,executor_tool='test.read',now=0)
    AgentEngine(s,r,providers={}).step(now=1)
    assert not calls and s.overview()['tasks'][0]['state']=='waiting_user'


def test_new_agent_can_be_registered_without_editing_core(tmp_path):
    cfg = AgencyConfig(agents=(AgentDefinition(key='new-agent',label='New',description='Custom',prompt='Custom',tools=('sum',)),),
        tools=(ToolDefinition(key='sum',label='Sum',description='Add',transport='python'),))
    s = AgencyStore(tmp_path/'new.sqlite',cfg)
    s.initialize()
    s.submit(agent='new-agent',objective='Add',inputs={'a':2,'b':3},request_key='custom',executor_tool='sum',now=0)
    r=Registry(cfg,{'sum':lambda x:{'sum':x['a']+x['b']}})
    engine=AgentEngine(s,r,providers={})
    engine.step(now=1); engine.step(now=2)
    assert s.overview()['tasks'][0]['result']['data']=={'sum':5}


def test_authenticated_task_ingress_does_not_grant_browser_writes(engine,tmp_path):
    s,r=setup(tmp_path)
    token='private-token-at-least-32-characters'
    with TestClient(create_app(engine,token,viewer_nonce='open',agency_store=s)) as c:
        c.get('/control/unlock/open')
        body={'agent':'operator','objective':'Read','inputs':{'id':'one'},'request_key':'api','executor_tool':'test.read'}
        assert c.post('/api/agency/tasks',json=body).status_code==401
        headers={'Authorization':'Bearer '+token}
        assert c.post('/api/agency/tasks',json=body,headers={**headers,'Origin':'http://elsewhere.test'}).status_code==403
        assert c.post('/api/agency/tasks',json=body,headers=headers).status_code==200
        assert len(s.overview()['tasks'])==1
        assert c.post('/api/agency/tasks',json=body,headers=headers).status_code==200
        assert len(s.overview()['tasks'])==1


def test_manager_can_dispatch_multiple_jobs_before_operator_is_free(tmp_path):
    s,r=setup(tmp_path)
    root=submit(s,'manager',now=0)
    provider=Provider([decision('dispatch','operator','First',{'id':'one'}),decision('dispatch','operator','Second',{'id':'two'}),decision('complete')])
    engine=AgentEngine(s,r,{'codex':provider})
    for n in range(3):engine.step(now=n+1)
    rows=s.overview()['tasks']
    assert len([t for t in rows if t['parent_id']==root and t['state']=='queued'])==2
    assert next(t for t in rows if t['id']==root)['state']=='waiting_children'
    # A blocked child wakes the manager to decide, rather than deadlocking it.
    child=s.claim(mode='local',now=4)
    s.transition(child,'blocked',reason='missing_data',now=5)
    parent=next(t for t in s.overview()['tasks'] if t['id']==root)
    assert parent['state']=='queued'


def test_agent_uses_its_configured_provider(tmp_path):
    cfg=config()
    from verda.runtime import AgentModel
    cfg=cfg.model_copy(update={'agents':(cfg.agents[0],cfg.agents[1].model_copy(update={'model':AgentModel(provider='custom',model='local-choice')}))})
    s,r=setup(tmp_path,cfg)
    submit(s,now=0)
    other=Provider([decision('complete')])
    AgentEngine(s,r,{'custom':other}).step(now=1)
    assert other.calls[0]['model']=='local-choice'
    assert s.overview()['tasks'][0]['state']=='complete'


def test_lost_child_worker_notifies_waiting_manager(tmp_path):
    s,r=setup(tmp_path)
    root=submit(s,'manager',now=0)
    parent=s.claim(mode='local',now=1)
    s.delegate(parent,'operator','Read',{'id':'one'},None,now=2)
    child=s.claim(mode='local',now=3)
    s.begin_tool(child,r.tools['test.read'],{'id':'one'},now=4)
    # Expiry wakes the manager, while preserving uncertain child state.
    awakened=s.claim(mode='local',now=604)
    assert awakened['id']==root
    assert next(t for t in s.overview()['tasks'] if t['id']==child['id'])['state']=='uncertain'


def test_inspector_distinguishes_configured_tools_from_verified_code():
    from verda.agency.inspection import tool_description
    cfg = config()
    script = cfg.tools[0].model_copy(update={'transport':'script','command':('/python','/private/test.py','--secret','not-for-ui')})
    registry = Registry(cfg.model_copy(update={'tools':(script,)}))
    description = tool_description(registry,script)
    assert description['status'] == 'configured'
    assert description['implementation']['script_name'] == 'test.py'
    assert 'not-for-ui' not in json.dumps(description)
    assert 'command' not in description
    registry = Registry(cfg,{'test.read':lambda a:a})
    assert tool_description(registry,cfg.tools[0])['status'] == 'ready'


def test_worker_status_is_reported_and_expires(tmp_path,monkeypatch):
    s,r=setup(tmp_path)
    monkeypatch.setattr('verda.agency.store.time.time',lambda: 100)
    engine=AgentEngine(s,r,providers={})
    assert engine.step(now=100) is False
    worker=s.overview()['workers'][0]
    assert worker['active'] and worker['state']=='idle'
    monkeypatch.setattr('verda.agency.store.time.time',lambda: 116)
    assert s.overview()['workers'][0]['active'] is False
    engine.close(now=116)
    assert s.overview()['workers'][0]['state']=='stopped'


def test_worker_migration_preserves_existing_tasks(tmp_path):
    s,r=setup(tmp_path)
    task=submit(s,now=0)
    with s.transaction() as con:
        con.execute('DROP TABLE record_deliveries')
        con.execute('DROP TABLE record_outcomes')
        con.execute('DROP TABLE observations')
        for table in ('record_events','record_subscriptions','incident_tasks','source_incidents'):
            con.execute('DROP TABLE '+table)
        con.execute('ALTER TABLE inbox DROP COLUMN retry')
        con.execute('ALTER TABLE tool_calls DROP COLUMN operation_key')
        con.execute('ALTER TABLE tool_calls DROP COLUMN attempt')
        con.execute('ALTER TABLE inbox DROP COLUMN record_version')
        con.execute('ALTER TABLE inbox DROP COLUMN caused_by_task_id')
        con.execute('DROP TABLE workers')
        con.execute('UPDATE agency_meta SET version=2')
    s.initialize()
    assert s.overview()['tasks'][0]['id']==task
    assert s.overview()['workers']==[]


def test_queue_counts_include_records_beyond_visible_page(tmp_path):
    s,r=setup(tmp_path)
    for n in range(201):submit(s,request_key=str(n),now=n)
    v=s.overview()
    assert v['total_tasks']==201 and len(v['tasks'])==200
    assert v['tasks_truncated'] is True
    assert sum(c['count'] for c in v['task_counts'])==201
