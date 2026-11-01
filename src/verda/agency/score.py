"""Purchase score and ranking: the old dashboard's model on this system's verified facts. Pure calculation.

value (70%): asking-price benchmark of comparable listings (same type, area ratio 0.5-2, same neighbourhood first),
discount to the benchmark -> 50 + 2 * discount, clamped 0-100. proximity (30%): the longest drive to the
destination, 45 min -> 0, 10 min -> 100. Without a benchmark the value is 30 and the score is capped at 49. The rule
engine's penalties (natural sit, existing right of way) come off afterwards, so they stay an exact, visible reduction.
An excluded listing has no score: a score never overrides a hard gate.
"""
import json
import math
import statistics

from verda.facts.text import fold

WEIGHTS = {'value': 0.70, 'proximity': 0.30}
MIN_LOCAL_PEERS, MIN_GENERAL_PEERS = 5, 8
RATE_RANGE = (50, 250_000)


def market_type(official_type, title=None):
    official = str(official_type or '').strip()
    text = fold(official) if official and official != '—' else fold(title or '')      # folded: "ZEYTİNLİK" is "zeytinlik"
    for needle, label in (('zeytin', 'Zeytinlik'), ('badem', 'Bademlik')):
        if needle in text:
            return label
    if 'arsa' in text or 'imar' in text:
        return 'Arsa / imarlı'
    if 'tarla' in text:
        return 'Tarla'
    return 'Diğer / belirsiz'


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    pos = (len(ordered) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def trim_outliers(peers):
    if len(peers) < 6:
        return peers
    rates = [c['rate'] for c in peers]
    q1, q3 = percentile(rates, 0.25), percentile(rates, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return peers
    trimmed = [c for c in peers if q1 - 1.5 * iqr <= c['rate'] <= q3 + 1.5 * iqr]
    return trimmed if len(trimmed) >= 5 else peers


def proximity(route_minutes_max):
    if route_minutes_max is None:
        return 20
    return round(max(0, min(100, 100 * (45 - route_minutes_max) / 35)))


def benchmark(subject, peers):
    """The value side for one listing, or None when there are not enough comparables.

    subject: {id, price, area, type, neighborhood}; peers: [{key, ids, rate, area, type, neighborhood}] (one per parcel).
    """
    if not (subject['price'] and subject['area'] and subject['area'] > 0) or subject['type'] == 'Diğer / belirsiz':
        return None
    pool = [p for p in peers if subject['id'] not in p['ids'] and p['type'] == subject['type'] and 0.5 <= p['area'] / subject['area'] <= 2.0]
    local = [p for p in pool if subject['neighborhood'] and p['neighborhood'] == subject['neighborhood']]
    if len(local) >= MIN_LOCAL_PEERS:
        chosen, scope, confidence = trim_outliers(local), f"{subject['neighborhood']} · {subject['type']}", None
        confidence = 'Yüksek' if len(chosen) >= 8 else 'Orta'
    elif len(pool) >= MIN_GENERAL_PEERS:
        chosen, scope, confidence = trim_outliers(pool), f"Datça geneli · {subject['type']}", 'Düşük'
    else:
        return None
    rate = statistics.median(p['rate'] for p in chosen)
    estimated = rate * subject['area']
    discount = 100 * (estimated - subject['price']) / estimated
    return {'benchmark_unit_price': round(rate, 2), 'estimated_price': round(estimated), 'discount_pct': round(discount, 1),
            'comparables': len(chosen), 'scope': scope, 'confidence': confidence,
            'value_score': round(max(0, min(100, 50 + 2 * discount)))}


def peers_from(rows):
    """One peer per parcel (a parcel listed twice counts once, by median), from rows {id, price, area, type, neighborhood, parcel}."""
    groups = {}
    for r in rows:
        if not (r['price'] and r['area'] and r['area'] > 0) or not RATE_RANGE[0] <= r['price'] / r['area'] <= RATE_RANGE[1]:
            continue
        key = f"{r['neighborhood']}|{r['parcel']}" if r.get('neighborhood') and r.get('parcel') else f"id:{r['id']}"
        groups.setdefault(key, []).append(r)
    return [{'key': k, 'ids': {x['id'] for x in g}, 'rate': statistics.median(x['price'] / x['area'] for x in g),
             'area': statistics.median(x['area'] for x in g), 'type': g[0]['type'], 'neighborhood': g[0]['neighborhood']}
            for k, g in groups.items()]


def purchase_score(subject, peers, route_minutes_max, penalties):
    """None for an excluded listing (callers do not pass one). Returns the score and everything that explains it."""
    value = benchmark(subject, peers)
    value_score = value['value_score'] if value else 30
    near = proximity(route_minutes_max)
    base = round(WEIGHTS['value'] * value_score + WEIGHTS['proximity'] * near)
    if value is None:
        base = min(base, 49)
    base = max(20, min(99, base))
    taken = sum(penalties.values())
    return {'score': max(0, base - taken), 'base_score': base, 'penalty_points': taken, 'penalties': dict(penalties),
            'value': value, 'value_score': value_score, 'proximity_score': near,
            'valuation': 'benchmark' if value else 'no_benchmark_capped_49'}


def _legacy_rows(store):
    """Peer rows from the old archive. Rows the old system excluded for a sit are left out, as the old model did."""
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return []
    out = []
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        for row in archive.execute("SELECT subject_id,payload FROM legacy_rows WHERE table_name='items' AND snapshot_id=?", (snapshot['id'],)) if snapshot else []:
            item = json.loads(row['payload'])
            data = json.loads(item.get('legacy_json') or '{}')
            if item.get('lifecycle') == 'excluded' and 'sit' in fold(str(data.get('exclusion_reason') or '')).split():
                continue
            out.append({'id': row['subject_id'], 'price': data.get('price_tl'), 'area': data.get('official_area_m2') or data.get('listing_area_m2'),
                        'type': market_type(data.get('official_type'), data.get('title')), 'neighborhood': data.get('neighborhood'),
                        'parcel': item.get('parcel_key') if item.get('parcel_key') not in (None, 'None') else None})
    finally:
        archive.close()
    return [r for r in out if isinstance(r['price'], (int, float)) and isinstance(r['area'], (int, float))]


def _verified_row(store, listing, current):
    """One listing as this system knows it: price, sale area, type, place, parcel."""
    from verda.agency.queue import neighborhood_of
    facts = {k: (v.get('value') if isinstance(v, dict) else v) for k, v in (current.get('facts') or {}).items()}
    return {'id': listing, 'price': facts.get('price_tl'), 'area': facts.get('sale_area_m2'), 'type': market_type(facts.get('property_type')),
            'neighborhood': neighborhood_of(store, listing), 'parcel': facts.get('parcel_key')}


def _overlay(rows, listing, row, current):
    """What this system verified replaces the old record. A listing whose sale area is in dispute (the official parcel is not what it
    sells) is left out of the comparison altogether: its price per m2 means nothing, under either record."""
    if 'sale_scope' in (current.get('conflicts') or []):
        rows.pop(listing, None)
    elif isinstance(row['price'], (int, float)) and isinstance(row['area'], (int, float)):
        rows[listing] = row


def peer_rows(store, mode, status):
    """Asking prices to compare against: the old archive overlaid by every listing this system has assessed (the same rows `ranking` uses)."""
    rows = {r['id']: r for r in _legacy_rows(store)}
    with store.transaction() as con:
        listings = [r[0] for r in con.execute('SELECT DISTINCT listing_ref FROM assessments WHERE mode=? ORDER BY listing_ref', (mode,))]
    for listing in listings:
        current = status(listing)
        _overlay(rows, listing, _verified_row(store, listing, current), current)
    return list(rows.values())


def _titles(store, mode):
    """The title of each listing's latest read, for the list; a listing never read has none."""
    with store.transaction() as con:
        rows = con.execute("""SELECT listing_ref, json_extract(payload, '$.title') AS title FROM observations
            WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref IS NOT NULL ORDER BY captured_at, id""", (mode,)).fetchall()
    return {r['listing_ref']: r['title'] for r in rows if isinstance(r['title'], str) and r['title'].strip()}


def ranking(store, args, *, mode, status=None):
    """Listings that are not excluded, best score first. Provisional until every screening fact is in."""
    from verda.agency.queue import neighborhood_of
    from verda.agency.research import fact_status
    status = status or (lambda listing: fact_status(store, listing, mode=mode))
    limit = int(args.get('limit', 20))
    with store.transaction() as con:
        listings = [r[0] for r in con.execute('SELECT DISTINCT listing_ref FROM assessments WHERE mode=? ORDER BY listing_ref', (mode,))]
    from verda.facts.flags import open_flags
    with store.transaction() as con:
        flagged = {f['listing_ref'] for f in open_flags(con, None, mode)}
    rows, subjects = {r['id']: r for r in _legacy_rows(store)}, []
    for listing in listings:
        current = status(listing)
        decision = current.get('decision') or {}
        row = _verified_row(store, listing, current)
        _overlay(rows, listing, row, current)
        if decision.get('state') in {'excluded', 'outside_search'} or current.get('legacy_lifecycle') == 'excluded' and not decision:
            continue
        if (current.get('next_action') or {}).get('why') == 'listing_removed':
            continue
        minutes = [m['value'] for m in (current.get('facts') or {}).get('route_minutes') or [] if isinstance(m, dict)]
        subjects.append((row, decision, max(minutes) if minutes else None, current))
    peers = peers_from(list(rows.values()))
    from verda.agency.learning import build, context_of, gather, summary, unknown_fields
    from verda.agency.listing_dates import collect, legacy_titles
    from verda.agency.research import archive_connection
    titles, when, old_titles = _titles(store, mode), collect(store, mode), legacy_titles(store)
    archive = archive_connection(store) if mode == 'local' else None
    snapshot = None
    if archive:
        found = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        snapshot = found['id'] if found else None
    ranked = []
    for row, decision, route, current in subjects:
        if decision.get('state') in {'excluded', 'outside_search'}:
            continue
        # what is being done about what is still unknown, for the listings that are not settled yet
        act = summary(build(gather(store, row['id'], mode, archive=archive, snapshot=snapshot), unknown_fields(current), current.get('next_action'), context_of(current))) \
            if decision.get('state') == 'research_required' else None
        result = purchase_score(row, peers, route, decision.get('penalties') or {})
        ranked.append({'listing_id': row['id'], 'title': titles.get(row['id']) or old_titles.get(row['id']), 'score': result['score'],
                       'published_at': (when.get(row['id']) or {}).get('published_at'), 'discovered_at': (when.get(row['id']) or {}).get('discovered_at'),
                       'discovered_by': (when.get(row['id']) or {}).get('discovered_by'), 'action': act, 'provisional': decision.get('state') not in {'eligible', 'conditional'},
                       'state': decision.get('state'), 'missing': decision.get('reasons') if decision.get('state') == 'research_required' else [],
                       'price_tl': row['price'], 'area_m2': row['area'], 'neighborhood': row['neighborhood'], 'type': row['type'],
                       'route_minutes_max': route, 'valuation': result['valuation'], 'discount_pct': (result['value'] or {}).get('discount_pct'),
                       'comparables': (result['value'] or {}).get('comparables', 0), 'penalties': result['penalties'],
                       'benchmark_unit_price': (result['value'] or {}).get('benchmark_unit_price'),
                       'benchmark_scope': (result['value'] or {}).get('scope'), 'confidence': (result['value'] or {}).get('confidence'),
                       'needs_clarification': row['id'] in flagged})
    if archive:
        archive.close()
    ranked.sort(key=lambda r: (r['provisional'], -r['score']))        # settled listings first, then by score
    return {'ranked': ranked[:limit], 'total': len(ranked), 'settled': sum(not r['provisional'] for r in ranked),
            'note': 'Emsal, ilan fiyatlarına dayalı bir tahmindir; resmî değerleme değildir. Geçici puanlar eksik doğrulama içerir.'}
