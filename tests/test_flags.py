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
        {'id': 'bb0000000002', 'role': 'seller', 'text': 'merhaba, yolu var, sit durumu da yoktur'}]}
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
        b = raise_flag(con, listing_ref='1000000001', mode='local', field='natural_sit', observation_id=oid, source_ref='message:bb0000000002', quote='sit durumu da yoktur', note='tür belli değil', now=10.0)
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


def _reread(store, listing='1000000001', seller_id='cc0000000003'):
    """The same conversation read again: a new record, and the seller's message under another id."""
    oid = uuid.uuid4().hex
    payload = {'listing_id': listing, 'found': True, 'messages': [
        {'id': 'aa0000000001', 'role': 'user', 'text': 'Kadastral yol cephesi var mı?'},
        {'id': seller_id, 'role': 'seller', 'text': 'merhaba, yolu var, sit durumu da yoktur'}]}
    con = sqlite3.connect(store.path); con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (oid, uuid.uuid4().hex, 'T', 'sahibinden', 't', listing, 'local', 'sahibinden.read_thread', 'sahibinden', 'tool_observation', 2.0, '{}',
                     json.dumps(payload), uuid.uuid4().hex, '{}'))
    con.close()
    return oid


def test_one_sentence_is_one_note_however_often_the_conversation_is_read_again(tmp_path):
    # 13 Nov 2026: 61 notes on 21 candidates; one sentence had six, one per re-read (the record and the message id change, the words do not)
    store = _store(tmp_path)
    first, second = _thread(store), _reread(store)
    note = dict(listing_ref='1000000001', mode='local', field='access', note='Hangi yol olduğu belli değil.')
    with store.transaction() as con:
        a = raise_flag(con, **note, observation_id=first, source_ref='message:bb0000000002', quote='yolu var', now=10.0)
        assert a['status'] == 'flagged'
        again = raise_flag(con, **note, observation_id=second, source_ref='message:cc0000000003', quote='merhaba, yolu var, sit durumu da yoktur', now=20.0)
        assert again == {'status': 'exists', 'flag_id': a['flag_id']}                                                            # the same sentence, quoted longer
        assert raise_flag(con, **{**note, 'field': 'natural_sit'}, observation_id=second, source_ref='message:cc0000000003', quote='sit durumu da yoktur', now=20.0)['status'] == 'flagged'
        # notes written before this rule: five more rows on the same sentence
        for n in range(5):
            con.execute('INSERT INTO listing_flags(id,listing_ref,mode,field,note,observation_id,source_ref,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (f'old{n}', '1000000001', 'local', 'access', f'eski not {n}', second, 'message:cc0000000003', 'Yolu var', 30.0 + n))
        shown = open_flags(con, '1000000001', 'local')
        assert [(f['field'], f['repeats']) for f in shown] == [('access', 6), ('natural_sit', 1)]
        assert shown[0]['note'] == 'eski not 4' and shown[0]['first_at'] == 10.0 and shown[0]['created_at'] == 34.0             # the newest wording, since the first marking
        close_flag(con, shown[0]['id'], 'netleştirdim', now=40.0)                                                                # closing one closes them all
        assert [f['field'] for f in open_flags(con, '1000000001', 'local')] == ['natural_sit']
        assert con.execute("SELECT count(*) FROM listing_flags WHERE field='access' AND closed_at IS NULL").fetchone()[0] == 0


def test_a_sentence_the_fields_value_was_accepted_from_is_not_marked_unclear(tmp_path):
    # a reader went on marking a road-frontage sentence as unclear for weeks after access=direct had been accepted from it
    from verda.facts.model import Proposal
    store = _store(tmp_path)
    first = _thread(store)
    note = dict(listing_ref='1000000001', mode='local', field='access', note='Kadastral olduğu yazmıyor.')
    with store.transaction() as con:
        early = raise_flag(con, **note, observation_id=first, source_ref='message:bb0000000002', quote='yolu var', now=5.0)
        assert early['status'] == 'flagged'
        said = Proposal(listing_ref='1000000001', field='access', value='direct', proposer='llm:manager', mode='local', observation_id=first, source_ref='message:bb0000000002', quote='yolu var')
        assert store.facts.submit(con, said, now=10.0)['status'] == 'accepted'
        assert open_flags(con, '1000000001', 'local') == []                                                                      # settled by the newer value, as before
    second = _reread(store)
    with store.transaction() as con:
        late = raise_flag(con, **note, observation_id=second, source_ref='message:cc0000000003', quote='merhaba, yolu var, sit durumu da yoktur', now=20.0)
        assert late == {'status': 'settled', 'reason': 'field_already_has_a_value_from_this_sentence'}
        # a note of that kind written before this rule is no longer shown either
        con.execute('INSERT INTO listing_flags(id,listing_ref,mode,field,note,observation_id,source_ref,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('old', '1000000001', 'local', 'access', 'eski not', second, 'message:cc0000000003', 'yolu var', 30.0))
        assert open_flags(con, '1000000001', 'local') == []
        # another field read from the same message is a different question, and so is another sentence about the road
        assert raise_flag(con, **{**note, 'field': 'natural_sit'}, observation_id=second, source_ref='message:cc0000000003', quote='sit durumu da yoktur', now=31.0)['status'] == 'flagged'
        con.execute('INSERT INTO listing_flags(id,listing_ref,mode,field,note,observation_id,source_ref,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('other', '1000000001', 'local', 'access', 'yeni mesaj', second, 'message:dd0000000004', 'ama kadastral mı bilmiyorum', 32.0))
        assert [(f['field'], f['id']) for f in open_flags(con, '1000000001', 'local')] == [('natural_sit', open_flags(con, '1000000001', 'local')[0]['id']), ('access', 'other')]
