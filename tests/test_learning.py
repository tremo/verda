import json
import sqlite3
import uuid

from verda.agency.learning import build, covers, gather, legacy_fields
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore


def data(**kw):
    base = {'thread': None, 'checks': {}, 'tasks': [], 'incidents': [], 'flags': [], 'outbox': []}
    base.update(kw)
    return base


def thread(*roles, url='https://banaozel.sahibinden.com/mesajlarim/1234567890/a/b', found=True):
    texts = {'user': 'Selamlar sit durumu ve kadastral yol var mı?', 'seller': 'Yol var, sit konusunu bilemem'}
    return {'found': found, 'url': url, 'checked_at': 100.0, 'older_omitted': False,
            'messages': [{'id': str(i), 'role': r, 'sent_at_text': None, 'text': texts[r], 'truncated': False} for i, r in enumerate(roles)]}


def test_a_message_is_read_for_what_it_asks_and_the_old_outbox_names_map_to_fields():
    assert covers('Selamlar sit durumu ve kadastral yol var mı?') == {'archaeological_sit', 'natural_sit', 'access'}
    assert covers('Ada parsel numaranız nedir?') == {'parcel_key'}
    assert covers('Arkeolojik sit var mı?') == {'archaeological_sit'}
    assert legacy_fields(['ada_parsel', 'cadastral_road_frontage', 'sit_status']) == {'parcel_key', 'access', 'archaeological_sit', 'natural_sit'}
    assert legacy_fields(['current_natural_sit_status_statement']) == {'natural_sit'}


def test_an_unanswered_question_shows_the_conversation_link_and_a_replied_one_says_nothing_certain_came_out():
    waiting = build(data(thread=thread('user')), ['natural_sit', 'access'])
    assert waiting['fields']['natural_sit']['state'] == 'asked' and waiting['fields']['natural_sit']['url'].startswith('https://banaozel.sahibinden.com/mesajlarim/')
    assert waiting['conversation'] == {'found': True, 'url': waiting['fields']['access']['url'], 'checked_at': 100.0, 'older_omitted': False, 'sent': 1, 'received': 0}
    replied = build(data(thread=thread('user', 'seller')), ['natural_sit'])
    assert replied['fields']['natural_sit']['state'] == 'replied'


def test_a_note_waiting_for_the_user_wins_and_nothing_sent_says_so_plainly():
    flagged = build(data(thread=thread('user', 'seller'), flags=[{'field': 'access', 'created_at': 5.0}]), ['access'])
    assert flagged['fields']['access']['state'] == 'unclear'
    planned = build(data(), ['archaeological_sit'], {'action': 'ask:sit'})
    assert planned['fields']['archaeological_sit']['state'] == 'to_ask' and 'kapalı' in planned['fields']['archaeological_sit']['label']
    idle = build(data(), ['natural_sit'])
    assert idle['fields']['natural_sit']['state'] == 'none'
    assert build(data(), ['natural_sit'], {'action': 'sahibinden.read_thread'})['fields']['natural_sit']['state'] == 'planned'
    assert build(data(), ['route_minutes'], context={'parcel_known': False})['fields']['route_minutes']['state'] == 'waiting'
    assert build(data(), ['route_minutes'], context={'parcel_known': True, 'official_known': False})['fields']['route_minutes']['state'] == 'waiting'


def test_old_system_messages_count_as_asked_and_a_halted_source_is_named():
    outbox = [{'state': 'confirmed', 'asked': ['access', 'natural_sit'], 'prepared_at': 1.0, 'sent_at': 50.0, 'text': 'Merhaba', 'url': 'https://banaozel.sahibinden.com/mesajlarim/1/a/b'},
              {'state': 'prepared', 'asked': ['parcel_key'], 'prepared_at': 60.0, 'sent_at': None, 'text': '', 'url': None}]
    out = build(data(outbox=outbox, incidents=[{'resource': 'tkgm', 'reason': 'tkgm_http_403', 'created_at': 70.0}]), ['access', 'parcel_key'])
    assert out['fields']['access'] == {'state': 'asked', 'label': 'Soru satıcıya gönderildi; yanıt bekleniyor', 'at': 50.0, 'url': 'https://banaozel.sahibinden.com/mesajlarim/1/a/b'}
    assert out['fields']['parcel_key']['state'] == 'draft'
    halted = build(data(incidents=[{'resource': 'tkgm', 'reason': 'tkgm_http_403', 'created_at': 70.0}]), ['parcel_key'])
    assert halted['fields']['parcel_key']['state'] == 'halted' and 'tkgm_http_403' in halted['fields']['parcel_key']['label']
    assert [e['kind'] for e in out['events']] == ['sent']                         # a halted source is listed only where it holds the listing back
    assert [e['kind'] for e in build(data(incidents=[{'resource': 'tkgm', 'reason': 'tkgm_http_403', 'created_at': 70.0}]), ['parcel_key'])['events']] == ['halt']


def test_gather_reads_the_thread_the_open_incident_and_nothing_else(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO observations(id,call_id,task_id,agent,trace_id,listing_ref,mode,tool,kind,captured_at,arguments,payload,payload_hash,fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('o1', 'c1', 't1', 'a', 'tr', '1234567890', 'local', 'sahibinden.read_thread', 'source', 5.0, '{}', json.dumps(thread('user', 'seller')), 'h', '[]'))
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, 'tkgm', 'local', 'tkgm_http_403', 'open', 9.0))
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, 'sahibinden', 'local', 'session_lost', 'resolved', 3.0))
    con.close()
    got = gather(store, '1234567890')
    assert got['thread']['found'] and got['thread']['checked_at'] == 5.0
    assert [i['resource'] for i in got['incidents']] == ['tkgm'] and got['tasks'] == [] and got['outbox'] == []
    assert build(got, ['natural_sit'])['fields']['natural_sit']['state'] == 'replied'


def test_a_later_read_that_did_not_find_the_conversation_does_not_hide_one_that_was_read(tmp_path):
    store = AgencyStore(tmp_path / 'b.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    con = sqlite3.connect(store.path)
    with con:
        for oid, at, payload in (('o1', 5.0, thread('user', 'seller')), ('o2', 9.0, {'found': False, 'listing_id': '1234567890', 'messages': []})):
            con.execute("INSERT INTO observations(id,call_id,task_id,agent,trace_id,listing_ref,mode,tool,kind,captured_at,arguments,payload,payload_hash,fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (oid, 'c' + oid, 't1', 'a', 'tr', '1234567890', 'local', 'sahibinden.read_thread', 'source', at, '{}', json.dumps(payload), 'h' + oid, '[]'))
    con.close()
    got = gather(store, '1234567890')
    assert got['thread']['found'] and got['thread']['checked_at'] == 5.0 and len(got['thread']['messages']) == 2      # the seller's answer is still known
