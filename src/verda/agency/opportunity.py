"""The opportunity score: "is this a good buy for what this buyer wants", next to the older price-vs-peers score (which stays as it is).

1. Fair price: log(price per m2) ~ land type + size curve + neighbourhood (ridge, so small neighbourhoods lean on the island) fitted on one row per
   parcel. A listing's gap to its fair price is judged against how much asking prices normally scatter, so a small gap is "fair", not "dear".
2. Purpose fit: investment 40% / house 20% / production 40% (the buyer's own split), from facts the system knows. A fact it does not know scores
   in the middle and is shown as unknown; it never counts as bad.
3. Distance to the destination: 15% (was 30%).
Rules: above the ideal budget a listing needs a real discount to rank high; a very small parcel needs a large one. Hard gates stay in the rule engine.
"""
import math
import re
import statistics

from verda.agency import score as base

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
_COUNT = re.compile(r'(\d[\d\.]*)\s*(?:adet|tane|ağaç)?\s*(?:yaşlı|yetişkin|meyveli|asırlık|verimli|aşılı)?\s*(' + _TREES + r')\w*', re.I)
RIDGE_HOOD = 2.0
TYPES = ('Zeytinlik', 'Bademlik')                                # baseline: Tarla
LAND = ('Tarla', 'Zeytinlik', 'Bademlik')
BUILDING_HINT = re.compile(r'(yapı izni|yapı yapıl|ev yapıl|ev yapı|çiftlik evi|bağ evi|tarımsal yapı|yapılaşma|konut izn|imar izni|il tarım)', re.I)
WATER_HINT = re.compile(r'(su hat|kuyu|su mevcut|suyu var|sulama|su kaynağ|şebeke su)', re.I)
POWER_HINT = re.compile(r'(elektrik)', re.I)
_MH = re.compile(r'\s+(mh\.?|mah\.?|mahallesi)$', re.I)


def hood_of(name):
    return _MH.sub('', str(name or '')).strip()


def _features(kind, area, hood, hoods, types=TYPES):
    la = math.log(area)
    return [1.0, la, la * la / 10] + [1.0 if kind == k else 0.0 for k in types] + [1.0 if hood == h else 0.0 for h in hoods]


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


def fit(peers, types=TYPES):
    """-> predict(kind, area, hood, listing=None) -> fair TL per m2, and the scatter (sd of log residuals). None when there is too little data.

    A listing that is itself one of the fitted rows is judged without its own price (leave-one-out): otherwise a cheap listing pulls its own
    fair price down and looks less cheap than it is (131 of 142 ranked listings were in their own data, Oct 2026). Exact for this linear
    smoother: the prediction without row i is y_i - e_i / (1 - h_i), h_i the row's leverage."""
    data = [(p['type'], p['area'], hood_of(p['neighborhood']), p['rate'], p.get('weight', 1.0), p.get('ids') or set())
            for p in peers if p['type'] in LAND and p.get('neighborhood') and p['area'] > 0 and p['rate'] > 0]
    if len(data) < 30:
        return None, None
    hoods = sorted({h for _, _, h, _, _, _ in data})
    n = 3 + len(types) + len(hoods)
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    rows = []
    for kind, area, hood, rate, weight, ids in data:
        x, y = _features(kind, area, hood, hoods, types), math.log(rate)
        rows.append((x, y, weight, ids))
        for i in range(n):
            b[i] += weight * x[i] * y
            for j in range(n):
                A[i][j] += weight * x[i] * x[j]
    for i in range(n):
        A[i][i] += RIDGE_HOOD if i >= 3 + len(types) else 1e-6
    inverse = _solve(A, [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)])
    w = [sum(inverse[i][j] * b[j] for j in range(n)) for i in range(n)]
    own = {}                                                    # listing id -> log shift that removes its own row from its fair price
    for x, y, weight, ids in rows:
        fitted = sum(wi * xi for wi, xi in zip(w, x))
        leverage = weight * sum(x[i] * sum(inverse[i][j] * x[j] for j in range(n)) for i in range(n))
        if ids and leverage < 0.95:
            shift = (fitted - y) * leverage / (1 - leverage)
            own.update({i: shift for i in ids})

    def predict(kind, area, hood, listing=None):
        h = hood_of(hood)
        return math.exp(sum(wi * xi for wi, xi in zip(w, _features(kind, area, h if h in hoods else None, hoods, types))) + own.get(listing, 0.0))
    scatter = statistics.pstdev([y - sum(wi * xi for wi, xi in zip(w, x)) for x, y, _, _ in rows])
    return predict, scatter


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
        digits = m.group(1).replace('.', '')
        if digits.isdigit() and int(digits) < 5000:
            counts[m.group(2).lower()] = max(counts.get(m.group(2).lower(), 0), int(digits))
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


def listing_view(row, predict, scatter, text, access, sit_known, enclosed=False):
    """The new numbers for one ranked row (a dict from score.ranking); the old ones are left alone."""
    kind, area, price = row['type'], row['area_m2'], row['price_tl']
    road = road_state(access, enclosed)
    fair = predict(kind, area, row['neighborhood'], row.get('listing_id')) if predict and kind in LAND and area and price else None
    near = proximity(row['route_minutes_max'])
    if fair is None:
        purpose, parts = purpose_fit(kind, area or 0, road, text, sit_known)
        base_score = WEIGHTS['opportunity'] * 50 + WEIGHTS['purpose'] * purpose + WEIGHTS['proximity'] * near     # no price judgement: the middle
        return {'score': min(49, round(base_score)), 'opportunity': None, 'purpose': purpose, 'proximity': near, 'label': 'Belirsiz', 'road': road,
                'fair_unit_price': None, 'gap_pct': None, 'caps': [], 'parts': parts}
    fair_adj = fair
    rate = price / area
    gap_ln = math.log(fair_adj / rate)
    hood_effect = math.log(predict(kind, area, row['neighborhood']) / predict(kind, area, None))
    purpose, parts = purpose_fit(kind, area, road, text, sit_known, hood_effect)
    opp = opportunity_score(gap_ln)
    doubt = []
    if gap_ln > TOO_GOOD * scatter:
        # Counted as a strong opportunity and no more until a person has checked the price and the area against the listing and TKGM.
        opp, doubt = min(opp, opportunity_score(scatter)), ['çok ucuz görünüyor: fiyat ve alan doğrulanmalı']
    final, capped = combine(opp, purpose, near, price, area, gap_ln, row.get('penalties') or {})
    capped = doubt + capped
    if road == ENCLOSED:
        final, capped = max(0, final - ROAD_QUESTION_PENALTY), capped + ['yol: geçiş hakkı sorusu bekliyor −40']
    return {'score': final, 'opportunity': opp, 'purpose': purpose, 'proximity': near, 'label': label(gap_ln, scatter), 'road': road,
            'fair_unit_price': round(fair_adj), 'gap_pct': round(100 * (fair_adj - rate) / fair_adj, 1), 'caps': capped, 'parts': parts}


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
    gone = excluded_ids(store, mode)
    # Eliminated listings still show what land asks, but they may lack a road or carry other faults the buyer would not take: half weight.
    known = {}

    def status(listing):                                    # one fact read per listing for the whole ranking
        if listing not in known:
            known[listing] = fact_status(store, listing, mode=mode)
        return known[listing]
    peers = base.peers_from(base.peer_rows(store, mode, status))      # the old archive overlaid by what this system verified
    for p in peers:
        p['weight'] = 0.5 if p['ids'] <= gone else 1.0
    pooled, scatter = fit(peers)
    tarla, _ = fit([p for p in peers if p['type'] == 'Tarla'], types=())                         # tarla is judged against tarla only (enough parcels)

    def predict(kind, area, hood, listing=None):
        model = tarla if (kind == 'Tarla' and tarla) else pooled
        return model(kind, area, hood, listing) if model else None
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
        model = None if 'sale_scope' in (current.get('conflicts') or []) else predict
        shut = enclosed.get(row['listing_id'], False) and (current.get('seen') or {}).get('corridor_enclosed', True)    # False for a several-deed sale
        out.append({**row, 'new': listing_view(row, model, scatter, texts.get(row['listing_id']), facts.get('access'), sit_known, shut)})
    out.sort(key=lambda r: -r['new']['score'])
    return {'ranked': out, 'scatter': scatter}
