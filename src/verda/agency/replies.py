"""Seller replies already read but not yet turned into facts: the agent's work list for interpreting them.

Bookkeeping only (no site access): it hands over, per listing, the observation id, the seller's messages with their ids and the
fields still unknown. The agent proposes facts with `facts.propose`; the verifier still checks every quote word for word.

Two things used to hide what a seller had said (found on 13 Nov 2026):
- A road hint from the parcel's geometry filled the `access` field, so a seller's "there is a road" was never read as an answer about the road.
  A hint is not the seller's word: with only a hint, the field is still open here.
- A field a reader had marked "needs clarification" was never looked at again, also where the owner's standing rules settle it (a seller who
  names the parcel's frontage on a road has said there is a road). Such a field is handed over once more, with the reader's note; after that
  the note stands until the owner decides."""
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


def words_key(message):
    """A message told by its words. Its id is made from the time label the page shows ("Dün", "2 gün önce") and so changes from one read
    to the next; a mark keyed on the id forgot what had been handed over as soon as the label changed."""
    import hashlib
    from verda.facts.text import fold
    return 'w:' + hashlib.sha1(fold(str(message.get('text') or '')).encode()).hexdigest()[:12]


def _reviewed(mark):
    """field -> the seller's last words when this routine read the field ('*': read, under an older mark that did not keep the words)."""
    seen = mark.get('reviewed') if isinstance(mark, dict) else None
    return dict(seen) if isinstance(seen, dict) else {f: '*' for f in seen or []}


def _open(field, facts, scope_open):
    """Still worth reading the seller's words for: no value yet, or for the road only a hint from the parcel's geometry."""
    from verda.facts.assemble import HINT
    if field == 'sale_scope':
        return scope_open
    return field not in facts or (field == 'access' and (facts[field] or {}).get('value') == HINT)


def has_pending(store, mode='local', now=None):
    """True when some seller answer still waits to be interpreted. Marks nothing."""
    return bool(_find(store, mode, time.time() if now is None else now, [], 1, first_only=True)[0])


def pending(store, args, *, mode='local', now=None):
    now = time.time() if now is None else now
    limit = max(1, min(int(args.get('limit') or 4), 8))
    wanted = [str(x) for x in args.get('listing_ids') or []]
    out, remaining, tried = _find(store, mode, now, wanted, limit)
    if out:
        for o in out:
            last, before = o.pop('last_words'), o.pop('handed', None)
            # every field handed over now has been read by this routine for these words: a note it writes itself is not read "once more"
            tried[o['listing_id']] = {'at': now, 'last': last, 'times': (before['times'] if before else 0) + 1,
                                      'reviewed': {**_reviewed(tried.get(o['listing_id'])), **{f: last for f in o['missing_fields']}}}
        with store.transaction() as con:
            con.execute('INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (STATE_KEY, json.dumps(tried), now))
    return {'items': out, 'remaining_after_this': max(0, remaining - len(out)),
            'advice': 'propose_facts' if out else 'nothing_to_do',
            'rules': "Her eksik alan için, ilgili satıcı mesajından harfi harfine alıntıyla facts.propose çağır. Genel 'sit yok' cevabı hem archaeological_sit=false hem natural_sit=false demektir. Net değilse öneri verme. "
                     "flagged: bir okuyucunun daha önce 'netleştirme gerekli' dediği alanlar ve notu. Bunları kurallarına göre bir kez daha oku: cevap alanı netleştiriyorsa "
                     "facts.propose ile öner (işaret kendiliğinden kalkar); gerçekten belirsizse hiçbir şey yapma, işaret durur. Yeniden işaretleme."}


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
        flagged = {}
        for f in open_flags(con, None, mode):
            flagged[(f['listing_ref'], f['field'])] = f
    out, remaining = [], 0
    for listing, (observation_id, payload, read_at) in latest.items():
        if wanted and listing not in wanted:
            continue
        mark = tried.get(listing) or {}
        mark = mark if isinstance(mark, dict) else {'at': mark}
        seller = [m for m in (json.loads(payload).get('messages') or []) if m.get('role') != 'user' and m.get('id') and str(m.get('text') or '').strip()]
        if not seller:
            continue
        last = words_key(seller[-1])
        same = bool(mark.get('at')) and mark.get('last') in (last, seller[-1]['id'])        # these very words were handed over before
        if same and now - mark['at'] < RETRY_AFTER and not include_tried:
            continue                             # handed over recently and nothing newer has arrived from the seller
        current = fact_status(store, listing, mode=mode)
        if (current.get('decision') or {}).get('state') in ('excluded', 'outside_search') or current.get('legacy_lifecycle') == 'excluded':
            continue
        facts = current.get('facts') or {}
        from verda.facts.text import fold
        said = fold(' '.join(str(m['text']) for m in seller))
        words = set(re.findall(r'\w+', said))
        scope_open = 'sale_scope' in (current.get('conflicts') or [])
        spoken = [f for f in FIELDS if _open(f, facts, scope_open) and any((w in words) if w in WHOLE_WORDS else (w in said) for w in map(fold, SUBJECT[f]))]
        noted = {f: flagged.get((listing, f if f != 'sale_scope' else 'sale_scope_resolved')) for f in spoken}
        seen = _reviewed(mark)
        # Marked by a reader and not yet read once more under the standing rules. A note first written after this routine was handed these very
        # words is its own, or repeats what it had already judged: no second look.
        again = [f for f in spoken if noted[f] and not (seen.get(f) in ('*', last) or (same and noted[f].get('first_at', 0) >= mark['at']))]
        missing = [f for f in spoken if not noted[f]] + again
        if not missing:
            continue
        remaining += 1
        if first_only:
            out.append({'listing_id': listing})
            break
        if len(out) < limit:
            out.append({'listing_id': listing, 'observation_id': observation_id, 'missing_fields': missing, 'read_at': read_at, 'last_words': last,
                        **({'handed': {'at': mark['at'], 'times': int(mark.get('times') or 1)}} if same else {}),
                        **({'flagged': [{'field': f, 'note': noted[f]['note'], 'quote': noted[f]['quote']} for f in again]} if again else {}),
                        'seller_messages': [{'source_ref': 'message:' + str(m['id']), 'text': str(m['text'])[:600]} for m in seller][-6:]})
    return out, remaining, tried


def waiting(store, mode='local', now=None):
    """Every seller answer still not turned into a fact or a flag, whether or not it was handed over today: for the watchdog. Marks nothing."""
    now = time.time() if now is None else now
    return _find(store, mode, now, [], 10_000, include_tried=True)[0]
