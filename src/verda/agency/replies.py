"""Seller replies already read but not yet turned into facts: the agent's work list for interpreting them.

Bookkeeping only (no site access): it hands over, per listing, the observation id, the seller's messages with their ids and the
fields still unknown. The agent proposes facts with `facts.propose`; the verifier still checks every quote word for word."""
import json
import re
import time

FIELDS = ('parcel_key', 'natural_sit', 'archaeological_sit', 'access', 'sale_scope')
# A field is only worth the agent's time when some seller message even mentions its subject. "A1", "D2" are the sit grades' short forms.
SUBJECT = {'parcel_key': ('parsel', 'ada'), 'natural_sit': ('sit', 'd1', 'd2', 'd3'), 'archaeological_sit': ('sit', 'a1', 'a2', 'a3'),
           'access': ('yol', 'cephe', 'cadde', 'sokak', 'geçiş', 'gecis'), 'sale_scope': ('hisse', 'tapu', 'm2', 'm²', 'metrekare', 'dönüm', 'parsel')}
WHOLE_WORDS = {'ada', 'a1', 'a2', 'a3', 'd1', 'd2', 'd3'}      # matched as words: "ada" is inside "kadastral"
RETRY_AFTER = 86400
STATE_KEY = 'replies:attempted'


def has_pending(store, mode='local', now=None):
    """True when some seller answer still waits to be interpreted. Marks nothing."""
    return bool(_find(store, mode, time.time() if now is None else now, [], 1, first_only=True)[0])


def pending(store, args, *, mode='local', now=None):
    now = time.time() if now is None else now
    limit = max(1, min(int(args.get('limit') or 4), 8))
    wanted = [str(x) for x in args.get('listing_ids') or []]
    out, remaining, tried = _find(store, mode, now, wanted, limit)
    if out:
        tried.update({o['listing_id']: {'at': now, 'last': o['seller_messages'][-1]['source_ref'].split(':', 1)[1]} for o in out})
        with store.transaction() as con:
            con.execute('INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (STATE_KEY, json.dumps(tried), now))
    return {'items': out, 'remaining_after_this': max(0, remaining - len(out)),
            'advice': 'propose_facts' if out else 'nothing_to_do',
            'rules': "Her eksik alan için, ilgili satıcı mesajından harfi harfine alıntıyla facts.propose çağır. Genel 'sit yok' cevabı hem archaeological_sit=false hem natural_sit=false demektir. Net değilse öneri verme."}


def _find(store, mode, now, wanted, limit, first_only=False, include_tried=False):
    from verda.agency.research import fact_status
    with store.transaction() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (STATE_KEY,)).fetchone()
        tried = json.loads(row[0]) if row else {}
        latest = {}
        # The newest read that found the conversation: a later "not found" (a reading failure) must not hide what the seller wrote.
        for r in con.execute("SELECT listing_ref,id,payload,captured_at FROM observations WHERE tool='sahibinden.read_thread' AND mode=? AND json_extract(payload,'$.found')=1 ORDER BY captured_at,id", (mode,)):
            latest[r['listing_ref']] = (r['id'], r['payload'], r['captured_at'])
        from verda.facts.flags import open_flags
        flagged = {(f['listing_ref'], f['field']) for f in open_flags(con, None, mode)}
    out, remaining = [], 0
    for listing, (observation_id, payload, read_at) in latest.items():
        if wanted and listing not in wanted:
            continue
        mark = tried.get(listing) or {}
        mark = mark if isinstance(mark, dict) else {'at': mark}
        seller = [m for m in (json.loads(payload).get('messages') or []) if m.get('role') != 'user' and m.get('id') and str(m.get('text') or '').strip()]
        if not seller:
            continue
        if not include_tried and now - mark.get('at', 0) < RETRY_AFTER and mark.get('last') == seller[-1]['id']:
            continue                             # handed over recently and nothing newer has arrived from the seller
        current = fact_status(store, listing, mode=mode)
        if (current.get('decision') or {}).get('state') in ('excluded', 'outside_search') or current.get('legacy_lifecycle') == 'excluded':
            continue
        facts = current.get('facts') or {}
        from verda.facts.text import fold
        said = fold(' '.join(str(m['text']) for m in seller))
        words = set(re.findall(r'\w+', said))
        scope_open = 'sale_scope' in (current.get('conflicts') or [])
        missing = [f for f in FIELDS if (f not in facts if f != 'sale_scope' else scope_open) and (listing, f if f != 'sale_scope' else 'sale_scope_resolved') not in flagged
                   and any((w in words) if w in WHOLE_WORDS else (w in said) for w in map(fold, SUBJECT[f]))]
        if not missing:
            continue
        remaining += 1
        if first_only:
            out.append({'listing_id': listing})
            break
        if len(out) < limit:
            out.append({'listing_id': listing, 'observation_id': observation_id, 'missing_fields': missing, 'read_at': read_at,
                        'seller_messages': [{'source_ref': 'message:' + str(m['id']), 'text': str(m['text'])[:600]} for m in seller][-6:]})
    return out, remaining, tried


def waiting(store, mode='local', now=None):
    """Every seller answer still not turned into a fact or a flag, whether or not it was handed over today: for the watchdog. Marks nothing."""
    now = time.time() if now is None else now
    return _find(store, mode, now, [], 10_000, include_tried=True)[0]
