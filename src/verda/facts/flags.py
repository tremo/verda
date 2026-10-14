""""Needs clarification": a reading agent's note that a seller's answer does not settle a field.

The note carries the seller's own words, checked by code against the recorded message exactly like a proposed fact, so the
user sees the evidence on the listing page and can settle the field himself (a user decision) or say it is clear. A note is
never a fact and never changes a decision. It ends when the field gets a newer accepted value, or when the user closes it.
"""
import time
from uuid import uuid4

from verda.facts.model import FIELDS, THREAD
from verda.facts.verify import source_texts
from verda.facts.text import locate

CLARIFIABLE = ('access', 'archaeological_sit', 'natural_sit', 'parcel_key', 'shared_title')
MAX_NOTE = 300


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
    texts = [t for t in source_texts(row['tool'], json.loads(row['payload'])) if t['ref'] == source_ref]
    if not texts or (row['tool'] == THREAD and texts[0]['role'] != 'seller') or locate(quote, texts[0]['text']) is None:
        return {'status': 'rejected', 'reason': 'quote_not_in_source'}
    _ensure(con)
    old = con.execute('SELECT id FROM listing_flags WHERE listing_ref=? AND mode=? AND field=? AND source_ref=? AND observation_id=? AND closed_at IS NULL',
                      (listing_ref, mode, field, source_ref, observation_id)).fetchone()
    if old:
        return {'status': 'exists', 'flag_id': old['id']}
    flag_id = uuid4().hex
    con.execute('INSERT INTO listing_flags(id,listing_ref,mode,field,note,observation_id,source_ref,quote,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (flag_id, listing_ref, mode, field, note, observation_id, source_ref, quote.strip()[:500], now))
    return {'status': 'flagged', 'flag_id': flag_id}


def open_flags(con, listing_ref=None, mode='local'):
    """Notes still standing: not closed by the user and the field has no accepted value newer than the note."""
    _ensure(con)
    where, params = "mode=? AND closed_at IS NULL", [mode]
    if listing_ref:
        where += ' AND listing_ref=?'; params.append(listing_ref)
    out = []
    for f in con.execute(f'SELECT * FROM listing_flags WHERE {where} ORDER BY created_at', params):
        settled = con.execute("SELECT 1 FROM claims WHERE listing_ref=? AND mode=? AND field=? AND status='accepted' AND created_at>? LIMIT 1",
                              (f['listing_ref'], mode, f['field'], f['created_at'])).fetchone()
        if not settled:
            out.append(dict(f))
    return out


def close_flag(con, flag_id, note, now=None):
    _ensure(con)
    now = time.time() if now is None else now
    if con.execute('UPDATE listing_flags SET closed_at=?, closed_note=? WHERE id=? AND closed_at IS NULL', (now, (note or '')[:300], flag_id)).rowcount != 1:
        raise ValueError('Flag not open')
