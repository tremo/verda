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
    oid = uuid.uuid4().hex
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (oid, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_thread', 'sahibinden', 'tool_observation', at, '{}',
                     json.dumps({'listing_id': listing, 'found': found, 'messages': messages}), uuid.uuid4().hex, '{}'))
    con.close()
    return oid


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


def test_a_road_hint_from_the_geometry_does_not_hide_what_the_seller_said_about_the_road(tmp_path, monkeypatch):
    # 13 Nov 2026: an answer that settled the sit question and added that there is a road gave both sit facts and no road fact: the corridor hint already filled the field
    store = _store(tmp_path)
    said = [{'id': 'b', 'role': 'seller', 'text': 'Sit yoktur. Arazinin yolu vardır.'}]
    for listing in ('1', '2', '3'):
        _thread(store, listing, said)
    sit = {'natural_sit': {'value': 'none'}, 'archaeological_sit': {'value': False}}
    status = {'1': {'facts': {**sit, 'access': {'value': 'corridor_conditional', 'source_kind': 'calc'}}, 'decision': {'state': 'conditional'}},     # only the hint
              '2': {'facts': {**sit, 'access': {'value': 'direct', 'source_kind': 'thread'}}, 'decision': {'state': 'eligible'}},                    # his word is already recorded
              '3': {'facts': {**sit, 'access': {'value': 'existing_easement', 'source_kind': 'user'}}, 'decision': {'state': 'conditional'}}}        # the owner decided
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: status[listing])
    out = replies.pending(store, {'limit': 8}, mode='local', now=100000.0)
    assert [(i['listing_id'], i['missing_fields']) for i in out['items']] == [('1', ['access'])] and 'flagged' not in out['items'][0]


def test_a_field_a_reader_marked_unclear_is_read_once_more_with_the_note_and_then_left_to_the_owner(tmp_path, monkeypatch):
    # a sentence about road frontage was marked unclear by a reader and never looked at again, although the owner's rule calls it a road
    from verda.facts.flags import raise_flag
    store = _store(tmp_path)
    oid = _thread(store, '1', [{'id': 'b', 'role': 'seller', 'text': 'Arazinin yola 40 metre cephesi var, sit yok'}])
    with store.transaction() as con:
        assert raise_flag(con, listing_ref='1', mode='local', field='access', observation_id=oid, source_ref='message:b', quote='yola 40 metre cephesi',
                          note='Yolun kadastral olduğu yazmıyor.', now=50.0)['status'] == 'flagged'
    sit = {'natural_sit': {'value': 'none'}, 'archaeological_sit': {'value': False}}
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: {'facts': dict(sit), 'decision': {'state': 'research_required'}})
    assert replies.has_pending(store, now=100000.0) is True
    first = replies.pending(store, {}, mode='local', now=100000.0)['items']
    assert [(i['listing_id'], i['missing_fields']) for i in first] == [('1', ['access'])]
    assert first[0]['flagged'] == [{'field': 'access', 'note': 'Yolun kadastral olduğu yazmıyor.', 'quote': 'yola 40 metre cephesi'}]
    # the reader left it unclear again: the note stands and the field is not handed over a third time, today or later
    assert replies.has_pending(store, now=100100.0) is False and replies.pending(store, {}, mode='local', now=100000.0 + 5 * 86400)['advice'] == 'nothing_to_do'
    assert replies.waiting(store, now=100000.0 + 5 * 86400) == []                                                 # nothing for the watchdog either
    # a field that was never marked still comes back the next day, with the reviewed one left out
    monkeypatch.setattr('verda.agency.research.fact_status', lambda s, listing, mode: {'facts': {}, 'decision': {'state': 'research_required'}})
    later = replies.pending(store, {}, mode='local', now=100000.0 + 6 * 86400)['items']
    assert [(i['listing_id'], i['missing_fields']) for i in later] == [('1', ['natural_sit', 'archaeological_sit'])] and 'flagged' not in later[0]


def test_no_road_and_no_right_of_way_is_not_concluded_where_nobody_spoke_of_a_right_of_way(tmp_path):
    # 13 Nov 2026: asked only about cadastral frontage, a seller answered that there is none but a stream track; a reader made it "none" and a good candidate was eliminated
    from verda.agency.research import fact_propose
    store = _store(tmp_path)
    asked = {'id': 'a', 'role': 'user', 'text': 'Merhaba, taşınmazın doğrudan kadastral yol cephesi var mı?'}
    stream = _thread(store, '1', [asked, {'id': 'b', 'role': 'seller', 'text': 'Kadastral yola cephe yok, dere yatağından girilir.'}])
    args = dict(listing_id='1', observation_id=stream, field='access', value='none', source_ref='message:b', quote='Kadastral yola cephe yok')
    refused = fact_propose(store, args, mode='local', task_id='', proposer='llm:manager')
    assert (refused['status'], refused['reason']) == ('rejected', 'easement_not_addressed') and 'listing.flag' in refused['hint']
    with store.transaction() as con:
        assert con.execute("SELECT count(*) FROM claims WHERE field='access'").fetchone()[0] == 0
    # the seller denies both: the conclusion is his own words
    both = _thread(store, '2', [asked, {'id': 'c', 'role': 'seller', 'text': 'Yolu yok, geçiş hakkı da yok.'}])
    said = fact_propose(store, {**args, 'listing_id': '2', 'observation_id': both, 'source_ref': 'message:c', 'quote': 'Yolu yok, geçiş hakkı da yok'}, mode='local', task_id='', proposer='llm:manager')
    assert said['status'] == 'accepted'
    # we asked about the right of way ourselves: his "no" answers it
    ours = {'id': 'd', 'role': 'user', 'text': 'Parselin kadastral yol cephesi görünmüyor; yola geçiş hakkınız (irtifak, mahkeme yolu vb.) var mı?'}
    denied = _thread(store, '3', [ours, {'id': 'e', 'role': 'seller', 'text': 'Maalesef yoktur.'}])
    answer = fact_propose(store, {**args, 'listing_id': '3', 'observation_id': denied, 'source_ref': 'message:e', 'quote': 'Maalesef yoktur'}, mode='local', task_id='', proposer='llm:manager')
    assert answer.get('reason') != 'easement_not_addressed'
    # nothing else is held back: a road he names, and the owner's own decision
    road = fact_propose(store, {**args, 'value': 'direct', 'quote': 'dere yatağından girilir'}, mode='local', task_id='', proposer='llm:manager')
    assert road.get('reason') != 'easement_not_addressed'
    with store.transaction() as con:
        assert store.facts.decide(con, '1', 'local', 'access', 'none', 'yerinde gördüm', 10.0)['status'] == 'accepted'
