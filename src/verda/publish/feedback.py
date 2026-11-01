"""The owner's corrections made on the dashboard (userFacts in `listingFeedback`) become Verda's own user-grade facts, once each.

The dashboard keeps its own vocabulary (natural '1'/'2'/'3'/'none', road 'cadastral'/'rightOfWay'/'none', ...); this maps it back to the fact
vocabulary. Only changes are imported: every (listing, field, value, time) is remembered, so a correction is applied once and a later
different one is applied after it."""
import json
import time

COLLECTION = 'listingFeedback'
STATE_KEY = 'feedback:imported'
MAP = {
    'natural': ('natural_sit', {'none': 'none', '1': 'first_degree', '2': 'second_degree', '3': 'third_degree'}),
    'archaeological': ('archaeological_sit', {'none': False, 'present': True}),
    'road': ('access', {'cadastral': 'direct', 'rightOfWay': 'existing_easement', 'none': 'none'}),
    'sharedTitle': ('shared_title', {'no': False, 'yes': True}),
}


def decisions(doc):
    """[(field, value, signature, basis)] from one feedback document."""
    out = []
    for key, fact in (doc.get('userFacts') or {}).items():
        if not isinstance(fact, dict):
            continue
        at = str(fact.get('at') or '')
        basis = str(fact.get('basis') or '')
        if key in MAP:
            field, values = MAP[key]
            if fact.get('value') in values:
                out.append((field, values[fact['value']], f"{key}={fact['value']}@{at}", basis))
        elif key == 'parcelId' and str(fact.get('ada', '')).isdigit() and str(fact.get('parsel', '')).isdigit():
            out.append(('parcel_key', f"{int(fact['ada'])}/{int(fact['parsel'])}", f"parcelId={fact['ada']}/{fact['parsel']}@{at}", basis))
    # "İlgilenmiyorum" on the dashboard is the user's own elimination: Verda stops working on the listing (and starts again when it is taken back).
    decision = doc.get('decision')
    if decision in ('excluded', 'shortlist', 'maybe') and doc.get('updatedAt'):              # a note alone decides nothing
        out.append(('user_excluded', decision == 'excluded', f"decision={decision}@{doc['updatedAt']}", 'dashboard'))
    elif decision == '' and doc.get('updatedAt'):
        # "Kararı kaldır": the dashboard's own elimination is taken back. A note saved without a decision carries '' too, so this only undoes an
        # exclusion that came from the dashboard (import_feedback checks), never one the user gave Verda another way.
        out.append(('user_excluded', False, f"decision=removed@{doc['updatedAt']}", 'dashboard-removed'))
    return out


def tracked(signature):
    """The fact key a signature belongs to ('natural=none@t' -> 'natural'); None for the decision."""
    key = signature.split('=', 1)[0]
    return key if key in MAP or key == 'parcelId' else None


FIELD = {**{key: field for key, (field, _) in MAP.items()}, 'parcelId': 'parcel_key'}


BASIS = {'dashboard-removed': 'panoda kararını geri aldın', 'site': 'yerinde gördüm', 'seller': 'satıcı ya da emlakçı söyledi', 'official': 'resmî belgede gördüm', 'map': 'haritadan baktım', 'dashboard': 'panoda kararını verdin'}


HELD_KEY = 'feedback:held'


def _dashboard_excluded(store, listing_id, mode):
    """True when Verda's newest word on user_excluded is an elimination that came from the dashboard."""
    with store.transaction() as con:
        row = con.execute("SELECT value, quote FROM claims c WHERE listing_ref=? AND mode=? AND field='user_excluded' AND status='accepted' "
                          "AND NOT EXISTS (SELECT 1 FROM claims w WHERE w.resolves=c.id AND w.status='withdrawn') ORDER BY seq DESC LIMIT 1", (listing_id, mode)).fetchone()
    return bool(row) and json.loads(row['value']) is True and str(row['quote'] or '').startswith('Panoda düzeltildi')


def import_feedback(store, firestore, *, now=None, mode='local'):
    """Applies the dashboard's corrections once each, and takes back one the dashboard has since deleted ("Sistemdeki gibi bırak")."""
    now = time.time() if now is None else now
    docs = firestore.list_docs(COLLECTION)
    with store.transaction() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (STATE_KEY,)).fetchone()
        done = set(json.loads(row[0])) if row else set()
        row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (HELD_KEY,)).fetchone()
        if row:
            held = {listing: set(keys) for listing, keys in json.loads(row[0]).items()}
        else:                                       # first run with this memory: every correction ever applied counts as held
            held = {}
            for entry in done:
                listing, _, signature = entry.partition(':')
                if tracked(signature):
                    held.setdefault(listing, set()).add(tracked(signature))
    applied = withdrawn = 0
    for listing_id, doc in docs:
        present = {key for key, fact in (doc.get('userFacts') or {}).items() if isinstance(fact, dict) and (key in MAP or key == 'parcelId')}
        for key in sorted(held.get(listing_id, set()) - present):
            try:
                with store.transaction() as con:
                    store.facts.withdraw(con, listing_id, mode, FIELD[key], 'Panoda geri alındı: sistemdeki gibi bırakıldı', now)
                withdrawn += 1
            except Exception:                       # the correction stays held and is tried again next round
                present.add(key)
        for field, value, signature, basis in decisions(doc):
            key = f'{listing_id}:{signature}'
            if key in done:
                continue
            if basis == 'dashboard-removed' and not _dashboard_excluded(store, listing_id, mode):
                done.add(key)
                continue
            try:
                with store.transaction() as con:
                    store.facts.decide(con, listing_id, mode, field, value, f'Panoda düzeltildi ({BASIS.get(basis, basis) or "dayanak yok"})', now)
            except Exception:                       # a correction the fact rules refuse is not retried every round; it stays in the dashboard
                pass
            done.add(key)
            applied += 1
        if present:
            held[listing_id] = present
        else:
            held.pop(listing_id, None)
    with store.transaction() as con:
        for state_key, state in ((STATE_KEY, sorted(done)), (HELD_KEY, {l: sorted(k) for l, k in sorted(held.items())})):
            con.execute('INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                        (state_key, json.dumps(state), now))
    return {'feedback_documents': len(docs), 'user_facts_applied': applied, 'user_facts_withdrawn': withdrawn}
