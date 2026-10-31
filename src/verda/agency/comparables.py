"""Fair price from comparables, in three steps that say which one they used.

1. Local: parcels of the same type in the same neighbourhood, from half to twice the size, with a road we know about. Their asking prices per m2, brought
   to this size with the size slope, give the fair price (median) and the usual range (quartiles). Needs MIN_LOCAL of them.
2. Regional: too few local ones. A log-linear fit (type + size + neighbourhood) over the parcels whose road is not known to be missing gives the
   neighbourhood's level relative to the others; the fair price is its prediction, with a range from how far asking prices scatter. Wider and labelled so.
3. None: the neighbourhood or the size is outside what the data covers. No number is made up.

Road: only parcels with a road we know about (cadastral road, easement) are local comparables; a parcel known to lack one is in neither step.
"""
import json
import math
import statistics

MIN_LOCAL = 4
SIZE_BAND = (0.5, 2.0)
MIN_HOOD_ROWS = 5                # fewer rows than this and the neighbourhood's level is not estimated
RANGE_OF_DATA = (0.5, 2.0)       # the regional fit is not stretched past half the smallest or twice the largest parcel it has seen, per type
SLOPE_LIMITS = (-0.8, 0.0)       # bigger parcels never ask more per m2 here; the fit is kept inside what the market plausibly does
ROAD_OK, ROAD_BAD = 'ok', 'bad'


def roads(store, peers, statuses):
    """Sets peer['road'] to 'ok', 'bad' or None (unknown). `statuses`: listing id -> fact status for the listings this system has looked at.
    The old archive's neighbour-access checks fill the rest; one parcel with several listings is bad if any says so."""
    legacy = _legacy_roads(store)
    for peer in peers:
        seen = set()
        for listing in peer['ids']:
            access = (((statuses.get(listing) or {}).get('facts') or {}).get('access') or {}).get('value')
            if access in ('direct', 'existing_easement'):
                seen.add(ROAD_OK)
            elif access in ('none', 'absent', 'no_access'):
                seen.add(ROAD_BAD)
            if legacy.get(listing):
                seen.add(legacy[listing])
        peer['road'] = ROAD_BAD if ROAD_BAD in seen else ROAD_OK if ROAD_OK in seen else None
    return peers


def _legacy_roads(store):
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return {}
    out = {}
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        if not snapshot:
            return {}
        for row in archive.execute("SELECT payload FROM legacy_rows WHERE table_name='checks' AND snapshot_id=?", (snapshot['id'],)):
            check = json.loads(row['payload'])
            if check.get('kind') == 'neighbor_access':
                status = str(check.get('status') or '')
                out[check['item_id']] = ROAD_OK if status.startswith('verified') else ROAD_BAD if status == 'excluded_by_statement' else None
        for row in archive.execute("SELECT subject_id,payload FROM legacy_rows WHERE table_name='items' AND snapshot_id=?", (snapshot['id'],)):
            item = json.loads(row['payload'])
            reason = str(json.loads(item.get('legacy_json') or '{}').get('exclusion_reason') or '')
            if item.get('lifecycle') == 'excluded' and 'road_absent' in reason:
                out[row['subject_id']] = ROAD_BAD
    finally:
        archive.close()
    return {k: v for k, v in out.items() if v}


def _pct(effect):
    value = round(100 * (math.exp(effect) - 1))
    return f'%{value}' if value >= 0 else f'−%{-value}'


class Estimator:
    """Built once per ranking from the peers (dicts with type, area, rate, neighborhood, ids, road)."""

    def __init__(self, peers, hood_of, lands, solve):
        self.hood_of, self.lands = hood_of, lands
        rows = [(p, hood_of(p['neighborhood'])) for p in peers
                if p['type'] in lands and p.get('neighborhood') and p['area'] > 0 and p['rate'] > 0 and p.get('road') != ROAD_BAD]
        self.rows = rows
        self.slope, self.sigma, self.effects, self.kinds, self.leverage_shift = -0.55, 0.5, {}, [], {}
        self.reference = None
        self.span = {}
        self.w, self.others, self.hoods = None, [], set()
        self._fit(solve)

    def _fit(self, solve):
        rows = self.rows
        count = {}
        for _, hood in rows:
            count[hood] = count.get(hood, 0) + 1
        hoods = sorted(h for h, n in count.items() if n >= MIN_HOOD_ROWS)
        if len(rows) < 30 or len(hoods) < 2:
            return
        self.reference = max(hoods, key=lambda h: count[h])
        others = [h for h in hoods if h != self.reference]
        self.kinds = [k for k in ('Zeytinlik', 'Bademlik') if any(p['type'] == k for p, _ in rows)]
        fit_rows = [(p, h) for p, h in rows if h in hoods]
        width = 2 + len(self.kinds) + len(others)
        A = [[0.0] * width for _ in range(width)]
        b = [0.0] * width
        xs = []
        for p, h in fit_rows:
            x = self._features(p['type'], p['area'], h, others)
            y = math.log(p['rate'])
            xs.append((x, y, p))
            for i in range(width):
                b[i] += x[i] * y
                for j in range(width):
                    A[i][j] += x[i] * x[j]
        for i in range(width):
            A[i][i] += 1e-6
        inverse = solve(A, [[1.0 if i == j else 0.0 for j in range(width)] for i in range(width)])
        w = [sum(inverse[i][j] * b[j] for j in range(width)) for i in range(width)]
        self.slope = min(SLOPE_LIMITS[1], max(SLOPE_LIMITS[0], w[1]))
        self.w, self.others, self.hoods = w, others, set(hoods)
        self.effects = {self.reference: 0.0, **{h: w[2 + len(self.kinds) + i] for i, h in enumerate(others)}}
        residuals = [y - sum(wi * xi for wi, xi in zip(w, x)) for x, y, _ in xs]
        self.sigma = max(0.2, statistics.pstdev(residuals))
        for x, y, p in xs:                                    # a listing is judged without its own row (leave-one-out, exact for this linear fit)
            leverage = sum(x[i] * sum(inverse[i][j] * x[j] for j in range(width)) for i in range(width))
            if leverage < 0.95:
                shift = (sum(wi * xi for wi, xi in zip(w, x)) - y) * leverage / (1 - leverage)
                for listing in p['ids']:
                    self.leverage_shift[listing] = shift
        for p, _ in fit_rows:
            lo, hi = self.span.get(p['type'], (p['area'], p['area']))
            self.span[p['type']] = (min(lo, p['area']), max(hi, p['area']))

    def _features(self, kind, area, hood, others):
        return [1.0, math.log(area)] + [1.0 if kind == k else 0.0 for k in self.kinds] + [1.0 if hood == h else 0.0 for h in others]

    def estimate(self, kind, area, hood, listing=None):
        """-> {tier: local|region|none, fair (TL/m2), low, high, n, comps, note}."""
        hood = self.hood_of(hood)
        if kind not in self.lands or not area or not hood:
            return {'tier': 'none', 'fair': None, 'note': 'Tür, alan ya da mahalle bilinmiyor'}
        comps = []
        for p, h in self.rows:
            if h != hood or p['type'] != kind or p.get('road') != ROAD_OK or listing in p['ids']:
                continue
            if SIZE_BAND[0] * area <= p['area'] <= SIZE_BAND[1] * area:
                comps.append({'ids': sorted(p['ids']), 'rate': round(p['rate']), 'area': round(p['area']), 'adjusted': p['rate'] * (area / p['area']) ** self.slope})
        if len(comps) >= MIN_LOCAL:
            adjusted = sorted(c['adjusted'] for c in comps)
            quartiles = statistics.quantiles(adjusted, n=4, method='inclusive')
            comps.sort(key=lambda c: abs(math.log(c['area'] / area)))
            for c in comps:
                c['adjusted'] = round(c['adjusted'])
            return {'tier': 'local', 'fair': statistics.median(adjusted), 'low': quartiles[0], 'high': quartiles[2], 'n': len(comps), 'comps': comps[:12],
                    'note': f'{hood} mahallesinde, yolu bilinen {len(comps)} benzer parselin (yarım ile iki katı büyüklük) fiyatlarının ortancası'}
        if not self.effects or hood not in self.hoods:
            return {'tier': 'none', 'fair': None, 'n': len(comps), 'comps': comps, 'note': f'{hood} için yeterli emsal ve bölge katsayısı yok'}
        low, high = self.span.get(kind, (None, None))
        if low is None or not RANGE_OF_DATA[0] * low <= area <= RANGE_OF_DATA[1] * high:
            return {'tier': 'none', 'fair': None, 'n': len(comps), 'comps': comps, 'note': 'Parsel, verideki büyüklük aralığının çok dışında'}
        w = self.w
        x = self._features(kind, area, hood, self.others)
        x[1] = math.log(area)
        value = sum(wi * xi for wi, xi in zip(w, x)) + self.leverage_shift.get(listing, 0.0)
        return {'tier': 'region', 'fair': math.exp(value), 'low': math.exp(value - self.sigma), 'high': math.exp(value + self.sigma), 'n': len(comps),
                'comps': [{k: v for k, v in c.items() if k != 'adjusted'} for c in comps],
                'effect_pct': round(100 * (math.exp(self.effects[hood]) - 1)), 'reference': self.reference,
                'note': f'{hood} için aynı mahallede yeterli emsal yok ({len(comps)}); bölge katsayısıyla tahmin ({self.reference}\'ye göre {_pct(self.effects[hood])})'}
