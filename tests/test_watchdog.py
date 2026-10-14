"""The watchdog: every rule catches the incident it was written for, stays quiet when the rule holds, and runs as a model-free timer task."""
import json
import sqlite3
import uuid

import pytest

from verda.agency import message, watchdog
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore

DAY = 86400.0
NOW = 1_800_000_000.0                       # 15 Jan 2027, 11:00 Istanbul: inside the message window


@pytest.fixture
def store(tmp_path):
    s = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    s.initialize()
    con = sqlite3.connect(s.path)
    with con:
        message.ensure(con)
    con.close()
    return s


def sql(store, query, *args):
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute(query, args)
    con.close()


def observe(store, tool, listing, payload, at, task='t'):
    sql(store, 'INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', uuid.uuid4().hex, uuid.uuid4().hex, task, 'a', 'tr', listing, 'local', tool, None,
        'tool_observation', at, '{}', json.dumps(payload), uuid.uuid4().hex, '{}')


def sent(store, listing, fields, text, at):
    sql(store, "INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,created_at,updated_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        uuid.uuid4().hex, listing, 'local', json.dumps(fields), text, 'h', 'v1', uuid.uuid4().hex, 'confirmed', at - 60, at, at)


def call(store, tool, args, at, state='complete', result=None):
    sql(store, "INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,result,started_at,finished_at,operation_key,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        uuid.uuid4().hex, 't', tool, None, 'read', state, json.dumps(args), json.dumps({'output': result, 'error': None if state == 'complete' else 'read_failed'}), at, at, uuid.uuid4().hex, 1)


def thread(found, *messages):
    return {'listing_id': '1000000101', 'found': found, 'messages': [{'id': f'{i:012x}', 'role': r, 'text': t} for i, (r, t) in enumerate(messages)]}


ROAD = message.render('1000000101', ['access'])


def test_a_question_the_conversation_already_answered_is_a_critical_finding(store):
    # asked parcel, road and sit, the seller answered all three; weeks later a read missed the conversation and the road question went again
    observe(store, 'sahibinden.read_thread', '1000000101', thread(True, ('user', 'ada/parsel bilgisini paylaşabilir misiniz? Parselin doğrudan kadastral yol cephesi var mı?'),
                                                                    ('seller', 'parsel 51, yola 20 metre cephesi var')), NOW - 7 * DAY + 3600)
    observe(store, 'sahibinden.read_thread', '1000000101', thread(False), NOW - 3 * 3600)
    sent(store, '1000000101', ['access'], ROAD, NOW - 3000)
    observe(store, 'sahibinden.read_thread', '1000000101', thread(True, ('user', 'ada/parsel...'), ('seller', 'parsel 51'), ('user', ROAD)), NOW - 2000)
    found = watchdog.repeat_question(store, 'local', NOW)
    assert [(f['severity'], f['listings']) for f in found] == [('critical', ['1000000101'])]
    assert 'daha önce sorulmuştu: kadastral yol' in found[0]['detail'] and 'satıcı zaten yazmıştı' in found[0]['detail'] and 'son okuma bulamamıştı' in found[0]['detail']


def test_a_first_question_and_a_new_topic_are_not_findings(store):
    observe(store, 'sahibinden.read_thread', '1000000102', {**thread(True, ('user', 'ada/parsel bilgisini paylaşabilir misiniz?'), ('seller', 'ada yok parsel 88')), 'listing_id': '1000000102'}, NOW - DAY)
    text = message.render('1000000102', ['access'])
    sent(store, '1000000102', ['access'], text, NOW - 3000)
    observe(store, 'sahibinden.read_thread', '1000000102', {**thread(True, ('user', 'ada/parsel...'), ('seller', 'ada yok parsel 88'), ('user', text)), 'listing_id': '1000000102'}, NOW - 2000)
    sent(store, '1000000103', ['access'], message.render('1000000103', ['access']), NOW - 900)          # no conversation before
    assert watchdog.repeat_question(store, 'local', NOW) == []


def test_messages_outside_the_window_or_too_close_together_are_critical(store):
    from datetime import datetime
    night = datetime(2027, 1, 14, 22, 30, tzinfo=message.TZ).timestamp()
    sent(store, '1', ['access'], 'a', night)
    sent(store, '2', ['access'], 'b', NOW - 600)
    sent(store, '3', ['access'], 'c', NOW - 590)
    titles = [f['title'] for f in watchdog.send_policy(store, 'local', NOW)]
    assert titles == ['1 mesaj izinli saat dışında gitti', '1 mesaj bir öncekine çok yakın gitti']


def test_send_attempts_that_keep_stopping_are_a_loop(store):
    for i in range(SEND := watchdog.SEND_ATTEMPTS):
        call(store, 'sahibinden.send_message', {'listing_id': '1000000103'}, NOW - 900 * (i + 1), result={'status': 'not_sent', 'reason': 'listing_changed'})
    call(store, 'sahibinden.send_message', {'listing_id': '1000000103'}, NOW - 2 * DAY)
    found = watchdog.send_loop(store, 'local', NOW)
    assert [(f['listings'], f['count']) for f in found] == [(['1000000103'], SEND)]


def test_a_conversation_read_once_and_missing_now_is_found(store):
    observe(store, 'sahibinden.read_thread', '1000000101', thread(True, ('seller', 'x')), NOW - DAY)
    observe(store, 'sahibinden.read_thread', '1000000101', thread(False), NOW - 100)
    observe(store, 'sahibinden.read_thread', '1000000001', {'listing_id': '1000000001', 'found': False, 'messages': []}, NOW - 100)    # never had one
    assert watchdog.conversation_lost(store, 'local', NOW)[0]['listings'] == ['1000000101']
    observe(store, 'sahibinden.read_thread', '1000000101', thread(True, ('seller', 'x')), NOW - 10)
    assert watchdog.conversation_lost(store, 'local', NOW) == []


def test_a_stalled_inbox_list_that_calls_itself_complete_is_flagged(store):
    row = lambda i: {'title': f'Arsa {i}', 'counterpart': None, 'last_at_text': None, 'unread': 0}
    observe(store, 'sahibinden.list_inbox', None, {'complete': True, 'rows': [row(i) for i in range(216)]}, NOW - 3 * DAY)
    observe(store, 'sahibinden.list_inbox', None, {'complete': True, 'rows': [row(i) for i in range(30)]}, NOW - 3600)
    assert watchdog.inbox_list_short(store, 'local', NOW)[0]['title'] == 'Gelen kutusu listesi eksik okunmuş (30 satır, en uzunu 216)'


def test_an_area_exclusion_on_land_that_is_not_what_is_for_sale_is_flagged(store):
    sql(store, "INSERT INTO assessments(id,listing_ref,mode,policy_version,input_fingerprint,facts,basis,conflicts,state,reasons,conditions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        'a1', '1000000104', 'local', 'p', 'f', '{}', '{}', '[]', 'excluded', '["area_below_minimum"]', '[]', NOW - DAY)
    for seq, (grade, value) in enumerate((('beyan', 3260.0), ('resmi', 678.02))):
        sql(store, """INSERT INTO claims(id,listing_ref,mode,field,value,scope,source_kind,subject,proposer,status,grade,reason,verifier,created_at,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", f'c{seq}', '1000000104', 'local', 'sale_area_m2', json.dumps(value), 'listing', 'listing', 'this_parcel', 'code', 'accepted', grade,
            'verified', 'v1', NOW - DAY, f'd{seq}')
    assert watchdog.exclusion_doubt(store, 'local', NOW)[0]['listings'] == ['1000000104']


def test_a_full_scan_that_stops_before_the_last_page_and_a_market_not_read_are_flagged(store):
    sql(store, "INSERT INTO inbox(id,request_key,request_hash,agent,objective,inputs,trace_id,source,mode,priority,state,created_at,available_at,config_revision,definition) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        'full', 'full', 'h', 'sahibinden', 'tam tarama', json.dumps({'routine': 'full_scan'}), 'tr', 'timer:weekly-full-scan', 'local', 10, 'waiting_user', NOW - 3 * DAY, NOW - 3 * DAY, 'r', '{}')
    for page in range(1, 9):
        observe(store, 'sahibinden.search', None, {'page': page, 'total_reported': 290, 'has_more': True, 'listings': []}, NOW - 3 * DAY + page, task='full')
    titles = [f['title'] for f in watchdog.scan_coverage(store, 'local', NOW)]
    assert titles == ['İlan taraması yapılmıyor', 'Tam tarama 8/15. sayfada kaldı']


def test_a_rule_that_cannot_be_checked_is_a_finding_not_a_pass(store, monkeypatch):
    def broken(store, mode, now):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(watchdog, 'RULES', (('broken', 'Bozuk kural', broken),))
    report = watchdog.check(store, now=NOW)
    assert report['ok'] is False and report['rules'] == [{'rule': 'broken', 'label': 'Bozuk kural', 'status': 'error', 'error': 'OperationalError'}]
    assert report['findings'][0]['title'] == 'Nöbetçi kuralı kontrol edilemedi: Bozuk kural'


def test_the_timer_runs_the_watchdog_as_code_records_it_and_wakes_no_one(store):
    """No model call (no provider is registered), the result is kept as a calculation record, no other task appears."""
    from verda.agency.engine import AgentEngine, default_registry
    spec = next(t for t in json.load(open(watchdog.__file__.replace('watchdog.py', 'schedules.json'))) if t['key'] == 'watchdog')
    store.register_trigger(key='watchdog', kind='timer', spec=spec['spec'], next_at=NOW - 1)
    assert len(store.tick(now=NOW)) == 1
    engine = AgentEngine(store, default_registry(store.config, store), providers={})
    while engine.step(now=NOW + 1):
        pass
    con = sqlite3.connect(store.path); con.row_factory = sqlite3.Row
    tasks = [dict(r) for r in con.execute('SELECT agent, state, executor_tool FROM inbox')]
    records = [dict(r) for r in con.execute("SELECT kind, payload FROM observations WHERE tool='watchdog.check'")]
    con.close()
    assert tasks == [{'agent': 'manager', 'state': 'complete', 'executor_tool': 'watchdog.check'}]
    assert len(records) == 1 and records[0]['kind'] == 'calculation' and 'findings' in json.loads(records[0]['payload'])


def test_broken_rules_reach_the_panel_and_the_dashboard_status(store):
    from verda.agency.attention import attention
    from verda.publish import status
    observe(store, 'sahibinden.read_thread', '1000000101', thread(True, ('user', 'Parselin doğrudan kadastral yol cephesi var mı?'), ('seller', 'yola 20 metre cephesi var')), NOW - DAY)
    sent(store, '1000000101', ['access'], ROAD, NOW - 3000)
    topics = attention(store, now=NOW, queue={'skipped_ids': {}})['items']
    assert any(t['title'] == 'KRİTİK · #1000000101 ilanına tekrar soru gitti' and t['severity'] == 'action' for t in topics)
    page = status.build(store, now=NOW, running={})
    assert any(h['level'] == 'bad' and h['text'].startswith('KRİTİK · #1000000101') for h in page['health'])
    assert page['watchdog']['ok'] is False and page['watchdog']['findings'][0]['rule'] == 'repeat_question'


def test_the_dashboard_says_when_the_opportunity_score_could_not_be_computed(store, monkeypatch):
    from verda.publish import projection
    monkeypatch.setattr('verda.agency.opportunity.rank_all', lambda *a, **k: 1 / 0)
    docs = projection.build(store, now=NOW)
    page = next(d for c, k, d in docs if (c, k) == ('meta', 'v2status'))
    assert page['health'][0] == {'level': 'bad', 'text': page['health'][0]['text']} and page['health'][0]['text'].startswith('Fırsat puanı hesaplanamadı (ZeroDivisionError')


def test_a_service_taken_out_of_launchd_still_shows_on_the_status_page(store):
    from verda.publish import status
    page = status.build(store, now=NOW, running={'com.verda.panel': True, 'com.verda.publisher': True})
    texts = [h['text'] for h in page['health']]
    assert 'Ajan işçisi: durduruldu (yüklü değil)' in texts and 'Tarayıcı işçisi: durduruldu (yüklü değil)' in texts and 'Panel: çalışıyor' in texts
