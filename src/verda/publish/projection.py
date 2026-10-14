"""The documents v2 publishes: one overlay per listing plus a status document. Private data only goes to the private Firestore; the
dashboard merges these over its own listing records. The conversation text is included for the owner's own private view; seller names and phone numbers are not added."""
import datetime
import json

from verda.agency import learning
from verda.agency.research import fact_status
from verda.agency.score import ranking

COLLECTION = 'v2Listings'
ASSETS = 'evidenceAssets'             # the collection the dashboard already reads photos from; ids are content hashes, so nothing collides
PHOTOS_SHOWN = 5
SHOWN_FACTS = ('parcel_key', 'sale_area_m2', 'property_type', 'natural_sit', 'archaeological_sit', 'access', 'shared_title')


def iso(epoch):
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat() if epoch else None


def contact_of(conversation, actions):
    """The dashboard's own wording for how far the conversation has come."""
    if any(a['state'] in ('replied', 'unclear') for a in actions) or (conversation or {}).get('received'):
        return 'Yanıt işlendi'
    if (conversation or {}).get('sent') or any(a['state'] in ('asked', 'unknown_delivery', 'sending') for a in actions):
        return 'İletişim kuruldu'
    return None


def photo_ids(store, listing_id, mode='local'):
    with store.transaction() as con:
        return [r[0] for r in con.execute("SELECT id FROM evidence_assets WHERE listing_ref=? AND mode=? AND kind='photo' ORDER BY position, created_at LIMIT ?", (listing_id, mode, PHOTOS_SHOWN))] \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_assets'").fetchone() else []


def thread_of(store, listing_id, mode='local', limit=30, cap=1200):
    """The last conversation read, as the dashboard shows it: who wrote, when (as Sahibinden prints it) and what. For the owner's private view only."""
    with store.transaction() as con:
        row = con.execute("SELECT payload FROM observations WHERE tool='sahibinden.read_thread' AND mode=? AND listing_ref=? ORDER BY captured_at DESC, id DESC LIMIT 1", (mode, listing_id)).fetchone()
    messages = (json.loads(row['payload']).get('messages') or []) if row else []
    return [{'who': 'sen' if m.get('role') == 'user' else 'satıcı', 'text': str(m.get('text') or '')[:cap], 'at': m.get('sent_at_text')} for m in messages[-limit:]]


def proposals_of(store, listing_id, mode='local', limit=6):
    """Proposals read from the seller's words that were not accepted (turned down by the rules, or waiting for a person), newest first. A field that now has an accepted claim with the same value is left out."""
    with store.transaction() as con:
        rows = [dict(r) for r in con.execute("SELECT id,field,value,status,reason,quote,source_ref,created_at FROM claims WHERE listing_ref=? AND mode=? AND source_kind='thread' "
                                             "AND status IN ('rejected','needs_review') ORDER BY created_at DESC", (listing_id, mode))]
        accepted = {(r['field'], r['value']) for r in con.execute("SELECT field,value FROM claims WHERE listing_ref=? AND mode=? AND status='accepted'", (listing_id, mode))}
    out, seen = [], set()
    for r in rows:
        key = (r['field'], r['value'])
        if key in accepted or key in seen:
            continue
        seen.add(key)
        out.append({'id': r['id'], 'field': r['field'], 'value': json.loads(r['value']) if isinstance(r['value'], str) else r['value'], 'status': r['status'],
                    'reason': r['reason'], 'quote': (r['quote'] or '')[:300], 'at': iso(r['created_at'])})
    return out[:limit]


def facts_of(store, current, mode='local', quote_cap=200):
    """The screening facts as the dashboard shows them: value, grade, where it came from, what the seller wrote and when it was recorded."""
    shown = {k: v for k, v in (current.get('facts') or {}).items() if k in SHOWN_FACTS and isinstance(v, dict)}
    ids = [v['claim_id'] for v in shown.values() if v.get('claim_id')]
    when = {}
    if ids:
        with store.transaction() as con:
            when = {r['id']: r['created_at'] for r in con.execute(f"SELECT id, created_at FROM claims WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return {k: {'value': v.get('value'), 'grade': v.get('grade'), 'source': v.get('source_kind'), 'at': iso(when.get(v.get('claim_id'))),
                'quote': (v.get('quote') or '')[:quote_cap] or None} for k, v in shown.items()}


def parcel_shape(store, listing_id, mode='local'):
    """The parcel's outline from the newest TKGM lookup, in the dashboard's own wrapping (Firestore takes no array inside an array, so a list of
    points travels as {items: [...]}). None when the lookup found no outline."""
    with store.transaction() as con:
        rows = con.execute("SELECT payload FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref=? ORDER BY captured_at DESC, id DESC", (mode, listing_id)).fetchall()
    for row in rows:
        try:
            geometry = json.loads(row['payload']).get('geometry') or {}
        except ValueError:
            continue
        ring = geometry.get('ring')
        if geometry.get('type') == 'Polygon' and isinstance(ring, list) and len(ring) >= 4 and isinstance(geometry.get('centroid'), list):
            return {'geometry': {'type': 'Polygon', 'coordinates': [{'items': [{'items': [float(x), float(y)]} for x, y in ring]}]}, 'centroid': [float(c) for c in geometry['centroid']]}
    return None


def opp_view(new):
    """The opportunity score's numbers for the dashboard: what it says and why."""
    return {'score': new['score'], 'opportunity': new['opportunity'], 'purpose': new['purpose'], 'proximity': new['proximity'], 'label': new['label'], 'gapPct': new['gap_pct'],
            'fairUnitPrice': new['fair_unit_price'], 'road': new['road'], 'caps': new['caps'], 'parts': new['parts']}


def listing_document(store, row, now_iso, *, mode='local', new=None):
    current = fact_status(store, row['listing_id'], mode=mode)
    built = learning.learning(store, row['listing_id'], learning.unknown_fields(current), current.get('next_action'), learning.context_of(current), mode=mode)
    actions = [{'field': f, 'state': v['state'], 'label': v['label'], 'at': iso(v['at']), 'url': v['url']} for f, v in built['fields'].items()]
    conversation = built.get('conversation') or {}
    next_action = current.get('next_action') or {}
    routes = [e['value'] for e in ((current.get('facts') or {}).get('route_minutes') or []) if isinstance(e, dict) and isinstance(e.get('value'), (int, float))]
    return {
        'links': {'listing': f"https://www.sahibinden.com/ilan/{row['listing_id']}/detay", 'tkgm': (current.get('links') or {}).get('tkgm')},
        'decisionReasons': (current.get('decision') or {}).get('reasons') or [],
        'routeMinutesMin': min(routes) if routes else None,
        'listingId': row['listing_id'], 'state': row['state'], 'score': row['score'], 'penalties': row['penalties'], 'missing': row['missing'],
        'needsClarification': bool(row['needs_clarification']), 'provisional': bool(row['provisional']),
        'title': row['title'], 'priceTl': row['price_tl'], 'areaM2': row['area_m2'], 'neighborhood': row['neighborhood'], 'type': row['type'],
        'routeMinutesMax': row['route_minutes_max'], 'valuation': row['valuation'], 'discountPct': row['discount_pct'], 'comparables': row['comparables'],
        'benchmarkUnitPrice': row['benchmark_unit_price'], 'benchmarkScope': row['benchmark_scope'], 'confidence': row['confidence'],
        'publishedAt': iso(row['published_at']), 'discoveredAt': iso(row['discovered_at']), 'discoveredBy': row['discovered_by'],
        'nextAction': {'action': next_action.get('action'), 'why': next_action.get('why')},
        'actions': actions, 'contact': contact_of(conversation, actions),
        'conversation': {'sent': conversation.get('sent', 0), 'received': conversation.get('received', 0), 'url': conversation.get('url'), 'checkedAt': iso(conversation.get('checked_at'))},
        'facts': facts_of(store, current, mode),
        'thread': thread_of(store, row['listing_id'], mode),
        'proposals': proposals_of(store, row['listing_id'], mode),
        'opp': opp_view(new) if new else None,
        'photos': photo_ids(store, row['listing_id'], mode),
        **(parcel_shape(store, row['listing_id'], mode) or {}),
        'updatedAt': now_iso,
    }


def closed_documents(store, ranked_ids, now_iso, *, mode='local'):
    """Listings this system has taken out of the running, so the dashboard's older record cannot keep calling them candidates: removed from
    Sahibinden (the page says "ilan yayında değildir"), outside the search (route over the limit) or eliminated by a rule. A listing only the
    old system eliminated is left to the old record."""
    from verda.agency import score as base
    from verda.agency.listing_dates import legacy_titles
    from verda.agency.queue import neighborhood_of
    legacy = {r['id']: r for r in base._legacy_rows(store)}
    titles, old_titles = base._titles(store, mode), legacy_titles(store)
    with store.transaction() as con:
        assessed = [r[0] for r in con.execute('SELECT DISTINCT listing_ref FROM assessments WHERE mode=? ORDER BY listing_ref', (mode,))]
        gone_at = {r['listing_ref']: r['at'] for r in con.execute("SELECT listing_ref, max(captured_at) AS at FROM observations WHERE tool='sahibinden.read_listing' AND mode=? "
                                                                  "AND payload LIKE '%\"unavailable\":true%' AND listing_ref IS NOT NULL GROUP BY 1", (mode,))}
    out = []
    for listing in sorted(set(assessed) | set(gone_at)):
        if listing in ranked_ids:
            continue
        current = fact_status(store, listing, mode=mode)
        decision = current.get('decision') or {}
        removed = bool((current.get('seen') or {}).get('listing_removed'))
        state = 'removed' if removed else decision.get('state')
        reasons = decision.get('reasons') or []
        if state not in ('removed', 'excluded', 'outside_search') or (state == 'excluded' and (not reasons or reasons == ['legacy_excluded'])):
            continue
        facts = {k: (v.get('value') if isinstance(v, dict) else v) for k, v in (current.get('facts') or {}).items()}
        old = legacy.get(listing) or {}
        parcel = facts.get('parcel_key')
        doc = {'listingId': listing, 'state': state, 'decisionReasons': [] if state == 'removed' else reasons, 'title': titles.get(listing) or old_titles.get(listing),
               'priceTl': facts.get('price_tl') or old.get('price'), 'areaM2': facts.get('sale_area_m2') or old.get('area'),
               'type': base.market_type(facts.get('property_type')) if facts.get('property_type') else old.get('type'),
               'neighborhood': neighborhood_of(store, listing) or old.get('neighborhood'),
               'links': {'listing': f'https://www.sahibinden.com/ilan/{listing}/detay', 'tkgm': (current.get('links') or {}).get('tkgm')}, 'updatedAt': now_iso}
        if parcel:
            doc['parcel'] = str(parcel)
        if state == 'removed':
            doc['removedAt'] = iso(gone_at.get(listing))
        out.append((COLLECTION, listing, {k: v for k, v in doc.items() if v is not None}))
    return out


def build(store, *, mode='local', now=None, limit=1000):
    import time
    now_iso = iso(time.time() if now is None else now)
    ranked = ranking(store, {'limit': limit}, mode=mode)
    from verda.agency import opportunity
    problems = []
    try:
        newer = {r['listing_id']: r['new'] for r in opportunity.rank_all(store, mode=mode)['ranked']}
    except Exception as error:                # the older score keeps being published, but never silently: the status page and the log say so
        import sys, traceback
        where = traceback.extract_tb(error.__traceback__)[-1]
        problems.append(f"Fırsat puanı hesaplanamadı ({type(error).__name__} · {where.name}:{where.lineno}); pano eski puanı gösteriyor")
        print('opportunity.rank_all failed:', type(error).__name__, f'{where.filename}:{where.lineno}', file=sys.stderr, flush=True)
        newer = {}
    docs = [(COLLECTION, row['listing_id'], listing_document(store, row, now_iso, mode=mode, new=newer.get(row['listing_id']))) for row in ranked['ranked']]
    docs += closed_documents(store, {d[1] for d in docs}, now_iso, mode=mode)
    by_state = {}
    for _, _, d in docs:
        by_state[d['state']] = by_state.get(d['state'], 0) + 1
    status = {'generatedAt': now_iso, 'recordCount': len(docs), 'byState': by_state, 'total': ranked['total'], 'settled': ranked['settled'], 'schemaVersion': 1}
    from verda.publish import status as status_page
    return docs + [('meta', 'v2', status), ('meta', 'v2status', status_page.build(store, mode=mode, now=now, problems=problems))]


def digest(data):
    return json.dumps({k: v for k, v in data.items() if k != 'updatedAt'}, sort_keys=True, ensure_ascii=False)
