import json
import sqlite3
import uuid

import pytest

from verda.agency.attention import acknowledge, attention
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore

QUIET = {'skipped_ids': {}}


@pytest.fixture(autouse=True)
def no_watchdog(monkeypatch):
    """These tests count the topics of a nearly empty store; the watchdog (tested on its own) would add 'no scan yet'."""
    monkeypatch.setattr('verda.agency.attention.watchdog_items', lambda store, mode, now: [])


def _store(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _call(store, tool, args, state, at, error=None):
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute("INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,result,started_at,finished_at,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                    (uuid.uuid4().hex, 'T', tool, 'sahibinden', 'read', state, json.dumps(args), json.dumps({'output': None, 'error': error}), at - 1, at))
    con.close()


def test_three_failures_of_a_step_are_reported_and_a_later_success_closes_them(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        _call(store, 'sahibinden.read_listing', {'listing_id': '1234567890'}, 'failed', 1000.0 + i, error='read_failed')
    _call(store, 'sahibinden.read_listing', {'listing_id': '1111111111'}, 'failed', 1000.0, error='read_failed')      # one failure: retried, not yet a topic
    out = attention(store, now=2000.0, queue=QUIET)
    stuck = [i for i in out['items'] if i['kind'] == 'step_stuck']
    assert len(stuck) == 1 and stuck[0]['listings'] == ['1234567890'] and 'read_failed' in stuck[0]['detail'] and out['count'] == 1
    _call(store, 'sahibinden.read_listing', {'listing_id': '1234567890'}, 'complete', 1500.0)
    assert attention(store, now=2000.0, queue=QUIET)['count'] == 0


def test_a_topic_can_be_marked_seen_and_returns_only_when_its_cause_changes(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        _call(store, 'tkgm.lookup', {'parcel_key': '12/3', 'listing_id': '1234567890'}, 'failed', 1000.0 + i, error='found_false')
    first = attention(store, now=2000.0, queue=QUIET)['items'][0]
    assert first['ackable']
    acknowledge(store, first['key'])
    assert attention(store, now=2000.0, queue=QUIET)['count'] == 0
    for i in range(3):
        _call(store, 'tkgm.lookup', {'parcel_key': '12/3', 'listing_id': '1234567890'}, 'failed', 90_000.0 + i, error='found_false')   # next day, new failures
    assert attention(store, now=91_000.0, queue=QUIET)['count'] == 1


def test_stopped_tasks_open_sources_questions_and_removed_listings_are_listed_by_weight(tmp_path):
    store = _store(tmp_path)
    store.submit(agent='sahibinden', objective='bir iş', inputs={}, request_key='k1')
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute("UPDATE inbox SET state='blocked', reason='read_failed'")
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc1','sahibinden','local','captcha','open',5.0)")
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', '1000000004', 'local', 'sahibinden.read_listing', 'sahibinden', 'tool_observation', 1.0, '{}',
                     json.dumps({'listing_id': '1000000004', 'unavailable': True}, separators=(',', ':')), uuid.uuid4().hex, '{}'))
    con.close()
    queue = {'skipped_ids': {'waits_for_sender': ['1111111111', '2222222222'], 'human': ['3333333333'], 'needs_neighborhood': ['4444444444']}}
    out = attention(store, now=2000.0, queue=queue)
    kinds = {i['kind']: i for i in out['items']}
    assert set(kinds) == {'source_blocked', 'task_stopped', 'human_review', 'place_missing', 'questions_waiting', 'listings_removed'}
    assert out['count'] == 4 and out['waiting'] == 1 and out['info'] == 1                 # the badge counts only what needs the user
    assert kinds['source_blocked']['ackable'] is False                                     # a blocked source is cleared by fixing it, not by looking away
    assert kinds['listings_removed']['listings'] == ['1000000004'] and kinds['listings_removed']['severity'] == 'info'


def _inbox_list(store, rows, complete, at):
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', None, 'local', 'sahibinden.list_inbox', 'sahibinden', 'tool_observation', at, '{}',
                     json.dumps({'url': 'https://banaozel.sahibinden.com/mesajlarim', 'complete': complete, 'rows': rows}), uuid.uuid4().hex, '{}'))
    con.close()


def test_a_recent_listing_stops_when_enough_rows_in_a_row_need_nothing():
    from verda.agency.inbox import enough_rows
    rows = [{'title': f'ilan {i}'} for i in range(30)]
    needs = {'ilan 0', 'ilan 3'}                                         # the newest rows are the ones that changed
    settled = lambda r: r['title'] not in needs
    assert not enough_rows(rows[:12], settled)                           # not enough settled rows below the last one that needs something
    assert enough_rows(rows[:19], settled)                               # rows 4..18 = 15 settled rows in a row: nothing older can need anything
    assert not enough_rows(rows[:19], lambda r: r['title'] != 'ilan 17')  # a row that needs something near the bottom keeps the scroll going


def test_absence_of_a_conversation_is_known_only_from_a_recent_complete_list_plus_later_ones(tmp_path):
    from verda.agency.inbox import known_titles
    store = _store(tmp_path)
    day = 86400.0
    assert known_titles(store, now=10 * day) is None                      # no list at all: absence proves nothing
    _inbox_list(store, [{'title': 'Zeytinlik Yaka', 'counterpart': 'A', 'last_at_text': 'x', 'unread': 0}], complete=False, at=1 * day)
    assert known_titles(store, now=2 * day) is None                       # only a partial list: still nothing
    _inbox_list(store, [{'title': 'Tarla Cumalı', 'counterpart': 'B', 'last_at_text': 'x', 'unread': 0}], complete=True, at=2 * day)
    _inbox_list(store, [{'title': 'Yeni konuşma Palamutbükü', 'counterpart': 'C', 'last_at_text': 'x', 'unread': 1}], complete=False, at=3 * day)
    titles = known_titles(store, now=4 * day)
    assert any('tarla cumali' in t for t in titles) and any('palamutbükü' in t for t in titles)     # the full list and the later recent one
    assert not any('zeytinlik yaka' in t for t in titles)                 # a list from before the full one is not needed
    assert known_titles(store, now=2 * day + 9 * day) is None             # the complete list is too old: browse again


def test_a_tkgm_rate_limit_is_explained_as_something_to_wait_out_never_to_work_around(tmp_path):
    store = _store(tmp_path)
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc403','tkgm','local','tkgm_http_403','open',5.0)")
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inccap','sahibinden','local','captcha','open',6.0)")
    con.close()
    items = {i['key']: i for i in attention(store, now=10.0, queue=QUIET)['items']}
    tkgm, other = items['incident:inc403'], items['incident:inccap']
    assert tkgm['title'] == 'tkgm erişimi durdu' and 'Sebep: tkgm_http_403.' in tkgm['detail'] and not tkgm['ackable']
    assert 'aşmaya çalışmaz' in tkgm['detail'] and 'bekle' in tkgm['detail'] and 'devam ettir' in tkgm['detail']
    assert 'aşmaya çalışmaz' not in other['detail'] and 'Sebep: captcha.' in other['detail']       # other sources keep the general wording
