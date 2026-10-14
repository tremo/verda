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
    return out


BASIS = {'site': 'yerinde gördüm', 'seller': 'satıcı ya da emlakçı söyledi', 'official': 'resmî belgede gördüm', 'map': 'haritadan baktım'}


def import_feedback(store, firestore, *, now=None, mode='local'):
    now = time.time() if now is None else now
    docs = firestore.list_docs(COLLECTION)
    with store.transaction() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (STATE_KEY,)).fetchone()
        done = set(json.loads(row[0])) if row else set()
    applied = 0
    for listing_id, doc in docs:
        for field, value, signature, basis in decisions(doc):
            key = f'{listing_id}:{signature}'
            if key in done:
                continue
            try:
                with store.transaction() as con:
                    store.facts.decide(con, listing_id, mode, field, value, f'Panoda düzeltildi ({BASIS.get(basis, basis) or "dayanak yok"})', now)
            except Exception:                       # a correction the fact rules refuse is not retried every round; it stays in the dashboard
                pass
            done.add(key)
            applied += 1
    with store.transaction() as con:
        con.execute('INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                    (STATE_KEY, json.dumps(sorted(done)), now))
    return {'feedback_documents': len(docs), 'user_facts_applied': applied}
