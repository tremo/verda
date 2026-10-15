import json
import sqlite3
import uuid

import pytest

from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore
from verda.facts.flags import close_flag, open_flags, raise_flag


def _store(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _thread(store, listing='1000000001'):
    oid = uuid.uuid4().hex
    payload = {'listing_id': listing, 'found': True, 'messages': [
        {'id': 'aa0000000001', 'role': 'user', 'text': 'Kadastral yol cephesi var mı?'},
        {'id': 'bb0000000002', 'role': 'seller', 'text': 'merhaba, yolu var sit durumu yoktur'}]}
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (oid, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_thread', 'sahibinden', 'tool_observation', 1.0, '{}',
                     json.dumps(payload), uuid.uuid4().hex, '{}'))
    con.close()
    return oid


def test_a_flag_needs_the_sellers_exact_words_from_this_listings_message(tmp_path):
    store = _store(tmp_path)
    oid = _thread(store)
    good = dict(listing_ref='1000000001', mode='local', field='access', observation_id=oid, source_ref='message:bb0000000002', quote='yolu var', note='Hangi yol olduğu belli değil.', now=10.0)
    with store.transaction() as con:
        assert raise_flag(con, **{**good, 'quote': 'asfalt yolu var'})['reason'] == 'quote_not_in_source'                          # invented words
        assert raise_flag(con, **{**good, 'source_ref': 'message:aa0000000001', 'quote': 'Kadastral yol cephesi'})['reason'] == 'quote_not_in_source'   # our own message, not the seller's
        assert raise_flag(con, **{**good, 'listing_ref': '1999999999'})['reason'] == 'observation_not_of_this_listing'
        assert raise_flag(con, **{**good, 'field': 'price_tl'})['reason'] == 'field_not_clarifiable'
        assert raise_flag(con, **{**good, 'note': ' '})['reason'] == 'note_and_quote_required'
        first = raise_flag(con, **good)
        assert first['status'] == 'flagged' and raise_flag(con, **good)['status'] == 'exists'                                       # one note per cause
        assert [f['field'] for f in open_flags(con, '1000000001', 'local')] == ['access']


def test_a_flag_ends_when_the_user_settles_the_field_or_closes_it(tmp_path):
    store = _store(tmp_path)
    oid = _thread(store)
    with store.transaction() as con:
        a = raise_flag(con, listing_ref='1000000001', mode='local', field='access', observation_id=oid, source_ref='message:bb0000000002', quote='yolu var', note='belirsiz', now=10.0)
        b = raise_flag(con, listing_ref='1000000001', mode='local', field='natural_sit', observation_id=oid, source_ref='message:bb0000000002', quote='sit durumu yoktur', note='tür belli değil', now=10.0)
    # the user decides the access field after the note: that note is settled, the other stays
    with store.transaction() as con:
        assert store.facts.decide(con, '1000000001', 'local', 'access', 'direct', 'telefonda konuştum', 20.0)['status'] == 'accepted'
    with store.transaction() as con:
        assert [f['field'] for f in open_flags(con, '1000000001', 'local')] == ['natural_sit']
        close_flag(con, b['flag_id'], 'netleştirdim', now=30.0)
        assert open_flags(con, '1000000001', 'local') == []
        with pytest.raises(ValueError):
            close_flag(con, b['flag_id'], 'tekrar')


def test_a_bare_or_wrong_message_id_does_not_refuse_a_quote_that_is_in_the_sellers_message(tmp_path):
    # 15 Oct 2026: 32 true flags were refused because the reader wrote the id without "message:"
    store = _store(tmp_path)
    oid = _thread(store)
    base = dict(listing_ref='1000000001', mode='local', field='access', observation_id=oid, quote='yolu var', note='Hangi yol olduğu belli değil.', now=10.0)
    with store.transaction() as con:
        assert raise_flag(con, **base, source_ref='bb0000000002')['status'] == 'flagged'
        assert raise_flag(con, **base, source_ref='message:ffffffffffff')['status'] == 'exists'           # the same note, found in the seller's message
        assert raise_flag(con, **{**base, 'quote': 'Kadastral yol cephesi'}, source_ref='aa0000000001')['reason'] == 'quote_not_in_source'   # still never our own words
