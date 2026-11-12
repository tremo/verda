"""What a parcel of this size in this place is usually asked for: the yardstick an asking price is held against.

Measured on the records (213 parcels with an outline, Nov 2026): asking prices per m² are explained by the parcel's size (a third of the spread on
its own; twice the size, about 30 % less per m²) and by its neighbourhood, and a little by the distance to the sea and the height. Slope, the side
the land faces, water, a sea view, the road and the land type were not priced in any measurable way. So this expectation uses only what sellers
visibly price; everything else is left to the quality score (opportunity.purpose_fit), which is where a buyer finds what the price does not show.

A ridge-regularised, outlier-resistant linear fit on the logarithm of the price per m². Every listing is judged by a fit that has not seen its own
price (leave-one-out). These are asking prices, not sale prices: the result is a reference, never a valuation.
"""
import math
import statistics

from verda.agency.comparables import neighborhood

METHOD = 'datca-value-v1'
MIN_ROWS = 40                   # fewer parcels than this and no expectation is made at all
MIN_HOOD_ROWS = 4               # a neighbourhood's own level is learned from at least this many parcels
HOOD_PENALTY = 1.5              # pulls a level learned from few parcels towards the overall one
TERRAIN_PENALTY = 1.0
REF_AREA, REF_COAST, REF_ELEVATION = 3000.0, 2000.0, 120.0
AREA_RANGE, RATE_RANGE = (300.0, 30000.0), (50.0, 250_000.0)
ROBUST_AT = 1.5                 # a price further than this many scales from the fit counts for less
UNPRICED = 'Eğim, bakı, su, manzara ve yol istenen fiyata ölçülebilir biçimde yansımıyor; bunlar kalite puanında değerlendirilir.'


def rows_from(peer_rows, lands, land_of):
    """One row per parcel from the ranking's price rows ({id, price, area, type, neighborhood, parcel}); a parcel under several listings counts once,
    by its median. `land_of`: {listing id: terrain measurement} (coast_m, elevation_m), where there is one."""
    groups = {}
    for r in peer_rows:
        if r.get('type') not in lands or not r.get('neighborhood') or not isinstance(r.get('price'), (int, float)) or not isinstance(r.get('area'), (int, float)):
            continue
        if not (AREA_RANGE[0] <= r['area'] <= AREA_RANGE[1]) or not (RATE_RANGE[0] <= r['price'] / r['area'] <= RATE_RANGE[1]):
            continue
        hood = neighborhood(r['neighborhood'])
        key = f"{hood}|{r['parcel']}" if r.get('parcel') else f"id:{r['id']}"
        groups.setdefault(key, []).append({**r, 'hood': hood})
    out = []
    for group in groups.values():
        lands_here = [land_of[g['id']] for g in group if g['id'] in land_of]
        land = lands_here[-1] if lands_here else {}
        out.append({'ids': {g['id'] for g in group}, 'hood': group[0]['hood'], 'neighborhood': group[0]['neighborhood'], 'type': group[0]['type'],
                    'area': statistics.median(g['area'] for g in group), 'rate': statistics.median(g['price'] / g['area'] for g in group),
                    'coast_m': land.get('coast_m'), 'elevation_m': land.get('elevation_m')})
    return out


def _coast(value):
    return math.log(max(float(value), 100.0) / REF_COAST)


def _elevation(value):
    return (float(value) - REF_ELEVATION) / 100.0


class Model:
    """Built once per ranking. `solve(A, B)` solves A X = B (opportunity._solve)."""

    def __init__(self, rows, solve):
        self.rows, self.solve = rows, solve
        self.w, self.hoods, self.scatter, self.loo, self.counts = None, [], 0.5, {}, {}
        self.span = (AREA_RANGE[0], AREA_RANGE[1])
        self._fit()

    def _x(self, area, hood, coast_m, elevation_m):
        return [1.0, math.log(area / REF_AREA),
                _coast(coast_m) if coast_m is not None else self.mean_coast, _elevation(elevation_m) if elevation_m is not None else self.mean_elevation] \
            + [1.0 if hood == h else 0.0 for h in self.hoods]

    def _fit(self):
        rows = self.rows
        for r in rows:
            self.counts[r['hood']] = self.counts.get(r['hood'], 0) + 1
        if len(rows) < MIN_ROWS:
            return
        self.hoods = sorted(h for h, n in self.counts.items() if n >= MIN_HOOD_ROWS)
        coasts = [_coast(r['coast_m']) for r in rows if r['coast_m'] is not None]
        heights = [_elevation(r['elevation_m']) for r in rows if r['elevation_m'] is not None]
        self.mean_coast, self.mean_elevation = (statistics.fmean(coasts) if coasts else 0.0), (statistics.fmean(heights) if heights else 0.0)
        xs = [self._x(r['area'], r['hood'], r['coast_m'], r['elevation_m']) for r in rows]
        ys = [math.log(r['rate']) for r in rows]
        width = len(xs[0])
        penalty = [0.0, 0.0, TERRAIN_PENALTY, TERRAIN_PENALTY] + [HOOD_PENALTY] * len(self.hoods)
        robust = [1.0] * len(rows)
        for _ in range(4):
            A = [[0.0] * width for _ in range(width)]
            b = [0.0] * width
            for x, y, down in zip(xs, ys, robust):
                for i in range(width):
                    if x[i]:
                        b[i] += down * x[i] * y
                        for j in range(width):
                            A[i][j] += down * x[i] * x[j]
            for i in range(width):
                A[i][i] += penalty[i] + 1e-9
            inverse = self.solve(A, [[1.0 if i == j else 0.0 for j in range(width)] for i in range(width)])
            w = [sum(inverse[i][j] * b[j] for j in range(width)) for i in range(width)]
            residuals = [y - sum(a * v for a, v in zip(w, x)) for x, y in zip(xs, ys)]
            centre = statistics.median(residuals)
            scale = max(0.2, 1.4826 * statistics.median(abs(r - centre) for r in residuals))
            robust = [min(1.0, ROBUST_AT * scale / max(abs(r - centre), 1e-9)) for r in residuals]
        self.w, self.inverse = w, inverse
        # Each parcel's own expectation is taken from the fit as it would be without that parcel (exact for this linear fit at fixed weights).
        outside = []
        for row, x, y, down, residual in zip(rows, xs, ys, robust, residuals):
            leverage = min(0.9, down * sum(x[i] * sum(inverse[i][j] * x[j] for j in range(width)) for i in range(width)))
            left_out = y - residual / (1 - leverage)
            outside.append(y - left_out)
            for listing in row['ids']:
                self.loo[listing] = (left_out, leverage)
        centre = statistics.median(outside)
        self.scatter = max(0.2, 1.4826 * statistics.median(abs(r - centre) for r in outside))
        areas = [r['area'] for r in rows]
        self.span = (min(areas), max(areas))

    def effects(self):
        """{neighbourhood: its level against the overall one, as a factor on the logarithm}; 0 for one too thin to learn."""
        return {} if self.w is None else {h: self.w[4 + i] for i, h in enumerate(self.hoods)}

    def estimate(self, kind, area, hood, listing=None, land=None):
        """-> {tier: model|none, fair (TL/m²), low, high, scatter, confidence, n, hood_n, drivers, note, ...}. `land`: this parcel's terrain, when measured."""
        hood_key = neighborhood(hood)
        base = {'method': METHOD, 'tier': 'none', 'fair': None, 'n': len(self.rows), 'confidence': 'insufficient', 'unmodeled': [UNPRICED], 'warnings': []}
        if self.w is None:
            return {**base, 'note': f'Fiyat beklentisi için yeterli ilan yok ({len(self.rows)}; en az {MIN_ROWS} gerekli).'}
        if not area or area <= 0 or not hood_key:
            return {**base, 'note': 'Alan ya da mahalle bilinmiyor.'}
        land = land or {}
        coast, height = land.get('coast_m'), land.get('elevation_m')
        x = self._x(area, hood_key, coast, height)
        value = sum(a * v for a, v in zip(self.w, x))
        leverage = None
        if listing in self.loo:
            value, leverage = self.loo[listing]
        warnings = []
        hood_n = self.counts.get(hood_key, 0)
        if hood_key not in self.hoods:
            warnings.append(f'{hood} için {hood_n} ilan var; mahalle düzeyi öğrenilemedi, Datça geneli kullanıldı.')
        if not self.span[0] * 0.5 <= area <= self.span[1] * 2:
            warnings.append('Parsel, verideki büyüklük aralığının dışında.')
        if coast is None:
            warnings.append('Arazi ölçümü (denize uzaklık, rakım) henüz yok; ortalama değer varsayıldı.')
        confidence = 'low' if warnings and (hood_key not in self.hoods or not self.span[0] * 0.5 <= area <= self.span[1] * 2) else 'medium'
        effect = lambda v: round(100 * (math.exp(v) - 1))
        drivers = [{'key': 'size', 'label': f'Büyüklük ({area:,.0f} m²)'.replace(',', '.'), 'pct': effect(self.w[1] * x[1])},
                   {'key': 'hood', 'label': str(hood), 'pct': effect(self.w[4 + self.hoods.index(hood_key)]) if hood_key in self.hoods else None}]
        if coast is not None:
            drivers.append({'key': 'coast', 'label': f'Denize {coast / 1000:.1f} km'.replace('.', ','), 'pct': effect(self.w[2] * x[2])})
        if height is not None:
            drivers.append({'key': 'elevation', 'label': f'Rakım {height:.0f} m', 'pct': effect(self.w[3] * x[3])})
        spread = self.scatter * math.sqrt(1 + (leverage or 0.0))
        return {**base, 'tier': 'model', 'fair': math.exp(value), 'low': math.exp(value - spread), 'high': math.exp(value + spread), 'scatter': self.scatter,
                'confidence': confidence, 'hood_n': hood_n, 'warnings': warnings, 'drivers': drivers, 'reference_rate': round(math.exp(self.w[0])),
                'size_elasticity': round(self.w[1], 2),
                'note': f'Datça\'daki {len(self.rows)} parselin istenen m² fiyatından: büyüklük, mahalle, denize uzaklık ve rakıma göre beklenen fiyat. '
                        'İlan fiyatıdır, satış fiyatı değildir.'}
