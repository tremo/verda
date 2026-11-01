"""The opportunity score: "is this a good buy for what this buyer wants", next to the older price-vs-peers score (which stays as it is).

1. Fair price: from comparables (agency/comparables.py): same-neighbourhood parcels with a known road when there are enough, otherwise the neighbourhood's
   level relative to the others, otherwise none. A listing's gap to its fair price is judged against how much asking prices normally scatter.
2. Purpose fit: investment 40% / house 20% / production 40% (the buyer's own split), from facts the system knows. A fact it does not know scores
   in the middle and is shown as unknown; it never counts as bad.
3. Distance to the destination: 15% (was 30%).
Rules: above the ideal budget a listing needs a real discount to rank high; a very small parcel needs a large one. Hard gates stay in the rule engine.
"""
import math
import re
import statistics

from verda.agency import comparables, score as base
from verda.facts.text import fold

WEIGHTS = {'opportunity': 0.45, 'purpose': 0.40, 'proximity': 0.15}
PURPOSE = {'investment': 0.40, 'house': 0.20, 'production': 0.40}
IDEAL_BUDGET, MAX_BUDGET = 9_000_000, 15_000_000
OVER_IDEAL_CAP, NEEDED_DISCOUNT_OVER_IDEAL = 60, 0.30          # above the ideal budget: capped unless this far below the fair price
SMALL_PARCEL, NEEDED_DISCOUNT_SMALL = 1000, 0.40
# A building claim in the listing text earns no price allowance and no bonus: asking prices show no reliable premium for it (+7-9%, interval -15..+37%, n=462),
# and since 4 Apr 2026 (bağ evi: Ek-1 md. 6, as amended 4 Aug 2026) almost no parcel can legally get one.
DIKILI_MIN_DONUM, ANY_CLASS_MIN_DONUM = 10, 20                  # bağ evi: dikili parcel from 10 dönüm, any other class from 20 dönüm (1 dönüm = 1000 m2)
HOUSE_SCORE = {'any_class': 80, 'dikili_dense': 50, 'dikili_unknown': 25, 'dikili_sparse': 0, 'too_small': 0}
OLIVE_FACTOR = 0.5                                              # zeytinlik: 3573 sayılı Kanun applies, olive trees cannot be removed without permit
# Ek-4 minimum trees per dönüm, lower end of each range (zeytin 28-55, badem kuru 27-30, ...); the class itself is decided by the il müdürlüğü
TREE_NORM = {'zeytin': 28, 'badem': 27, 'incir': 16, 'harnup': 21, 'keçiboynuzu': 21, 'ceviz': 10, 'nar': 40, 'kestane': 20, 'limon': 21, 'portakal': 27,
             'mandalin': 27, 'erik': 18, 'kayısı': 16, 'elma': 20, 'armut': 20, 'şeftali': 40, 'kiraz': 25, 'vişne': 18, 'dut': 20, 'avokado': 21}
_TREES = '|'.join(sorted(TREE_NORM, key=len, reverse=True))
_TREE_KEY = {fold(tree): tree for tree in TREE_NORM}
_THOUSANDS = re.compile(r'\d{1,3}(?:\.\d{3})+')


def _thousands(number):
    """A tree count as written: "1.500" is fifteen hundred, "300" is 300; "15.20" is no count at all and is left out rather than read as 1520."""
    if number.isdigit():
        return int(number)
    return int(number.replace('.', '')) if _THOUSANDS.fullmatch(number) else None
_COUNT = re.compile(r'(\d[\d\.]*)\s*(?:adet|tane|ağaç)?\s*(?:yaşlı|yetişkin|meyveli|asırlık|verimli|aşılı)?\s*(' + _TREES + r')\w*', re.I)
LAND = ('Tarla', 'Zeytinlik', 'Bademlik')
BUILDING_HINT = re.compile(r'(yapı izni|yapı yapıl|ev yapıl|ev yapı|çiftlik evi|bağ evi|tarımsal yapı|yapılaşma|konut izn|imar izni|il tarım)', re.I)
WATER_HINT = re.compile(r'(su hat|kuyu|su mevcut|suyu var|sulama|su kaynağ|şebeke su)', re.I)
POWER_HINT = re.compile(r'(elektrik)', re.I)
_MH = re.compile(r'\s+(mh\.?|mah\.?|mahallesi)$', re.I)


def hood_of(name):
    return _MH.sub('', str(name or '')).strip()


def _solve(A, B):
    """X with A X = B (Gauss-Jordan, partial pivoting). A: n x n, B: n x m. Small systems only (n is a few dozen)."""
    n, m = len(A), len(B[0])
    M = [A[i][:] + B[i][:] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        pivot = M[c][c]
        M[c] = [v / pivot for v in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [row[n:] for row in M]


def opportunity_score(gap_ln):
    return round(max(0, min(100, 50 + 60 * gap_ln)))


def label(gap_ln, scatter):
    """Only a gap beyond what asking prices normally scatter is called out."""
    if gap_ln is None:
        return 'Belirsiz'
    if gap_ln >= scatter:
        return 'Güçlü fırsat'
    if gap_ln >= 0.5 * scatter:
        return 'Olası fırsat'
    if gap_ln <= -scatter:
        return 'Pahalı'
    if gap_ln <= -0.5 * scatter:
        return 'Adilin üstünde'
    return 'Adil fiyat'


def road_state(access, enclosed=False):
    """`enclosed`: the official geometry check found only numbered neighbour parcels on every edge, i.e. no cadastral road frontage.
    An easement said by the seller or by the owner still counts (it costs the rule engine's 10 points); without one the road is missing."""
    value = (access or {}).get('value')
    if value == 'direct':
        return 'var'
    if value == 'existing_easement':
        return 'geçiş hakkı'
    if value == 'corridor_conditional':
        return 'koşullu (TKGM)'
    return 'yok (TKGM: kenarlar komşu parsel)' if enclosed else 'bilinmiyor'


ENCLOSED = 'yok (TKGM: kenarlar komşu parsel)'
# The easement penalty (-10) comes from the rule engine, so it is not taken off a second time here.
ROAD_SCORE = {'var': 100, 'geçiş hakkı': 100, 'koşullu (TKGM)': 60, 'bilinmiyor': 45, ENCLOSED: 10}
ROAD_QUESTION_PENALTY = 40            # until the seller says whether an easement exists (yes: the rule engine's -10 takes over; no: the gate removes the listing)


def tree_density(text, area):
    """Stated trees against the Ek-4 minimums: 1.0 = exactly the minimum density; None when the text names no counts or the area is unknown."""
    counts = {}
    for m in _COUNT.finditer(re.sub(r'\s+', ' ', text or '')):
        tree = _TREE_KEY.get(fold(m.group(2)))                     # "ZEYTİN" and "zeytin" are one tree
        digits = _thousands(m.group(1))
        if tree and digits is not None and digits < 5000:
            counts[tree] = max(counts.get(tree, 0), digits)
    return sum(n / (TREE_NORM[t] * area / 1000) for t, n in counts.items()) if counts and area else None


def house_potential(kind, area, text=''):
    """0-100: the chance of a legal bağ evi from the parcel's size and, between 10 and 20 dönüm, from its tree density (the dikili class)."""
    if area >= ANY_CLASS_MIN_DONUM * 1000:
        base = HOUSE_SCORE['any_class']
    elif area >= DIKILI_MIN_DONUM * 1000:
        density = tree_density(text, area)
        base = HOUSE_SCORE['dikili_unknown'] if density is None else HOUSE_SCORE['dikili_dense'] if density >= 1 else HOUSE_SCORE['dikili_sparse']
    else:
        base = HOUSE_SCORE['too_small']
    return base * (OLIVE_FACTOR if kind == 'Zeytinlik' else 1.0)


def purpose_fit(kind, area, road, text, sit_known, hood_effect=0.0):
    """-> (score, parts). Each part is 0-100; unknown things sit in the middle."""
    text = text or ''
    hint, water, power = bool(BUILDING_HINT.search(text)), bool(WATER_HINT.search(text)), bool(POWER_HINT.search(text))
    size = 40 if area < 2000 else 55 if area < 3000 else 70 if area < 5000 else 85 if area < 10000 else 92
    investment = 0.35 * (100 if sit_known else 55) + 0.25 * ROAD_SCORE[road] + 0.20 * size + 0.20 * max(0, min(100, 55 + 60 * hood_effect))
    house = house_potential(kind, area, text)
    # the buyer's order of land types: tarla > bademlik > zeytinlik
    production = 0.35 * {'Tarla': 100, 'Bademlik': 80, 'Zeytinlik': 60}.get(kind, 50) + 0.30 * ROAD_SCORE[road] + 0.15 * (55 + (25 if water else 0) + (10 if power else 0)) + 0.20 * size
    total = PURPOSE['investment'] * investment + PURPOSE['house'] * house + PURPOSE['production'] * production
    return round(total), {'investment': round(investment), 'house': round(house), 'production': round(production), 'building_hint': hint, 'water_hint': water, 'power_hint': power}


def combine(opportunity, purpose, proximity, price, area, gap_ln, penalties):
    base = WEIGHTS['opportunity'] * opportunity + WEIGHTS['purpose'] * purpose + WEIGHTS['proximity'] * proximity
    capped = []
    if price and price > IDEAL_BUDGET and (gap_ln is None or gap_ln < -math.log(1 - NEEDED_DISCOUNT_OVER_IDEAL)):
        base = min(base, OVER_IDEAL_CAP); capped.append('bütçe')
    if area and area < SMALL_PARCEL and (gap_ln is None or gap_ln < -math.log(1 - NEEDED_DISCOUNT_SMALL)):
        base = min(base, 55); capped.append('küçük parsel')
    taken = sum(penalties.values())
    return max(0, round(max(20, min(99, base)) - taken)), capped


TOO_GOOD = 2.0              # a gap beyond twice the usual scatter is more often a data error (wrong area, a share, a price typo) than a bargain
UNKNOWN_ROUTE = 50          # an unknown drive time sits in the middle, like every other unknown here (the old score's 20 counted it as bad)


def proximity(route_minutes_max):
    return UNKNOWN_ROUTE if route_minutes_max is None else base.proximity(route_minutes_max)


def listing_view(row, estimate, scatter, text, access, sit_known, enclosed=False, hood_effect=0.0):
    """The new numbers for one ranked row (a dict from score.ranking); the old ones are left alone. `estimate`: comparables.Estimator.estimate, or None."""
    kind, area, price = row['type'], row['area_m2'], row['price_tl']
    road = road_state(access, enclosed)
    fair = (estimate or {}).get('fair') if kind in LAND and area and price else None
    near = proximity(row['route_minutes_max'])
    basis = _basis(estimate) if estimate else None
    if fair is None:
        purpose, parts = purpose_fit(kind, area or 0, road, text, sit_known)
        base_score = WEIGHTS['opportunity'] * 50 + WEIGHTS['purpose'] * purpose + WEIGHTS['proximity'] * near     # no price judgement: the middle
        score, caps = min(49, round(base_score)), []
        taken = sum((row.get('penalties') or {}).values())            # the rule engine's point cuts (second-degree sit, easement) count with or without a price judgement
        if taken:
            score, caps = max(0, score - taken), caps + [f'kural cezaları −{taken}']
        if road == ENCLOSED:
            score, caps = max(0, score - ROAD_QUESTION_PENALTY), caps + ['yol: geçiş hakkı sorusu bekliyor −40']
        return {'score': score, 'opportunity': None, 'purpose': purpose, 'proximity': near, 'label': 'Belirsiz', 'road': road,
                'fair_unit_price': None, 'gap_pct': None, 'caps': caps, 'parts': parts, 'basis': basis}
    rate = price / area
    gap_ln = math.log(fair / rate)
    purpose, parts = purpose_fit(kind, area, road, text, sit_known, hood_effect)
    opp = opportunity_score(gap_ln)
    doubt = []
    if gap_ln > TOO_GOOD * scatter:
        # Counted as a strong opportunity and no more until a person has checked the price and the area against the listing and TKGM.
        opp, doubt = min(opp, opportunity_score(scatter)), ['çok ucuz görünüyor: fiyat ve alan doğrulanmalı']
    if estimate['tier'] == 'region':
        # A regional estimate is only a level: a gap smaller than its own range is not a signal, and never a "strong" one.
        opp = min(opp, opportunity_score(0.5 * scatter))
    final, capped = combine(opp, purpose, near, price, area, gap_ln, row.get('penalties') or {})
    capped = doubt + capped
    if road == ENCLOSED:
        final, capped = max(0, final - ROAD_QUESTION_PENALTY), capped + ['yol: geçiş hakkı sorusu bekliyor −40']
    shown = label(gap_ln, scatter)
    if estimate['tier'] == 'region' and shown in ('Güçlü fırsat', 'Olası fırsat'):
        shown = 'Adil fiyat'
    return {'score': final, 'opportunity': opp, 'purpose': purpose, 'proximity': near, 'label': shown, 'road': road,
            'fair_unit_price': round(fair), 'gap_pct': round(100 * (fair - rate) / fair, 1), 'caps': capped, 'parts': parts, 'basis': basis}


def _basis(estimate):
    """What the fair price rests on, for the dashboard: the step used, the range and the comparables with their links."""
    out = {'tier': estimate['tier'], 'note': estimate.get('note'), 'n': estimate.get('n')}
    if estimate.get('low') is not None:
        out['low'], out['high'] = round(estimate['low']), round(estimate['high'])
    for key in ('effect_pct', 'reference'):
        if key in estimate:
            out[key] = estimate[key]
    if estimate['tier'] == 'local':
        out['comps'] = [{'ids': c['ids'], 'rate': c['rate'], 'area': c['area'], 'adjusted': c['adjusted']} for c in estimate.get('comps', [])]
    return out


def excluded_ids(store, mode='local'):
    """Listings the old system or this one has eliminated."""
    out = set()
    with store.transaction() as con:
        for r in con.execute("SELECT a.listing_ref FROM assessments a JOIN (SELECT listing_ref, max(created_at) m FROM assessments WHERE mode=? GROUP BY 1) x "
                             "ON a.listing_ref=x.listing_ref AND a.created_at=x.m WHERE a.mode=? AND a.state='excluded'", (mode, mode)):
            out.add(r[0])
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if archive:
        try:
            snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
            if snapshot:
                out |= {r['listing_id'] for r in archive.execute("SELECT listing_id FROM cases WHERE snapshot_id=? AND lifecycle='excluded'", (snapshot['id'],))}
        except Exception:
            pass
    return out


def rank_all(store, *, mode='local'):
    """The ranked rows with their new view, best first. Reads facts and listing texts from the records."""
    import json
    from verda.agency.research import fact_status
    known = {}

    def status(listing):                                    # one fact read per listing for the whole ranking
        if listing not in known:
            known[listing] = fact_status(store, listing, mode=mode)
        return known[listing]
    peers = comparables.roads(store, base.peers_from(base.peer_rows(store, mode, status)), known)      # the old archive overlaid by what this system verified
    estimator = comparables.Estimator(peers, hood_of, LAND, _solve)
    scatter = estimator.sigma
    mean_effect = statistics.fmean(estimator.effects.values()) if estimator.effects else 0.0
    ranked = base.ranking(store, {'limit': 1000}, mode=mode, status=status)['ranked']
    texts = {}
    with store.transaction() as con:
        for r in con.execute("SELECT listing_ref, payload FROM observations WHERE tool='sahibinden.read_listing' AND mode=? ORDER BY captured_at,id", (mode,)):
            try:
                texts[r['listing_ref']] = (json.loads(r['payload']).get('description') or '')
            except ValueError:
                pass
    enclosed = {}
    with store.transaction() as con:
        for r in con.execute("SELECT listing_ref, payload FROM observations WHERE tool='tkgm.corridor' AND mode=? ORDER BY captured_at,id", (mode,)):
            try:
                enclosed[r['listing_ref']] = json.loads(r['payload']).get('verdict') == 'all_edges_numbered_neighbors'
            except ValueError:
                pass
    out = []
    for row in ranked:
        current = status(row['listing_id'])
        facts = current.get('facts') or {}
        sit_known = 'natural_sit' in facts and 'archaeological_sit' in facts
        # An area in dispute (the official parcel is not what the listing sells) gives a price per m2 that means nothing: no fair-price gap.
        estimate = None if 'sale_scope' in (current.get('conflicts') or []) else estimator.estimate(row['type'], row['area_m2'], row['neighborhood'], row['listing_id'])
        effect = estimator.effects.get(hood_of(row['neighborhood']), mean_effect) - mean_effect       # the neighbourhood against the island's average
        shut = enclosed.get(row['listing_id'], False) and (current.get('seen') or {}).get('corridor_enclosed', True)    # False for a several-deed sale
        out.append({**row, 'new': listing_view(row, estimate, scatter, texts.get(row['listing_id']), facts.get('access'), sit_known, shut, effect)})
    out.sort(key=lambda r: -r['new']['score'])
    return {'ranked': out, 'scatter': scatter}
