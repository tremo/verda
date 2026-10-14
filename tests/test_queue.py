import json
from pathlib import Path

from verda.agency.queue import work_queue


def _store(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _assessment(store, listing):
    import sqlite3, uuid
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO assessments(id,listing_ref,mode,policy_version,input_fingerprint,facts,basis,conflicts,state,reasons,conditions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, listing, 'local', 'p', uuid.uuid4().hex, '{}', '{}', '[]', 'research_required', '[]', '[]', 1.0))
    con.close()


def _observe_listing(store, listing, neighborhood, at=1.0):
    import sqlite3, uuid
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_listing', 'sahibinden', 'tool_observation', at, '{}',
                     json.dumps({'listing_id': listing, 'neighborhood': neighborhood}), uuid.uuid4().hex, '{}'))
    con.close()


def _observe_search(store, *listings, at=1.0):
    import sqlite3, uuid
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    rows = [{'listing_id': l, 'title': 'Arsa', 'url': f'https://www.sahibinden.com/ilan/arsa-{l}/detay'} for l in listings]
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', None, 'local', 'sahibinden.search', 'sahibinden', 'tool_observation', at, '{}',
                     json.dumps({'listings': rows}), uuid.uuid4().hex, '{}'))
    con.close()


def test_the_queue_hands_over_ready_arguments_and_skips_busy_stuck_and_finished_listings(tmp_path):
    store = _store(tmp_path)
    for listing in ('1111111111', '2222222222', '3333333333', '4444444444', '5555555555', '6666666666', '7777777777'):
        _assessment(store, listing)
    _observe_listing(store, '1111111111', 'Yaka')
    _observe_search(store, '2222222222')                     # 7777777777 is known only from a conversation: no address was ever seen
    plan = {
        '1111111111': {'next_action': {'action': 'tkgm.lookup', 'why': 'official_area_and_scope_needed'}, 'facts': {'parcel_key': {'value': '12/3'}}},
        '2222222222': {'next_action': {'action': 'sahibinden.read_listing', 'why': 'listing_detail_not_read'}, 'facts': {}},
        '3333333333': {'next_action': {'action': 'none', 'why': 'legacy_excluded'}, 'facts': {}},
        '4444444444': {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}},
        '5555555555': {'next_action': {'action': 'geo.route', 'why': 'route_to_destination_needed'}, 'facts': {}},
        '6666666666': {'next_action': {'action': 'human_review', 'why': 'conflict'}, 'facts': {}},
        '7777777777': {'next_action': {'action': 'sahibinden.read_listing', 'why': 'listing_detail_not_read'}, 'facts': {}},
    }
    from verda.agency.tuning import apply
    apply(store, [{'key': 'sender.mode', 'value': 'off'}])
    store.submit(agent='research', objective='x', inputs={}, request_key='busy', listing_ref='5555555555')   # an open task for this listing
    q = work_queue(store, {}, mode='local', status=lambda l: plan[l])
    groups = {g['action']: g for g in q['groups']}
    assert groups['tkgm.lookup']['operator'] == 'parcel'
    assert groups['tkgm.lookup']['batch'] == [{'listing_id': '1111111111', 'why': 'official_area_and_scope_needed', 'parcel_key': '12/3', 'neighborhood': 'Yaka'}]
    assert [i['listing_id'] for i in groups['sahibinden.read_listing']['batch']] == ['2222222222']
    assert 'geo.route' not in groups and q['skipped']['open_task'] == 1
    assert q['skipped']['waits_for_sender'] == 1 and q['skipped']['no_action'] == 1 and q['skipped']['human'] == 1
    assert q['skipped_ids']['no_listing_url'] == ['7777777777']                                   # never handed out to fail; shown to the user
    assert [g['action'] for g in q['groups']] == ['tkgm.lookup', 'sahibinden.read_listing']       # closest to a decision first


def test_a_missing_neighborhood_sends_the_listing_back_to_the_reader_twice_a_day_and_stuck_steps_stop(tmp_path):
    import sqlite3
    store = _store(tmp_path)
    for listing in ('1111111111', '2222222222'):
        _assessment(store, listing)
    _observe_listing(store, '1111111111', None, at=100.0)           # read long ago without a place
    _observe_listing(store, '2222222222', None, at=199_900.0)       # read twice today without a place: no third try
    _observe_listing(store, '2222222222', None, at=200_000.0)
    lookup = {'next_action': {'action': 'tkgm.lookup', 'why': 'w'}, 'facts': {'parcel_key': {'value': '5/6'}}}
    q = work_queue(store, {}, mode='local', status=lambda l: lookup, now=200_100.0)
    reread = [i['listing_id'] for g in q['groups'] if g['action'] == 'sahibinden.read_listing' for i in g['batch']]
    assert reread == ['1111111111'] and q['skipped']['needs_neighborhood'] == 1
    # three failed reads of the same listing today: it is no longer offered
    import sqlite3
    cols = [r[1] for r in sqlite3.connect(store.path).execute('pragma table_info(tool_calls)')]
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        for i in range(3):
            row = {'id': f'c{i}', 'task_id': 'T', 'tool': 'sahibinden.read_listing', 'arguments': '{"listing_id":"1111111111"}', 'state': 'failed',
                   'finished_at': 200_050.0, 'started_at': 200_040.0, 'attempt': 1, 'resource': 'sahibinden', 'effect': 'read'}
            use = {k: v for k, v in row.items() if k in cols}
            con.execute(f"INSERT INTO tool_calls({','.join(use)}) VALUES({','.join('?' * len(use))})", list(use.values()))
    con.close()
    q = work_queue(store, {}, mode='local', status=lambda l: lookup, now=200_100.0)
    assert not [i for g in q['groups'] for i in g['batch'] if i['listing_id'] == '1111111111'] and q['skipped']['needs_neighborhood'] == 2


def test_a_work_waiting_trigger_wakes_its_agent_only_when_the_queue_has_work(tmp_path):
    store = _store(tmp_path)
    store.register_trigger('sweep', 'timer', {'agent': 'manager', 'objective': 'x', 'interval_seconds': 900, 'when': 'work_waiting'}, next_at=100.0)
    store.register_trigger('plain', 'timer', {'agent': 'sahibinden', 'objective': 'y', 'interval_seconds': 900}, next_at=100.0)
    store.tick(now=1000.0, work_waiting=lambda: False)
    with store.transaction() as con:
        tasks = [dict(r) for r in con.execute("SELECT agent,source FROM inbox")]
        nexts = {r['key']: r['next_at'] for r in con.execute("SELECT key,next_at FROM triggers")}
    assert [t['source'] for t in tasks] == ['timer:plain']                 # the sweep stayed asleep
    assert nexts['sweep'] > 1000.0 and nexts['plain'] > 1000.0              # but its clock moved on
    store.tick(now=2000.0, work_waiting=lambda: True)
    with store.transaction() as con:
        sources = sorted(r['source'] for r in con.execute("SELECT source FROM inbox"))
    assert 'timer:sweep' in sources


def test_a_trigger_does_not_pile_up_runs_while_its_previous_run_is_still_open(tmp_path):
    store = _store(tmp_path)
    store.register_trigger('sweep', 'timer', {'agent': 'manager', 'objective': 'x', 'interval_seconds': 900}, next_at=100.0)
    store.tick(now=1000.0)
    store.tick(now=2000.0)                      # the first run is still queued: no second one
    store.tick(now=3000.0)
    with store.transaction() as con:
        assert con.execute("SELECT count(*) FROM inbox WHERE source='timer:sweep'").fetchone()[0] == 1
        con.execute("UPDATE inbox SET state='complete'")
    store.tick(now=4000.0)
    with store.transaction() as con:
        assert con.execute("SELECT count(*) FROM inbox WHERE source='timer:sweep'").fetchone()[0] == 2


def test_messages_are_offered_one_at_a_time_and_only_for_listings_the_gates_allow(tmp_path, monkeypatch):
    from verda.agency import message
    store = _store(tmp_path)
    for listing in ('1111111111', '2222222222', '3333333333'):
        _assessment(store, listing)
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}}
    monkeypatch.setattr(message, 'preview', lambda store, listing, mode, now, current=None: (
        {'ok': True, 'fields': ['natural_sit', 'archaeological_sit'], 'dry_run': True} if listing != '3333333333' else {'ok': False, 'reason': 'waiting_for_reply', 'message': 'x'}))
    q = work_queue(store, {}, mode='local', status=lambda l: ask)
    group = next(g for g in q['groups'] if g['action'] == 'sahibinden.send_message')
    assert group['operator'] == 'sahibinden' and group['waiting'] == 2 and len(group['batch']) == 1          # one message per task
    assert group['batch'][0]['asks'] == ['natural_sit', 'archaeological_sit']
    assert q['skipped']['sender_refused'] == 1 and q['sender'] == {'mode': 'dry_run', 'refusals': {'waiting_for_reply': 1}}


def test_listed_listings_get_their_first_five_photos_and_eliminated_ones_do_not(tmp_path):
    import sqlite3, uuid
    store = _store(tmp_path)
    for listing in ('1111111111', '2222222222', '3333333333', '4444444444'):
        _assessment(store, listing)
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        for listing in ('1111111111', '2222222222', '3333333333', '4444444444'):
            con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_listing', 'sahibinden', 'tool_observation', 1.0, '{}',
                         json.dumps({'listing_id': listing, 'photos': [f'https://i0.shbdn.com/{listing}/{n}.jpg' for n in range(8)]}), uuid.uuid4().hex, '{}'))
    con.close()
    plan = {l: {'next_action': {'action': 'human_review', 'why': 'x'}, 'facts': {}, 'decision': {'state': s}}
            for l, s in (('1111111111', 'conditional'), ('2222222222', 'eligible'), ('3333333333', 'excluded'), ('4444444444', 'research_required'))}
    result = work_queue(store, {}, mode='local', status=lambda listing: plan[listing])
    group = next(g for g in result['groups'] if g['action'] == 'listing.photos')
    assert sorted(i['listing_id'] for i in group['batch']) == ['1111111111', '2222222222']
    from verda.agency.photos import MAX_PHOTOS
    assert MAX_PHOTOS == 5


def test_a_listing_whose_last_send_found_our_message_last_is_read_first_not_offered_again(tmp_path, monkeypatch):
    import sqlite3, uuid
    from verda.agency import message
    store = _store(tmp_path)
    _assessment(store, '5555555555')
    con = sqlite3.connect(store.path)
    with con:
        message.ensure(con)
        con.execute("INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,reason,created_at,updated_at) VALUES('o','5555555555','local','[]','t','h','t','f','prepared','waiting_for_reply',10,20)")
    con.close()
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': True, 'fields': ['natural_sit'], 'dry_run': False})
    result = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert [g['action'] for g in result['groups']] == ['sahibinden.read_thread'] and result['groups'][0]['batch'][0]['why'] == 'conversation_exists'
    sqlite = sqlite3.connect(store.path); sqlite.execute('PRAGMA foreign_keys=OFF')
    with sqlite:
        sqlite.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', '5555555555', 'local', 'sahibinden.read_thread', 'sahibinden', 'tool_observation', 30.0, '{}',
                        json.dumps({'messages': []}), uuid.uuid4().hex, '{}'))
    sqlite.close()
    again = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert [g['action'] for g in again['groups']] == ['sahibinden.send_message']                         # read since: the normal gates decide again


def test_photos_that_cannot_get_better_are_not_offered_every_sweep(tmp_path):
    import sqlite3
    store = _store(tmp_path)
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    def call(result, at):
        with con:
            con.execute("INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,result,started_at,finished_at,operation_key,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(at), 't', 'listing.photos', 'sahibinden', 'read', 'complete', '{"listing_id":"9"}', json.dumps({'output': result}), at, at, str(at), 1))
    from verda.agency.queue import _photos_exhausted
    with store.transaction() as c:
        assert _photos_exhausted(c, '9', 1000.0) is False                                              # never saved: offer it
    call({'saved': 1, 'already': 4, 'photos_listed': 5, 'failed': []}, 900.0)                          # two of the five are the same picture
    with store.transaction() as c:
        assert _photos_exhausted(c, '9', 1000.0) is True
        assert _photos_exhausted(c, '9', 900.0 + 8 * 86400) is False                                   # a week later it may look again
    call({'saved': 0, 'already': 2, 'photos_listed': 5, 'failed': [{'position': 3, 'error': 'not_an_image'}]}, 950.0)
    with store.transaction() as c:
        assert _photos_exhausted(c, '9', 1000.0) is True                                               # nothing new and the rest fails: stop
    con.close()


def test_a_send_that_found_the_listing_changed_reads_it_again_instead_of_retrying(tmp_path, monkeypatch):
    import sqlite3, uuid
    from verda.agency import message
    store = _store(tmp_path)
    _assessment(store, '6666666666')
    con = sqlite3.connect(store.path)
    with con:
        message.ensure(con)
        con.execute("INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,reason,created_at,updated_at) VALUES('o','6666666666','local','[]','t','h','t','f','prepared','listing_changed',10,20)")
    con.close()
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': True, 'fields': ['natural_sit'], 'dry_run': False})
    first = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert [g['action'] for g in first['groups']] == ['sahibinden.read_listing'] and first['groups'][0]['batch'][0]['why'] == 'listing_changed'
    sqlite = sqlite3.connect(store.path); sqlite.execute('PRAGMA foreign_keys=OFF')
    with sqlite:
        sqlite.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', '6666666666', 'local', 'sahibinden.read_listing', 'sahibinden', 'tool_observation', 30.0, '{}',
                        json.dumps({'listing_id': '6666666666'}), uuid.uuid4().hex, '{}'))
    sqlite.close()
    again = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert [g['action'] for g in again['groups']] == ['sahibinden.send_message']


def _call(store, tool, listing, state, output, at):
    import sqlite3, uuid
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute("INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,result,started_at,finished_at,operation_key,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, 't', tool, 'sahibinden', 'read', state, json.dumps({'listing_id': listing}), json.dumps({'output': output, 'error': None if state == 'complete' else 'read_failed'}),
                     at, at, uuid.uuid4().hex, 1))
    con.close()


def test_a_changed_listing_whose_page_cannot_be_read_again_is_not_sent_to_again(tmp_path, monkeypatch):
    # 1340799649, 13 Oct 2026: the re-read failed, the queue fell back to sending, and every send stopped on the old digest: 32 times in a day
    import sqlite3
    from verda.agency import message
    store = _store(tmp_path)
    _assessment(store, '6666666666')
    con = sqlite3.connect(store.path)
    with con:
        message.ensure(con)
        con.execute("INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,reason,created_at,updated_at) VALUES('o','6666666666','local','[]','t','h','t','f','prepared','listing_changed',10,20)")
    con.close()
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': True, 'fields': ['natural_sit'], 'dry_run': False})
    for at in (500.0, 600.0, 700.0):
        _call(store, 'sahibinden.read_listing', '6666666666', 'failed', None, at)
    result = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert result['groups'] == [] and result['skipped_ids']['listing_changed_unreadable'] == ['6666666666']


def test_sends_that_keep_stopping_before_the_click_count_as_stuck(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _assessment(store, '7777777777')
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': True, 'fields': ['natural_sit'], 'dry_run': False})
    assert [g['action'] for g in work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)['groups']] == ['sahibinden.send_message']
    for at in (500.0, 600.0, 700.0):
        _call(store, 'sahibinden.send_message', '7777777777', 'complete', {'status': 'not_sent', 'reason': 'message_textbox_missing'}, at)
    assert work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)['groups'] == []


def test_a_conversation_that_went_missing_is_read_again_before_any_question(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _assessment(store, '8888888888')
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': False, 'reason': 'conversation_unconfirmed', 'message': 'x'})
    result = work_queue(store, {}, mode='local', status=lambda listing: ask, now=1000.0)
    assert [g['action'] for g in result['groups']] == ['sahibinden.read_thread'] and result['groups'][0]['batch'][0]['why'] == 'conversation_missing'
