""""Needs clarification": a reading agent's note that a seller's answer does not settle a field.

The note carries the seller's own words, checked by code against the recorded message exactly like a proposed fact, so the
user sees the evidence on the listing page and can settle the field himself (a user decision) or say it is clear. A note is
never a fact and never changes a decision. It ends when the field gets a newer accepted value, or when the user closes it.

One sentence is one note. On 13 Nov 2026 the 21 candidates with notes carried 61 of them: a reader marked the same sentence again at
every re-read of the conversation (its message id and the record change, the words do not), and went on marking a sentence from which
the field's value had already been accepted. So a note is refused when the field already holds a value accepted from that very
sentence, and notes are told apart by listing, field and the seller's words.
"""
import time
from uuid import uuid4

from verda.facts.model import FIELDS, THREAD
from verda.facts.verify import source_texts
from verda.facts.text import fold, locate

CLARIFIABLE = ('access', 'archaeological_sit', 'natural_sit', 'parcel_key', 'shared_title', 'sale_scope_resolved', 'sale_parcels')
MAX_NOTE = 300


def same_words(a, b):
    """Two quotes of the same sentence: one contains the other, whatever the case, spacing and punctuation."""
    a, b = fold(a or ''), fold(b or '')
    return bool(a and b) and (a in b or b in a)


def _answered(con, listing_ref, mode, field, quote, since):
    """The field has an accepted value newer than `since`, or one accepted from the very sentence in `quote`."""
    for c in con.execute("SELECT c.quote, c.created_at, c.source_kind FROM claims c WHERE listing_ref=? AND mode=? AND field=? AND status='accepted' "
                         "AND NOT EXISTS (SELECT 1 FROM claims w WHERE w.resolves=c.id AND w.status='withdrawn')", (listing_ref, mode, field)):
        # the same sentence: only where the value was read from the seller's own text (a user's note is not a quote of him)
        if (since is not None and c['created_at'] > since) or (c['source_kind'] in ('thread', 'listing') and same_words(c['quote'], quote)):
            return True
    return False


def _ensure(con):
    con.execute('''CREATE TABLE IF NOT EXISTS listing_flags(
        id TEXT PRIMARY KEY, listing_ref TEXT NOT NULL, mode TEXT NOT NULL, field TEXT NOT NULL, note TEXT NOT NULL,
        observation_id TEXT NOT NULL, source_ref TEXT NOT NULL, quote TEXT NOT NULL, created_at REAL NOT NULL,
        closed_at REAL, closed_note TEXT)''')


def raise_flag(con, *, listing_ref, mode, field, observation_id, source_ref, quote, note, now=None):
    now = time.time() if now is None else now
    if field not in CLARIFIABLE or field not in FIELDS:
        return {'status': 'rejected', 'reason': 'field_not_clarifiable'}
    note = (note or '').strip()[:MAX_NOTE]
    if not note or not (quote or '').strip():
        return {'status': 'rejected', 'reason': 'note_and_quote_required'}
    row = con.execute('SELECT mode,tool,listing_ref,payload FROM observations WHERE id=?', (observation_id,)).fetchone()
    if row is None or row['mode'] != mode or row['listing_ref'] != listing_ref:
        return {'status': 'rejected', 'reason': 'observation_not_of_this_listing'}
    import json
    every = source_texts(row['tool'], json.loads(row['payload']))
    ref = source_ref if source_ref.startswith(('message:', 'attributes.')) or source_ref in ('title', 'description', 'page_text') else 'message:' + source_ref
    named = [t for t in every if t['ref'] == ref]
    # A bare or wrong message id is the reader's slip, not a lie about the text (32 true flags were refused on 15 Oct 2026 for a missing
    # "message:" prefix): the quote is looked up, word for word, in the seller's messages of this very record, exactly as for a fact.
    candidates = named if named and locate(quote, named[0]['text']) is not None else [t for t in every if locate(quote, t['text']) is not None]
    texts = [t for t in candidates if row['tool'] != THREAD or t['role'] == 'seller']
    if not texts:
        return {'status': 'rejected', 'reason': 'quote_not_in_source'}
    source_ref = texts[0]['ref']
    _ensure(con)
    if _answered(con, listing_ref, mode, field, quote, None):
        return {'status': 'settled', 'reason': 'field_already_has_a_value_from_this_sentence'}
    old = next((f for f in open_flags(con, listing_ref, mode) if f['field'] == field and same_words(f['quote'], quote)), None)
    if old:
        return {'status': 'exists', 'flag_id': old['id']}
    flag_id = uuid4().hex
    con.execute('INSERT INTO listing_flags(id,listing_ref,mode,field,note,observation_id,source_ref,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (flag_id, listing_ref, mode, field, note, observation_id, source_ref, quote.strip()[:500], now))
    return {'status': 'flagged', 'flag_id': flag_id}


def open_flags(con, listing_ref=None, mode='local'):
    """Notes still standing, one per sentence (the newest wording, with how often it was marked): not closed by the user, and the field has
    neither an accepted value newer than the note nor one accepted from the sentence the note is about."""
    _ensure(con)
    where, params = "mode=? AND closed_at IS NULL", [mode]
    if listing_ref:
        where += ' AND listing_ref=?'; params.append(listing_ref)
    out, seen = [], {}
    for f in con.execute(f'SELECT * FROM listing_flags WHERE {where} ORDER BY created_at', params).fetchall():
        if _answered(con, f['listing_ref'], mode, f['field'], f['quote'], f['created_at']):
            continue
        key = (f['listing_ref'], f['field'], fold(f['quote']))
        if key in seen:
            kept = out[seen[key]]
            out[seen[key]] = {**dict(f), 'first_at': kept['first_at'], 'repeats': kept['repeats'] + 1}
        else:
            seen[key] = len(out)
            out.append({**dict(f), 'first_at': f['created_at'], 'repeats': 1})
    return out


def close_flag(con, flag_id, note, now=None):
    """The user says it is clear. Every note on the same sentence for the same field goes with it: closing one of six must not bring up the next."""
    _ensure(con)
    now = time.time() if now is None else now
    flag = con.execute('SELECT * FROM listing_flags WHERE id=? AND closed_at IS NULL', (flag_id,)).fetchone()
    if flag is None:
        raise ValueError('Flag not open')
    for other in con.execute('SELECT id, quote FROM listing_flags WHERE listing_ref=? AND mode=? AND field=? AND closed_at IS NULL',
                             (flag['listing_ref'], flag['mode'], flag['field'])).fetchall():
        if other['id'] == flag_id or fold(other['quote']) == fold(flag['quote']):
            con.execute('UPDATE listing_flags SET closed_at=?, closed_note=? WHERE id=?', (now, (note or '')[:300], other['id']))
