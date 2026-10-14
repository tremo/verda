"""The seller message through the whole system: agent -> gate -> outbox -> browser job -> worker -> receipt. A scripted page stands in for Chrome."""
import json
import sqlite3
import time

import pytest

from test_agency import Provider, decision
from test_send import DETAIL, HEAD, FakePage
from verda.agency import message as M
from verda.agency import research
from verda.agency.browser import BrowserBridge
from verda.agency.engine import AgentEngine, default_registry
from verda.agency.jev import run_worker
from verda.agency.registry import AgencyConfig
from verda.agency.send import Stopped, live_digest
from verda.agency.store import AgencyStore
from verda.agency.tuning import apply

ID = '1344169039'
LISTING_URL = f'https://www.sahibinden.com/ilan/arsa-{ID}/detay'


@pytest.fixture
def system(tmp_path, monkeypatch):
    config = AgencyConfig.load(None)
    store = AgencyStore(tmp_path / 'a.sqlite', config, supervisor_agent=None)
    store.initialize()
    registry = default_registry(config, store)
    current = {'decision': {'state': 'research_required', 'reasons': ['archaeological_sit', 'natural_sit'], 'conditions': [], 'penalties': {}},
               'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'legacy_lifecycle': None,
               'facts': {'parcel_key': {'value': '12/3', 'grade': 'resmi'}, 'shared_title': {'value': False, 'grade': 'beyan'}, 'access': {'value': 'direct', 'grade': 'beyan'},
                         'price_tl': {'value': 3000000.0, 'grade': 'beyan'}, 'sale_area_m2': {'value': 1053.0, 'grade': 'resmi'}, 'route_minutes': [{'value': 14.0}]},
               'seen': {'listing_read': True, 'thread_read': True, 'tkgm_looked_up': True, 'corridor_checked': True}}
    monkeypatch.setattr(research, 'fact_status', lambda store, listing, mode='local': current)
    monkeypatch.setattr(M, 'within_window', lambda now: True)
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO observations(id,call_id,task_id,agent,trace_id,listing_ref,mode,tool,kind,captured_at,arguments,payload,payload_hash,fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('o1', 'c1', 't1', 'a', 'tr', ID, 'local', 'sahibinden.read_listing', 'source', 1.0, '{}', json.dumps({**DETAIL, 'url': LISTING_URL}), 'h', '[]'))
    con.close()
    return store, registry, current


def submit_send(store, key='send-1'):
    store.submit(agent='sahibinden', objective='Mesaj gönder', inputs={'listing_id': ID}, listing_ref=ID, request_key=key, executor_tool='sahibinden.send_message')


class Reader:
    """The worker's reader: the page to use and a clock inside the message window."""
    browser_factory = None

    def __init__(self, page, clock=None):
        self.page, self.clock = page, clock or (lambda: time.time())

    sleep = staticmethod(lambda seconds: None)

    def ready(self): return None
    def open_page(self, url): return self.page


def table(store, sql, *a):
    with store.transaction() as con:
        return [dict(r) for r in con.execute(sql, a)]


def work(store, registry, page):
    engine = AgentEngine(store, registry, providers={})
    assert engine.step()                                        # decision + gate + job
    run_worker(store, reader=Reader(page))
    assert engine.step()                                        # the agent sees the result
    return engine


def sit_text():
    return table.__globals__['M'].render(ID, ['natural_sit', 'archaeological_sit'])


def test_a_dry_run_goes_through_the_whole_path_and_sends_nothing(system):
    store, registry, _ = system
    BrowserBridge(store).select('jev')
    submit_send(store)
    page = FakePage()
    work(store, registry, page)
    row = table(store, 'SELECT * FROM outbox')[0]
    assert row['state'] == 'dry_run' and json.loads(row['fields']) == ['archaeological_sit', 'natural_sit'] and row['text'] == sit_text()
    assert 'click' not in page.log and page.value == '' and page.closed
    call = table(store, "SELECT * FROM tool_calls WHERE tool='sahibinden.send_message'")[0]
    out = json.loads(call['result'])['output']
    assert call['state'] == 'complete' and out['status'] == 'dry_run' and out['outbox_state'] == 'dry_run' and out['dry_run'] is True
    assert json.loads(call['arguments'])['dry_run'] is True and json.loads(call['arguments'])['text'] == row['text']
    assert table(store, "SELECT kind FROM observations WHERE tool='sahibinden.send_message'")[0]['kind'] == 'external_receipt'
    assert table(store, "SELECT halted FROM resources WHERE key='sahibinden'")[0]['halted'] is None


def test_the_gate_refuses_with_a_reason_the_agent_can_read_and_takes_no_resource(system):
    store, registry, current = system
    ask = decision('tool', 'sahibinden.send_message', args={'listing_id': ID})
    engine = AgentEngine(store, registry, providers={'codex': Provider([ask, decision('complete'), ask])})
    apply(store, [{'key': 'sender.mode', 'value': 'off'}])
    store.submit(agent='sahibinden', objective='Mesaj gönder', inputs={'listing_id': ID}, listing_ref=ID, request_key='a')
    assert engine.step()
    call = table(store, "SELECT * FROM tool_calls WHERE tool='sahibinden.send_message'")[0]
    out = json.loads(call['result'])['output']
    assert call['state'] == 'complete' and out == {'sent': False, 'refused': 'sender_off', 'why': M.REFUSALS['sender_off']} and call['resource'] is None
    assert not table(store, "SELECT name FROM sqlite_master WHERE name='outbox'") and not table(store, 'SELECT * FROM browser_jobs')      # nothing was even prepared
    assert engine.step()                                                  # the first run ends
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    current['next_action'] = {'action': 'sahibinden.read_thread'}
    store.submit(agent='sahibinden', objective='Mesaj gönder', inputs={'listing_id': ID}, listing_ref=ID, request_key='b')
    assert engine.step()
    last = json.loads(table(store, "SELECT result FROM tool_calls WHERE tool='sahibinden.send_message' ORDER BY rowid DESC")[0]['result'])['output']
    assert last['refused'] == 'no_ask_step'


def test_one_message_per_run_and_a_second_run_waits_for_the_gap(system):
    store, registry, _ = system
    BrowserBridge(store).select('jev')
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    after = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'text': sit_text(), 'head': HEAD, 'editor': True, 'blocker': None, 'thread': [{'who': 'sen', 'text': sit_text()}]}
    ask = decision('tool', 'sahibinden.send_message', args={'listing_id': ID})
    provider = Provider([ask, ask, decision('complete'), ask])
    engine = AgentEngine(store, registry, providers={'codex': provider})
    store.submit(agent='sahibinden', objective='Mesaj gönder', inputs={'listing_id': ID}, listing_ref=ID, request_key='run-1')
    assert engine.step()                                                  # first ask: gate passes, job queued
    run_worker(store, reader=Reader(FakePage(after=after)))
    assert engine.step()                                                  # the same task asks a second time
    outputs = [json.loads(r['result'])['output'] for r in table(store, "SELECT result FROM tool_calls WHERE tool='sahibinden.send_message' ORDER BY rowid")]
    assert outputs[0]['status'] == 'confirmed' and outputs[1]['refused'] == 'one_message_per_run'
    assert engine.step()                                                  # complete
    store.submit(agent='sahibinden', objective='Mesaj gönder', inputs={'listing_id': ID}, listing_ref=ID, request_key='run-2')
    assert engine.step()                                                  # a new run, right after: the gap between two messages applies
    outputs = [json.loads(r['result'])['output'] for r in table(store, "SELECT result FROM tool_calls WHERE tool='sahibinden.send_message' ORDER BY rowid")]
    assert outputs[2]['refused'] in ('too_soon', 'already_asked') and len(table(store, 'SELECT * FROM browser_jobs')) == 1


def test_an_unverified_delivery_blocks_any_new_message_for_that_listing(system):
    store, registry, _ = system
    BrowserBridge(store).select('jev')
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    submit_send(store)
    after = {'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a1/b2', 'text': 'x', 'head': HEAD, 'editor': True, 'blocker': None, 'thread': []}
    work(store, registry, FakePage(after=after))
    assert table(store, 'SELECT state FROM outbox')[0]['state'] == 'delivery_unknown'
    assert M.gate(store, {'id': 'other-task', 'mode': 'local'}, {'listing_id': ID}, time.time() + 3600)['reason'] == 'delivery_unresolved'
    with store.transaction() as con:
        row = M.rows(con, ID)[0]
        M.resolve_unknown(con, row['id'], True, time.time(), 'Konuşmada gördüm')
    assert table(store, 'SELECT state FROM outbox')[0]['state'] == 'confirmed'


def test_a_security_control_after_the_click_quarantines_the_call_and_before_it_is_an_ordinary_stop(system):
    store, registry, _ = system
    bridge = BrowserBridge(store)
    bridge.select('jev')
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    submit_send(store)
    # blocked before any click: the draft goes back, the call fails normally, the source halts
    page = FakePage(form={'blocker': 'captcha'})
    engine = AgentEngine(store, registry, providers={})
    assert engine.step()
    run_worker(store, reader=Reader(page))
    assert table(store, 'SELECT state,phase FROM outbox')[0] == {'state': 'prepared', 'phase': None}
    call = table(store, "SELECT state FROM tool_calls WHERE tool='sahibinden.send_message'")[0]
    assert call['state'] == 'failed' and table(store, "SELECT halted FROM resources WHERE key='sahibinden'")[0]['halted'] == 'captcha'


def test_a_worker_lost_after_the_click_intent_is_never_retried(system):
    store, registry, _ = system
    bridge = BrowserBridge(store)
    bridge.select('jev')
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    submit_send(store)
    assert AgentEngine(store, registry, providers={}).step()
    bridge.attach('w', driver='jev')
    job = bridge.claim('w')
    assert bridge.checkpoint_send(job, 'w', job['arguments']['text_hash'], 'https://banaozel.sahibinden.com/mesajlarim/x') is True
    bridge.expire(now=time.time() + 700)
    assert table(store, 'SELECT state FROM outbox')[0]['state'] == 'delivery_unknown'
    assert table(store, "SELECT state FROM tool_calls WHERE tool='sahibinden.send_message'")[0]['state'] == 'uncertain'
    assert table(store, "SELECT state FROM inbox WHERE agent='sahibinden'")[0]['state'] == 'uncertain'


def test_resuming_a_source_closes_tasks_it_stopped_under_an_older_configuration(system):
    store, registry, _ = system
    config_old = AgencyConfig.load(None)
    with store.transaction() as con:
        for tid, rev, state in (('old1', 'old-revision', 'blocked'), ('cur1', store.config.revision, 'blocked')):
            con.execute("INSERT INTO inbox(id,request_key,request_hash,agent,objective,inputs,trace_id,source,mode,priority,state,created_at,available_at,config_revision,definition,reason,pending) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, tid, 'h', 'sahibinden', 'sahibinden.read_listing tek ilan', '{}', 'tr' + tid, 'agent:manager', 'local', 50, state, 1.0, 1.0, rev, '{}', 'unusual_access', json.dumps({'target': 'sahibinden.read_listing'})))
            con.execute("INSERT OR IGNORE INTO resources(key,halted) VALUES('sahibinden','unusual_access')")
        con.execute("UPDATE resources SET halted='unusual_access' WHERE key='sahibinden'")
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc1','sahibinden','local','unusual_access','open',1.0)")
        con.execute("INSERT INTO incident_tasks VALUES('inc1','old1')")
        con.execute("INSERT INTO incident_tasks VALUES('inc1','cur1')")
    store.resolve_source('inc1', 'Kullanıcı kontrol etti')
    states = {r['id']: r['state'] for r in table(store, 'SELECT id,state FROM inbox')}
    assert states['old1'] == 'cancelled' and states['cur1'] == 'queued'


def test_resuming_a_source_does_not_replay_repeating_timer_runs_or_batches(system):
    store, registry, _ = system
    rows = (('t_old', 'timer:inbox-sync', 'Gelen kutusunu eşitle', 1.0), ('t_new', 'timer:inbox-sync', 'Gelen kutusunu eşitle', 5.0),
            ('batch', 'agent:manager', 'sahibinden.read_listing toplu işi', 2.0), ('single', 'agent:manager', 'sahibinden.read_listing tek ilan', 3.0))
    with store.transaction() as con:
        for tid, source, objective, at in rows:
            con.execute("INSERT INTO inbox(id,request_key,request_hash,agent,objective,inputs,trace_id,source,mode,priority,state,created_at,available_at,config_revision,definition,reason,pending) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, tid, 'h', 'sahibinden', objective, '{}', 'tr' + tid, source, 'local', 50, 'blocked', at, at, store.config.revision, '{}', 'unusual_access', json.dumps({'target': 'sahibinden.read_listing'})))
            con.execute("INSERT OR IGNORE INTO resources(key,halted) VALUES('sahibinden','unusual_access')")
        con.execute("UPDATE resources SET halted='unusual_access' WHERE key='sahibinden'")
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc2','sahibinden','local','unusual_access','open',1.0)")
        for tid, *_ in rows:
            con.execute("INSERT INTO incident_tasks VALUES('inc2',?)", (tid,))
    store.resolve_source('inc2', 'Kullanıcı kontrol etti')
    states = {r['id']: r['state'] for r in table(store, 'SELECT id,state FROM inbox')}
    assert states == {'t_old': 'cancelled', 't_new': 'queued', 'batch': 'cancelled', 'single': 'queued'}


def add_thread_read(store, obs_id, at, found, messages):
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO observations(id,call_id,task_id,agent,trace_id,listing_ref,mode,tool,kind,captured_at,arguments,payload,payload_hash,fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (obs_id, 'call-' + obs_id, 't1', 'a', 'tr', ID, 'local', 'sahibinden.read_thread', 'tool_observation', at, json.dumps({'listing_id': ID}),
                     json.dumps({'listing_id': ID, 'found': found, 'url': f'https://banaozel.sahibinden.com/mesajlarim/{ID}/a/b' if found else 'https://banaozel.sahibinden.com/mesajlarim',
                                 'messages': [{'id': f'{i:012x}', 'role': role, 'text': text} for i, (role, text) in enumerate(messages)]}), 'h', '[]'))
    con.close()


def test_the_1214768369_replay_a_later_read_that_misses_the_conversation_cannot_make_the_gate_ask_again(system):
    store, registry, current = system
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    # the parcel was asked; the seller answered and raised the sit question without being asked. Later a read says "no conversation".
    add_thread_read(store, 'r1', 10.0, True, [('user', 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz?'), ('seller', 'parsel 51, sit durumu yok')])
    add_thread_read(store, 'r2', 20.0, False, [])
    verdict = M.gate(store, {'id': 'task-x', 'mode': 'local'}, {'listing_id': ID}, time.time())
    assert verdict['ok'] is False and verdict['reason'] == 'asked_before' and verdict['fields']['seller_wrote'] == ['natural_sit', 'archaeological_sit']
    assert not table(store, 'SELECT * FROM outbox')                                         # nothing prepared


def test_a_conversation_read_before_and_missing_now_holds_every_question_until_it_is_found_again(system):
    store, registry, current = system
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    add_thread_read(store, 'r1', 10.0, True, [('user', 'Merhaba, ilan hâlâ satılık mı?'), ('seller', 'Evet')])
    add_thread_read(store, 'r2', 20.0, False, [])
    assert M.gate(store, {'id': 'task-x', 'mode': 'local'}, {'listing_id': ID}, time.time())['reason'] == 'conversation_unconfirmed'
    add_thread_read(store, 'r3', 30.0, True, [('user', 'Merhaba, ilan hâlâ satılık mı?'), ('seller', 'Evet')])
    assert M.gate(store, {'id': 'task-y', 'mode': 'local'}, {'listing_id': ID}, time.time())['ok'] is True


def test_the_checkpoint_reads_the_history_again_and_refuses_the_click_when_it_changed_after_the_gate(system):
    store, registry, _ = system
    BrowserBridge(store).select('jev')
    apply(store, [{'key': 'sender.mode', 'value': 'live'}])
    submit_send(store)
    engine = AgentEngine(store, registry, providers={})
    assert engine.step()                                                  # the gate passes: no conversation known yet
    # between the gate and the click a read arrives: the seller already wrote about the sit status
    add_thread_read(store, 'r1', time.time(), True, [('user', 'Merhaba, ada/parsel bilgisini paylaşabilir misiniz?'), ('seller', 'Parsel 12, sit yok')])
    page = FakePage()
    run_worker(store, reader=Reader(page))
    assert 'click' not in page.log
    call = table(store, "SELECT result FROM tool_calls WHERE tool='sahibinden.send_message'")[0]
    assert json.loads(call['result'])['output']['reason'] == 'asked_before'
    refused = [json.loads(e['data']) for e in table(store, "SELECT data FROM agency_events WHERE kind='send_click_refused'")]
    assert refused and refused[0]['reason'] == 'asked_before'
    assert table(store, 'SELECT state FROM outbox')[0]['state'] == 'prepared'
    assert M.gate(store, {'id': 'other', 'mode': 'local'}, {'listing_id': ID}, time.time() + 3600)['reason'] == 'asked_before'    # and the gate now says so too
