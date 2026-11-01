"""Fixes from the second-opinion code review of 1 Nov 2026. Every case here is synthetic."""
import json
import sqlite3
import time

import pytest

from test_publish import SqlStore
from verda.agency import corridor, geo, login, opportunity, score, watchdog
from verda.agency.registry import AgencyConfig
from verda.agency.research import legacy_excluded_ids
from verda.agency.store import AgencyStore
from verda.facts.extract import EXTRACTORS, extract
from verda.facts.service import main_lookups
from verda.facts.text import fold, is_hedged
from verda.publish import feedback, projection


# ---- the neighbour check reaches the facts layer ------------------------------------------------------------------------------
def test_a_numbered_neighbour_that_is_a_road_parcel_becomes_a_conditional_access_fact():
    assert 'tkgm.neighbors' in EXTRACTORS                       # without this the observation was recorded and never read
    got = extract('tkgm.neighbors', 'o1', 'local', {'verdict': 'road_neighbor', 'road_neighbors': ['1/3']}, 'L1')
    assert [(p.field, p.value) for p in got] == [('access', 'corridor_conditional')]
    assert extract('tkgm.neighbors', 'o1', 'local', {'verdict': 'no_road_neighbor', 'road_neighbors': []}, 'L1') == []     # "no road neighbour" is no fact


# ---- seller wording -----------------------------------------------------------------------------------------------------------
def test_a_suggestion_to_check_or_ask_elsewhere_is_a_hedge_not_a_statement():
    for said in ('Belediyeden kontrol edin, sit yok.', 'Tapuya sorarsanız sorun yok.', 'Sorabilirsiniz, sit yok.'):
        assert is_hedged(fold(said)), said
    assert not is_hedged(fold('Teyit ettim, sit yok.'))             # a confirmation stays one


# ---- Turkish capitals ----------------------------------------------------------------------------------------------------------
def test_tree_counts_in_capitals_are_read_like_lowercase_ones_and_never_crash_the_ranking():
    assert opportunity.tree_density('300 ADET ZEYTİN AĞACI', 10000) == opportunity.tree_density('300 adet zeytin ağacı', 10000)
    assert opportunity.tree_density('2.500 ADET KAYISI', 10000) == opportunity.tree_density('2.500 adet kayısı', 10000)
    assert opportunity.tree_density('1.500 adet İNCİR', 10000) is not None
    assert opportunity.tree_density('15.20 adet zeytin', 10000) is None         # not a thousands separator: no count, not 1520 trees


def test_the_land_type_and_road_words_ignore_the_dot_on_a_capital_i():
    assert score.market_type(None, 'ZEYTİNLİK SATILIK') == 'Zeytinlik' and score.market_type('ZEYTİNLİK') == 'Zeytinlik'
    assert score.market_type('—', 'İMARLI ARSA') == 'Arsa / imarlı' and score.market_type('Tarla') == 'Tarla'
    assert corridor.is_road_type('TARLA YOLU') and corridor.is_road_type('PATİKA') and corridor.is_road_type('GEÇİT') and not corridor.is_road_type('ZEYTİNLİK')


def test_a_verification_page_in_capitals_still_stops_the_sign_in():
    assert login._stopped('LÜTFEN ROBOT OLMADIĞINIZI DOĞRULAYIN', login.SAHIBINDEN_STOPS)
    assert login._stopped('GÜVENLİK KODU GİRİN', login.SAHIBINDEN_STOPS)
    assert not login._stopped('Hoş geldiniz', login.GOOGLE_STOPS + login.SAHIBINDEN_STOPS)


# ---- the listing's own parcel in a several-deed sale -----------------------------------------------------------------------------
def _ring(x):
    return [[x, 36.1], [x + .1, 36.1], [x + .1, 36.2], [x, 36.1]]


def _lookup(store, listing, at, key, x, **extra):
    payload = {'found': True, 'parcel_key': key, 'geometry': {'type': 'Polygon', 'ring': _ring(x), 'centroid': [x + .03, 36.13]}, **extra}
    store.con.execute("INSERT INTO observations(listing_ref, mode, tool, payload, captured_at) VALUES(?, 'local', 'tkgm.lookup', ?, ?)", (listing, json.dumps(payload), at))


def test_an_extra_deed_looked_up_last_does_not_replace_the_main_parcel_outline_or_route_origin():
    store = SqlStore()
    _lookup(store, 'L1', 1.0, '402/5', 27.1)
    _lookup(store, 'L1', 2.0, '402/7', 27.5, part=True)                       # another deed of the same sale, looked up later
    assert projection.parcel_shape(store, 'L1')['centroid'][0] == pytest.approx(27.13)       # the main parcel
    assert projection.parcel_shape(store, 'L1', parcels=['402/5', '402/7'])['centroid'][0] == pytest.approx(27.13)
    with store.transaction() as con:
        assert main_lookups(con, 'L1', 'local')[0]['parcel_key'] == '402/5'
    seen = {}
    def fake_route(origin, destination, fetch, sleep):
        seen.update(origin)
        return {'engines': []}
    real, geo.route = geo.route, fake_route
    try:
        geo.listing_route(store, {'listing_id': 'L1'}, mode='local')
    finally:
        geo.route = real
    assert seen['parcel_key'] == '402/5'


def test_the_outline_of_a_parcel_the_user_has_since_corrected_is_not_drawn():
    store = SqlStore()
    _lookup(store, 'L1', 1.0, '9/9', 27.1)                                    # the wrong number the listing gave
    assert projection.parcel_shape(store, 'L1', parcels=['9/10']) is None     # the user says it is 9/10, which has no outline yet
    _lookup(store, 'L1', 2.0, '9/10', 27.3)
    assert projection.parcel_shape(store, 'L1', parcels=['9/10'])['centroid'][0] == pytest.approx(27.33)


# ---- a listing without a fair price still pays the rule engine's cuts ----------------------------------------------------------------------
def test_a_listing_without_a_fair_price_still_loses_the_points_the_rules_take_and_the_enclosed_road_penalty():
    row = {'type': 'Zeytinlik', 'area_m2': 5000, 'price_tl': 3_000_000, 'route_minutes_max': 30, 'penalties': {}}
    plain = opportunity.listing_view(row, None, 0.5, '', {'value': 'direct'}, True)
    cut = opportunity.listing_view({**row, 'penalties': {'natural_sit_second_degree': 30}}, None, 0.5, '', {'value': 'direct'}, True)
    assert plain['label'] == cut['label'] == 'Belirsiz'
    assert cut['score'] == max(0, plain['score'] - 30) and any('30' in c for c in cut['caps'])
    shut = opportunity.listing_view(row, None, 0.5, '', {'value': 'none'}, True, enclosed=True)
    assert shut['road'] == opportunity.ENCLOSED and shut['score'] < plain['score'] and any('40' in c for c in shut['caps'])


# ---- the old system's eliminations are not "active" ---------------------------------------------------------------------------------------
def _archive(path, items):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE legacy_snapshots(id TEXT, imported_at TEXT)')
    con.execute('CREATE TABLE legacy_rows(snapshot_id TEXT, table_name TEXT, row_key TEXT, subject_id TEXT, payload TEXT)')
    con.execute("INSERT INTO legacy_snapshots VALUES('S1', '2026-10-04')")
    for listing, lifecycle, reason in items:
        con.execute("INSERT INTO legacy_rows VALUES('S1', 'items', ?, ?, ?)", (listing, listing, json.dumps({'lifecycle': lifecycle, 'legacy_json': json.dumps({'exclusion_reason': reason})})))
    con.commit()
    con.close()


def _assess(store, listing, state, price=None):
    with store.transaction() as con:
        con.execute('INSERT INTO assessments(id,listing_ref,mode,policy_version,input_fingerprint,facts,basis,conflicts,state,reasons,conditions,created_at) '
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (f'a-{listing}', listing, 'local', 'p1', 'f', json.dumps({'price_tl': price}), '{}', '[]', state, '[]', '[]', time.time()))


def test_listings_the_old_system_eliminated_do_not_count_as_active_or_unseen(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    _archive(tmp_path / 'old.sqlite', [('L1', 'excluded', 'shared_title_excluded'), ('L2', 'excluded', 'price_over_15m'), ('L3', 'excluded', 'price_over_15m'), ('L4', 'verifying', 'None')])
    store.archive_path = tmp_path / 'old.sqlite'
    for listing, price in (('L1', 4e6), ('L2', 20e6), ('L3', 9e6), ('L4', 5e6), ('L5', 6e6)):
        _assess(store, listing, 'research_required', price)
    # L2 is still over the limit; L3's price fell under it, so only the price exclusion that lapsed stops counting
    assert legacy_excluded_ids(store) == {'L1', 'L2'}
    finding = watchdog.stale_listings(store, 'local', time.time() + 30 * 86400)[0]
    assert set(finding['listings']) == {'L3', 'L4', 'L5'} and finding['count'] == 3


# ---- dashboard decisions and corrections can be taken back ----------------------------------------------------------------------------------
def _store(tmp_path):
    store = AgencyStore(tmp_path / 'b.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


class Fs:
    def __init__(self, docs):
        self.docs = docs

    def list_docs(self, collection):
        return list(self.docs.items())


def _user_excluded(store, listing='L1'):
    with store.transaction() as con:
        return store.facts.status(con, listing, 'local')['decision']['state'] == 'excluded'


def test_removing_the_decision_on_the_dashboard_takes_back_its_own_elimination_but_a_note_alone_decides_nothing(tmp_path):
    store = _store(tmp_path)
    feedback.import_feedback(store, Fs({'L1': {'decision': 'excluded', 'updatedAt': 't1'}}))
    assert _user_excluded(store)
    feedback.import_feedback(store, Fs({'L1': {'decision': '', 'note': 'x', 'updatedAt': 't2'}}))
    assert not _user_excluded(store)                                           # "Kararı kaldır"
    # a note saved without any decision carries '' too: it must not touch an exclusion that did not come from the dashboard
    with store.transaction() as con:
        store.facts.decide(con, 'L2', 'local', 'user_excluded', True, 'told in the terminal', time.time())
    feedback.import_feedback(store, Fs({'L2': {'decision': '', 'note': 'only a note', 'updatedAt': 't3'}}))
    assert _user_excluded(store, 'L2')


def test_a_correction_the_dashboard_deletes_is_taken_back_and_the_system_decides_again(tmp_path):
    store = _store(tmp_path)
    fact = {'natural': {'value': '1', 'basis': 'official', 'at': 't1'}}
    first = feedback.import_feedback(store, Fs({'L1': {'userFacts': fact}}))
    assert first['user_facts_applied'] == 1
    with store.transaction() as con:
        got = store.facts.status(con, 'L1', 'local')['facts']['natural_sit']
    assert got['value'] == 'first_degree' and got['grade'] == 'kullanici'
    # "Sistemdeki gibi bırak": the key is gone from the document
    second = feedback.import_feedback(store, Fs({'L1': {'userFacts': {}}}))
    assert second['user_facts_withdrawn'] == 1
    with store.transaction() as con:
        assert 'natural_sit' not in store.facts.status(con, 'L1', 'local')['facts']
        rows = con.execute("SELECT status, proposer FROM claims WHERE field='natural_sit' ORDER BY seq").fetchall()
    assert [(r['status'], r['proposer']) for r in rows] == [('accepted', 'user'), ('withdrawn', 'user')]         # the log keeps both
    # nothing more to take back, and the same deletion is not repeated
    assert feedback.import_feedback(store, Fs({'L1': {'userFacts': {}}}))['user_facts_withdrawn'] == 0
    # correcting again later is applied again
    again = feedback.import_feedback(store, Fs({'L1': {'userFacts': {'natural': {'value': 'none', 'basis': 'site', 'at': 't3'}}}}))
    assert again['user_facts_applied'] == 1
    with store.transaction() as con:
        assert store.facts.status(con, 'L1', 'local')['facts']['natural_sit']['value'] == 'none'


def test_taking_back_the_newest_correction_does_not_bring_back_an_older_one_of_the_same_field(tmp_path):
    store = _store(tmp_path)
    for at, value in (('t1', '2'), ('t2', '3')):
        feedback.import_feedback(store, Fs({'L1': {'userFacts': {'natural': {'value': value, 'basis': 'site', 'at': at}}}}))
    feedback.import_feedback(store, Fs({'L1': {}}))
    with store.transaction() as con:
        assert 'natural_sit' not in store.facts.status(con, 'L1', 'local')['facts']


# ---- a lasting problem is told on this Mac ----------------------------------------------------------------------------------------------------
def test_a_problem_that_lasts_is_told_once_then_again_only_after_hours_and_a_blip_says_nothing(tmp_path, monkeypatch):
    from verda.publish import notify
    monkeypatch.delenv('VERDA_NOTIFY')
    shown = []
    class Run:
        def __call__(self, command, **kwargs):
            shown.append(command[-2])
            return type('R', (), {'returncode': 0})()
    store = type('S', (), {'path': tmp_path / 'a.sqlite'})()
    bad = lambda text: [{'level': 'bad', 'text': text}, {'level': 'ok', 'text': 'Panel: çalışıyor'}, {'level': 'warn', 'text': 'Nöbetçi · 60 ilan'}]
    run, t0 = Run(), 1_000_000.0
    assert notify.notify(store, bad('Kaynak engelli: tkgm · tkgm_http_403 (açılalı 1 dk)'), t0, run=run) == []                      # first round: maybe a blip
    assert notify.notify(store, bad('Kaynak engelli: tkgm · tkgm_http_403 (açılalı 11 dk)'), t0 + 660, run=run) == [
        'Kaynak engelli: tkgm · tkgm_http_403 (açılalı 11 dk)']                                                                      # lasted ten minutes: told, the age changing is the same problem
    assert notify.notify(store, bad('Kaynak engelli: tkgm · tkgm_http_403 (açılalı 20 dk)'), t0 + 1200, run=run) == []             # not again at once
    assert notify.notify(store, [], t0 + 1500, run=run) == []                                                                       # it cleared
    assert notify.notify(store, bad('Kaynak engelli: tkgm · tkgm_http_403 (açılalı 1 dk)'), t0 + 1800, run=run) == []             # coming back starts a new count
    assert notify.notify(store, bad('Kaynak engelli: tkgm · tkgm_http_403 (açılalı 30 dk)'), t0 + 1800 + 7 * 3600, run=run) != []    # a long one is repeated
    assert len(shown) == 2


def test_notifications_can_be_switched_off(tmp_path, monkeypatch):
    from verda.publish import notify
    monkeypatch.setenv('VERDA_NOTIFY', 'off')
    store = type('S', (), {'path': tmp_path / 'a.sqlite'})()
    assert notify.notify(store, [{'level': 'bad', 'text': 'x'}], 5e9, run=lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not run'))) == []


# ---- a long inbox card is a click target ----------------------------------------------------------------------------------------------------------
def test_an_unread_inbox_card_with_a_long_preview_is_still_offered_as_a_click_target():
    import re
    from verda.agency import jev
    limit = int(re.search(r't\.length>(\d+)', jev.CLICKABLES).group(1))
    # the two unread cards of 1 Nov 2026 were 395 and 351 characters; with a limit of 300 neither could ever be opened
    assert limit >= 1000


# ---- the ministry's sit map asks for a login ---------------------------------------------------------------------------------------------------------
def test_the_sit_map_answering_401_stops_the_source_instead_of_failing_every_listing(monkeypatch):
    import io
    import urllib.error
    from verda.agency import says
    from verda.agency.registry import ResourceBlocked
    def answer(code):
        def urlopen(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, code, 'x', {}, io.BytesIO(b''))
        return urlopen
    for code in (401, 403, 429):
        monkeypatch.setattr(says.urllib.request, 'urlopen', answer(code))
        with pytest.raises(ResourceBlocked, match=f'says_http_{code}'):
            says._get('https://example.test/layer')
    monkeypatch.setattr(says.urllib.request, 'urlopen', answer(500))
    with pytest.raises(urllib.error.HTTPError):                                  # a server error is not a block
        says._get('https://example.test/layer')


# ---- a conversation that went missing is read again without a question waiting -------------------------------------------------------------------------
def _sql(store, query, *args):
    con = sqlite3.connect(store.path)
    con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute(query, args)
    con.close()


def _thread_read(store, listing, found, at):
    import uuid
    _sql(store, 'INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_thread',
         'sahibinden', 'tool_observation', at, '{}', json.dumps({'found': found, 'messages': []}), uuid.uuid4().hex, '{}')


def _call(store, tool, arguments, state, at, listing_state='x'):
    import uuid
    _sql(store, 'INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,result,started_at,finished_at,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,1)',
         uuid.uuid4().hex, 'T', tool, 'r', 'read', state, json.dumps(arguments), None, at, at + 5)


def test_a_conversation_read_once_and_then_not_found_is_handed_out_again_once_a_day_even_with_sending_off(tmp_path):
    from test_queue import _assessment, _store as queue_store
    from verda.agency.queue import work_queue
    store = queue_store(tmp_path)
    for listing in ('1000000101', '1000000102', '1000000103'):
        _assessment(store, listing)
        _thread_read(store, listing, True, 100.0)
        _thread_read(store, listing, False, 200.0)
    _thread_read(store, '1000000104', False, 200.0)                     # never found at all: not "lost"
    _assessment(store, '1000000104')
    quiet = {'next_action': {'action': 'none', 'why': 'waiting_for_seller'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    shut = {**quiet, 'decision': {'state': 'excluded'}}
    status = lambda listing: shut if listing == '1000000103' else quiet
    got = work_queue(store, {}, mode='local', status=status, now=10_000.0)
    reads = [g for g in got['groups'] if g['action'] == 'sahibinden.read_thread']
    assert reads and {i['listing_id'] for i in reads[0]['batch']} == {'1000000101', '1000000102'} and {i['why'] for i in reads[0]['batch']} == {'conversation_lost'}
    _call(store, 'sahibinden.read_thread', {'listing_id': '1000000101'}, 'failed', 9_900.0)           # tried today: not again until tomorrow
    again = work_queue(store, {}, mode='local', status=status, now=10_000.0)
    assert [i['listing_id'] for g in again['groups'] for i in g['batch']] == ['1000000102']
    later = work_queue(store, {}, mode='local', status=status, now=10_000.0 + 90_000)
    assert {i['listing_id'] for g in later['groups'] for i in g['batch']} == {'1000000101', '1000000102'}


def test_a_lost_conversation_is_not_listed_twice_when_a_question_is_also_waiting(tmp_path, monkeypatch):
    from test_queue import _assessment, _store as queue_store
    from verda.agency.queue import work_queue
    store = queue_store(tmp_path)
    _assessment(store, '1000000105')
    _thread_read(store, '1000000105', True, 100.0)
    _thread_read(store, '1000000105', False, 200.0)
    ask = {'next_action': {'action': 'ask:sit', 'why': 'sit_status_unknown'}, 'facts': {}, 'decision': {'state': 'research_required'}}
    monkeypatch.setattr('verda.agency.message.preview', lambda *a, **k: {'ok': False, 'reason': 'conversation_unconfirmed', 'message': 'x'})
    monkeypatch.setattr('verda.agency.message.mode_of', lambda store: 'live')
    got = work_queue(store, {}, mode='local', status=lambda listing: ask, now=10_000.0)
    assert [(g['action'], g['waiting']) for g in got['groups']] == [('sahibinden.read_thread', 1)]


# ---- set-aside work stays visible ---------------------------------------------------------------------------------------------------------------------
def test_work_set_aside_after_repeated_failures_is_a_finding_until_something_succeeds(tmp_path):
    from test_queue import _store as queue_store
    store = queue_store(tmp_path)
    now = 1_900_000_000.0
    assert watchdog.stuck_work(store, 'local', now) == []
    for k in range(3):
        _call(store, 'sit.says', {'listing_id': '1000000106'}, 'failed', now - 3600 * (k + 1))
    _call(store, 'sit.says', {'listing_id': '1000000107'}, 'failed', now - 3600)               # one failure is no pattern
    row = {'row': {'title': 'Arsa ilanı', 'counterpart': 'Satıcı A', 'last_at_text': '21.10.2026 18:30'}}
    for k in range(2):
        _call(store, 'sahibinden.read_thread', row, 'failed', now - 7200 * (k + 1))
    found = watchdog.stuck_work(store, 'local', now)[0]
    assert found['rule'] == 'stuck_work' and found['listings'] == ['1000000106'] and found['count'] == 2 and 'sit.says' in found['detail']
    _call(store, 'sahibinden.read_thread', row, 'complete', now - 600)                         # the row opened afterwards
    _call(store, 'sit.says', {'listing_id': '1000000106'}, 'complete', now - 600)
    assert watchdog.stuck_work(store, 'local', now) == []
