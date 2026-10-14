import json
import sqlite3
import uuid

from verda.agency import replies


def _store(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _thread(store, listing, messages, at=1.0, found=True):
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_thread', 'sahibinden', 'tool_observation', at, '{}',
                     json.dumps({'found': found, 'messages': messages}), uuid.uuid4().hex, '{}'))
    con.close()


def test_listings_with_unread_seller_answers_are_handed_over_once_a_day(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _thread(store, '1', [{'id': 'a', 'role': 'user', 'text': 'Sit var mı?'}, {'id': 'b', 'role': 'seller', 'text': 'Sit yoktur.'}])
    _thread(store, '2', [{'id': 'c', 'role': 'user', 'text': 'Sit var mı?'}])                             # no answer yet
    _thread(store, '3', [{'id': 'd', 'role': 'seller', 'text': 'Sit yok.'}])                               # already has its facts
    _thread(store, '4', [{'id': 'e', 'role': 'seller', 'text': 'Sit yok.'}])                               # eliminated
    status = {'1': {'facts': {}, 'decision': {'state': 'conditional'}}, '2': {'facts': {}, 'decision': {'state': 'conditional'}},
              '3': {'facts': {'natural_sit': {}, 'archaeological_sit': {}, 'access': {}}, 'decision': {'state': 'conditional'}}, '4': {'facts': {}, 'decision': {'state': 'excluded'}}}
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: status[listing])
    first = replies.pending(store, {}, mode='local', now=100000.0)
    assert [i['listing_id'] for i in first['items']] == ['1'] and first['items'][0]['seller_messages'] == [{'source_ref': 'message:b', 'text': 'Sit yoktur.'}]
    assert first['items'][0]['missing_fields'] == ['natural_sit', 'archaeological_sit']                 # the seller said nothing about a road
    assert replies.pending(store, {}, mode='local', now=100100.0)['advice'] == 'nothing_to_do'                # handed over, not again today
    assert [i['listing_id'] for i in replies.pending(store, {}, mode='local', now=100000.0 + 90000)['items']] == ['1']


def test_a_new_seller_message_brings_the_listing_back_within_the_day(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _thread(store, '1', [{'id': 'b', 'role': 'seller', 'text': 'Sit yoktur.'}])
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: {'facts': {}, 'decision': {'state': 'conditional'}})
    assert [i['listing_id'] for i in replies.pending(store, {}, mode='local', now=100000.0)['items']] == ['1']
    assert replies.has_pending(store, now=100100.0) is False                                              # handed over, nothing new
    _thread(store, '1', [{'id': 'b', 'role': 'seller', 'text': 'Sit yoktur.'}, {'id': 'f', 'role': 'seller', 'text': 'Yolu da kadastral.'}], at=2.0)
    assert replies.has_pending(store, now=100200.0) is True


def test_a_numbered_neighbour_that_is_itself_a_road_parcel_counts_as_road_frontage(tmp_path):
    from verda.agency import corridor
    store = _store(tmp_path)
    probes = [{'edge': 0, 'result': 'neighbor', 'parcel': '1/2', 'at': [36.7, 27.5]}, {'edge': 1, 'result': 'neighbor', 'parcel': '1/3', 'at': [36.8, 27.6]}]
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, 'T', 'tkgm', 't', 'L1', 'local', 'tkgm.corridor', 'tkgm', 'tool_observation', 1.0, '{}',
                     json.dumps({'verdict': 'all_edges_numbered_neighbors', 'probes': probes}), uuid.uuid4().hex, '{}'))
    con.close()
    types = {'1/2': 'Tarla', '1/3': 'Tarla Yolu'}
    def fetch(url):
        lat = float(url.rstrip('/').split('/')[-2])
        parcel = '1/2' if lat < 36.75 else '1/3'
        ada, parsel = parcel.split('/')
        return {'properties': {'adaNo': ada, 'parselNo': parsel, 'mahalleId': 5, 'nitelik': types[parcel]}}
    result = corridor.neighbor_types(store, {'listing_id': 'L1'}, mode='local', fetch=fetch, sleep=lambda s: None)
    assert result['verdict'] == 'road_neighbor' and result['road_neighbors'] == ['1/3']
    types['1/3'] = 'Tarla'
    assert corridor.neighbor_types(store, {'listing_id': 'L1'}, mode='local', fetch=fetch, sleep=lambda s: None)['verdict'] == 'no_road_neighbor'
    assert corridor.is_road_type('Yol') and corridor.is_road_type('Tarla Yolu') and not corridor.is_road_type('Zeytinlik')


def test_a_later_read_that_missed_the_conversation_does_not_hide_the_sellers_answer(tmp_path, monkeypatch):
    # an archaeological-sit answer waited a week; fifteen others were hidden by a "not found" read on 14 Oct 2026
    store = _store(tmp_path)
    _thread(store, '1', [{'id': 'a', 'role': 'user', 'text': 'Sit var mı?'}, {'id': 'b', 'role': 'seller', 'text': 'resmi yol yok. Sit a2 ve d3 var'}], at=1.0)
    _thread(store, '1', [], at=2.0, found=False)
    _thread(store, '2', [{'id': 'c', 'role': 'seller', 'text': 'Merhaba, ada yok parsel 88'}], at=1.0)
    _thread(store, '3', [{'id': 'd', 'role': 'seller', 'text': 'Kadastral yola cepheli'}], at=1.0)          # "ada" inside "kadastral" is not a parcel answer
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: {'facts': {}, 'decision': {'state': 'research_required'}})
    items = {i['listing_id']: i['missing_fields'] for i in replies.pending(store, {'limit': 8}, mode='local', now=100000.0)['items']}
    assert items == {'1': ['natural_sit', 'archaeological_sit', 'access'], '2': ['parcel_key'], '3': ['access']}
