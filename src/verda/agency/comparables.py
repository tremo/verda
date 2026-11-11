"""Datça asking-price comparables. Deterministic, explainable, no area price coefficient.

Current accepted facts qualify parcels; unknown access/title/sit/sale scope is not a
negative fact. Direct access, easements and conditional access are separate groups,
as are different sit states. Conditional access can yield a provisional reference;
it is never promoted to confirmed road access and never exceeds low confidence.
One parcel is one observation, even under several ads. The subject and overlapping
sale packages are absent from BOTH comparables and coefficient learning.

The same 0.5–2 area band supplies the median and the optional regularized location /
type fit. With insufficient support, raw Datça prices remain a LOW-CONFIDENCE
reference, not an assertion that neighbourhoods or agricultural types cost the same.
All weights and thresholds below are explicit policy choices, not learned discounts.
"""
from collections import Counter
import math
import re
import statistics

from verda.facts.text import fold

METHOD = 'datca-comparables-v1'
SIZE_BAND = (0.5, 2.0)
MIN_COMPS = 3
MIN_FIT = 20
MIN_HOOD_ROWS = 3
HOOD_PENALTY, TYPE_PENALTY = 3.0, 6.0
PROFILE_FIELDS = ('access', 'shared_title', 'natural_sit', 'archaeological_sit')
COMPARABLE_ACCESS = ('direct', 'existing_easement', 'corridor_conditional')
CRITICAL = (*PROFILE_FIELDS, 'price_tl', 'sale_area_m2', 'parcel_area_m2', 'sale_scope', 'sale_scope_resolved', 'sale_parcels', 'parcel_identity', 'property_type')
GRADE_WEIGHT = {'kullanici': 1.0, 'resmi': 1.0, 'bagimsiz': 0.9, 'beyan': 0.7}
UNMODELED = ['Su, elektrik, eğim, manzara ve yapılaşma koşulları bu hesapta eşitlenmedi.']
REASONS = {
    'identity': 'Parsel kimliği bilinmiyor', 'conflict': 'Kayıtlarda çelişki var',
    'scope': 'Satılan alan ve satış kapsamı kesinleşmemiş', 'price': 'Güncel fiyat veya alan bilinmiyor', 'type': 'Nitelik bilinmiyor',
    'access': 'Yol yok veya erişim durumu bilinmiyor',
    'title': 'Hisseli tapu veya tapu durumu bilinmiyor', 'sit': 'Sit durumu eksik veya karşılaştırmaya uygun değil',
    'removed': 'İlan kaldırılmış veya kullanıcı tarafından elenmiş', 'stale': 'Fiyat 180 günden eski',
    'duplicate_conflict': 'Aynı parselin ilanlarında kapsam veya özellik çelişkisi var',
    'subject': 'İlanın kendisi veya aynı parselin diğer ilanı',
    'conditions': 'Yol veya sit koşulları farklı', 'size': 'Benzer büyüklük bandının dışında',
}


def neighborhood(name):
    name = fold(str(name or '')).strip()
    name = re.sub(r'^(koyler)[ /:>-]*', '', name)
    return re.sub(r'\s+(mh\.?|mah\.?|mahallesi)$', '', name).strip()


def parcel_keys(row, current):
    raw = current.get('parcels') or [row.get('parcel')]
    out = set()
    for value in raw:
        match = re.fullmatch(r'(?:(.+):)?\s*(\d+)\s*/\s*(\d+)\s*', str(value or ''))
        if match:
            hood = neighborhood(match[1] or row.get('neighborhood'))
            if hood:
                out.add(f'{hood}:{int(match[2])}/{int(match[3])}')
    return out


def price_dates(store, mode):
    """Age of the accepted price's actual observation, not the date of a later reimport."""
    with store.transaction() as con:
        rows = con.execute("""SELECT c.id, c.created_at, c.source_kind, o.tool, o.captured_at
            FROM claims c LEFT JOIN observations o ON o.id=c.observation_id AND o.mode=c.mode
            WHERE c.field='price_tl' AND c.mode=? AND c.status='accepted'""", (mode,)).fetchall()
    return {r['id']: (r['captured_at'] if r['tool'] in ('sahibinden.search', 'sahibinden.read_listing', 'sahibinden.read_thread')
                      else r['created_at'] if r['source_kind'] == 'user' else None) for r in rows}


def profile(row, current, dates, now):
    facts = {k: v for k, v in (current.get('facts') or {}).items() if isinstance(v, dict)}
    values = {k: v.get('value') for k, v in facts.items()}
    issues = []
    keys = parcel_keys(row, current)
    if not keys:
        issues.append('identity')
    if set(current.get('conflicts') or []) & set(CRITICAL):
        issues.append('conflict')
    if values.get('sale_scope_resolved') is not True:
        issues.append('scope')
    if not values.get('property_type'):
        issues.append('type')
    if values.get('access') not in COMPARABLE_ACCESS:
        issues.append('access')
    if values.get('shared_title') is not False:
        issues.append('title')
    if values.get('natural_sit') not in ('none', 'second_degree', 'third_degree', 'archival_exception') or values.get('archaeological_sit') is not False:
        issues.append('sit')
    if (current.get('seen') or {}).get('listing_removed') or values.get('user_excluded') is True:
        issues.append('removed')
    price, area = values.get('price_tl'), values.get('sale_area_m2')
    if not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in (price, area)):
        issues.append('price')
        price = area = None
    # A legacy row may have numbers, but cannot silently supply an unknown current fact.
    at = dates.get((facts.get('price_tl') or {}).get('claim_id'))
    age = max(0, (now - at) / 86400) if at is not None and at <= now + 86400 else None
    if age is not None and age > 180:
        issues.append('stale')
    grades = {k: facts[k].get('grade') for k in PROFILE_FIELDS if k in facts}
    quality = min((GRADE_WEIGHT.get(g, 0.5) for g in grades.values()), default=0.5)
    freshness = 0.5 if age is None else 1.0 if age <= 90 else 0.75
    return {**row, 'ids': {row['id']}, 'parcels': keys, 'hood': neighborhood(row.get('neighborhood')),
            'area': area, 'rate': price / area if price and area else None,
            'conditions': {k: values.get(k) for k in PROFILE_FIELDS}, 'issues': issues,
            'quality': quality, 'freshness': freshness, 'grades': grades, 'age_days': round(age, 1) if age is not None else None}


def prepare(rows, statuses, dates, now):
    """Consolidate connected parcel identities, including overlapping multi-parcel sales.
    Unknown old aliases count for self-exclusion but never add an unverified price.
    Different packages sharing a parcel are ambiguous, not independent observations.
    """
    profiles = [profile(r, statuses.get(r['id']) or {}, dates, now) for r in rows]
    groups, owners = {}, {}
    for p in profiles:
        hits = {owners[k] for k in p['parcels'] if k in owners}
        root = min(hits) if hits else p['id']
        group = groups.setdefault(root, [])
        for other in hits - {root}:
            group.extend(groups.pop(other))
        group.append(p)
        for member in group:
            for key in member['parcels']:
                owners[key] = root
    peers = []
    for group in groups.values():
        good = [p for p in group if not p['issues']]
        chosen = good or group
        conflict = any('conflict' in p['issues'] for p in group) or any(
            len({p['conditions'][f] for p in group if p['conditions'][f] is not None}) > 1 for f in PROFILE_FIELDS)
        sets = {tuple(sorted(p['parcels'])) for p in group if p['parcels']}
        if len(sets) > 1 or (good and max(p['area'] for p in good) / min(p['area'] for p in good) > 1.02):
            conflict = True
        if len({p['type'] for p in good}) > 1:
            conflict = True
        issues = ['duplicate_conflict'] if conflict else [] if good else list(dict.fromkeys(i for p in group for i in p['issues']))
        peers.append({**chosen[0], 'ids': set().union(*(p['ids'] for p in group)), 'parcels': set().union(*(p['parcels'] for p in group)),
                      'rate': statistics.median(p['rate'] for p in good) if good else chosen[0]['rate'],
                      'area': statistics.median(p['area'] for p in good) if good else chosen[0]['area'],
                      'quality': min(p['quality'] for p in chosen), 'freshness': min(p['freshness'] for p in chosen),
                      'grades': {f: min((p['grades'].get(f) for p in chosen), key=lambda g: GRADE_WEIGHT.get(g, 0.5)) for f in PROFILE_FIELDS},
                      'age_days': max(p['age_days'] for p in chosen) if all(p['age_days'] is not None for p in chosen) else None,
                      'issues': issues})
    return peers


def quantile(values, q):
    """Weighted midpoint-CDF interpolation; equal weights give the ordinary median."""
    ordered = sorted(values)
    total, cumulative, points = sum(w for _, w in ordered), 0.0, []
    for value, weight in ordered:
        points.append(((cumulative + weight / 2) / total, value))
        cumulative += weight
    if q <= points[0][0]:
        return points[0][1]
    for (a, x), (b, y) in zip(points, points[1:]):
        if q <= b:
            return x + (y - x) * (q - a) / (b - a)
    return points[-1][1]


def _fit(pool, target, kind, solve):
    """Robust ridge fit to log asking price: type + neighbourhood ONLY.
    No fit without a connected type/hood graph: otherwise the two effects cannot be
    separated. Penalties shrink sparse effects towards 1; they are policy constants.
    """
    counts = Counter(p['hood'] for p in pool)
    kinds = sorted({p['type'] for p in pool})
    hoods = sorted(counts)
    width = 1 + len(hoods) + len(kinds)
    if len(pool) < max(MIN_FIT, 2 * width) or counts[target] < MIN_HOOD_ROWS or sum(p['type'] == kind for p in pool) < MIN_HOOD_ROWS or len(hoods) < 2:
        return None
    graph = {}
    for p in pool:
        h, k = 'h:' + p['hood'], 'k:' + p['type']
        graph.setdefault(h, set()).add(k)
        graph.setdefault(k, set()).add(h)
    reached, todo = set(), [next(iter(graph))]
    while todo:
        v = todo.pop()
        if v not in reached:
            reached.add(v)
            todo.extend(graph[v] - reached)
    if len(reached) < len(graph):
        return None
    xs = [[1.0] + [float(p['hood'] == h) for h in hoods] + [float(p['type'] == k) for k in kinds] for p in pool]
    ys = [math.log(p['rate']) for p in pool]
    weights = [p['quality'] * p['freshness'] for p in pool]
    robust = [1.0] * len(pool)
    for _ in range(4):
        A, b = [[0.0] * width for _ in range(width)], [[0.0] for _ in range(width)]
        for x, y, weight, down in zip(xs, ys, weights, robust):
            for i in range(width):
                b[i][0] += weight * down * x[i] * y
                for j in range(width):
                    A[i][j] += weight * down * x[i] * x[j]
        for i in range(1, width):
            A[i][i] += HOOD_PENALTY if i <= len(hoods) else TYPE_PENALTY
        w = [r[0] for r in solve(A, b)]
        residuals = [y - sum(a * v for a, v in zip(w, x)) for x, y in zip(xs, ys)]
        center = statistics.median(residuals)
        scale = max(0.2, 1.4826 * statistics.median(abs(r - center) for r in residuals))
        robust = [min(1.0, 1.5 * scale / max(abs(r - center), 1e-9)) for r in residuals]
    return {'hood': dict(zip(hoods, w[1:1 + len(hoods)])), 'type': dict(zip(kinds, w[1 + len(hoods):])),
            'n': len(pool), 'scatter': scale}


class Estimator:
    def __init__(self, peers, hood_of, lands, solve):
        self.rows = [p for p in peers if p['type'] in lands]
        self.by_id = {i: p for p in peers for i in p['ids']}
        self.lands, self.solve = lands, solve
        self.sigma = 0.5
        # Kept for the separate purpose-fit component. This change introduces no new
        # area/type/region premium into the user's investment preferences.
        self.effects = {}

    def estimate(self, kind, area, hood, listing=None):
        subject, hood = self.by_id.get(listing), neighborhood(hood)
        base = {'method': METHOD, 'size_adjusted': False, 'tier': 'none', 'fair': None, 'n': 0, 'comps': [],
                'confidence': 'insufficient', 'unmodeled': UNMODELED, 'exclusions': [], 'warnings': []}
        if kind not in self.lands or not area or area <= 0 or not hood or subject is None:
            return {**base, 'note': 'Tür, alan, mahalle veya güncel parsel kaydı eksik.'}
        if subject['issues']:
            return {**base, 'note': 'Bu ilan için hesap bekliyor: ' + '; '.join(REASONS[i] for i in subject['issues']) + '.',
                    'subject_issues': subject['issues']}
        base['conditions'] = subject['conditions']
        if subject['conditions']['access'] == 'corridor_conditional':
            base['warnings'] = ['Yol erişimi koşullu: karşılaştırma aynı durumdaki parsellerle yapıldı. '
                                'Yolun varlığı doğrulanmış sayılmaz; yol olmadığı kesinleşirse ilan ve emsal havuzundan elenir.']
        counts, pool = Counter(), []
        for p in self.rows:
            reason = ('subject' if p['parcels'] & subject['parcels'] or listing in p['ids'] else
                      p['issues'][0] if p['issues'] else
                      'conditions' if p['conditions'] != subject['conditions'] else
                      'size' if not SIZE_BAND[0] * area <= p['area'] <= SIZE_BAND[1] * area else None)
            if reason:
                counts[reason] += 1
            else:
                pool.append(p)
        base.update(n=len(pool), exclusions=[{'reason': k, 'label': REASONS[k], 'n': n} for k, n in sorted(counts.items())])
        fit = _fit(pool, hood, kind, self.solve)
        comps = []
        for p in pool:
            same_hood, same_type = p['hood'] == hood, p['type'] == kind
            hf = math.exp(fit['hood'][hood] - fit['hood'][p['hood']]) if fit else 1.0
            tf = math.exp(fit['type'][kind] - fit['type'][p['type']]) if fit and kind in fit['type'] else 1.0
            weight = p['quality'] * p['freshness'] * (2.0 if same_hood else 1.0) * (1.25 if same_type else 1.0)
            comps.append({'ids': sorted(p['ids']), 'neighborhood': p['neighborhood'], 'type': p['type'],
                          'access': p['conditions']['access'],
                          'area': round(p['area'], 2), 'rate': round(p['rate'], 2), 'adjusted': p['rate'] * hf * tf,
                          'region_factor': hf if fit or same_hood else None,
                          'type_factor': tf if (fit and kind in fit['type']) or same_type else None,
                          'weight': weight, 'age_days': p['age_days'], 'grades': p['grades'],
                          'same_neighborhood': same_hood, 'same_type': same_type})
        total = sum(c['weight'] for c in comps)
        for c in comps:
            c['weight_pct'] = round(100 * c['weight'] / total, 1)
        comps.sort(key=lambda c: (-c['weight'], c['ids']))
        base['comps'] = [{**c, 'adjusted': round(c['adjusted'], 2), 'weight': round(c['weight'], 4),
                          'region_factor': round(c['region_factor'], 4) if c['region_factor'] is not None else None,
                          'type_factor': round(c['type_factor'], 4) if c['type_factor'] is not None else None} for c in comps]
        if len(comps) < MIN_COMPS:
            return {**base, 'note': f'Datça genelinde aynı yol/sit/tapu koşullarında {len(comps)} bağımsız emsal var; en az {MIN_COMPS} gerekli.'}
        warnings = list(base['warnings'])
        if any(c['region_factor'] is None for c in comps):
            warnings.append('Mahalle farkını öğrenmek için veri yetersiz; rakam Datça referansıdır, bu mahallenin değerlemesi değildir.')
        if any(c['type_factor'] is None for c in comps):
            warnings.append('Tarla, zeytinlik ve bademlik arasındaki fiyat farkı yeterince ölçülemedi; otomatik tür primi uygulanmadı.')
        if any(c['age_days'] is None for c in comps) or subject['age_days'] is None:
            warnings.append('Bazı fiyatların gözlem tarihi bilinmiyor.')
        if any(c['age_days'] is not None and c['age_days'] > 90 for c in comps) or (subject['age_days'] or 0) > 90:
            warnings.append('90 günden eski fiyatlar var.')
        effective = total ** 2 / sum(c['weight'] ** 2 for c in comps)
        confidence = 'low' if warnings or len(comps) < 6 or effective < 5 else 'medium'
        if len(comps) < 6 or effective < 5:
            warnings.append('Bağımsız emsal sayısı veya ağırlık dengesi sınırlı.')
        values = [(c['adjusted'], c['weight']) for c in comps]
        fair = quantile(values, 0.5)
        logs = [(abs(math.log(c['adjusted'] / fair)), c['weight']) for c in comps]
        scatter = max(0.2, 1.4826 * quantile(logs, 0.5))
        if scatter > 0.6:
            confidence = 'low'
            warnings.append('Emsal fiyatları çok dağınık.')
        return {**base, 'tier': 'datca', 'fair': fair, 'low': quantile(values, 0.25), 'high': quantile(values, 0.75),
                'confidence': confidence, 'effective_n': round(effective, 1), 'scatter': scatter, 'warnings': warnings,
                'fit_n': fit['n'] if fit else 0, 'conditions': subject['conditions'],
                'note': f'Datça genelindeki {len(comps)} bağımsız parselin ağırlıklı m² ilan fiyatı ortancası. '
                        'Aynı mahalle ×2, aynı tür ×1,25; beyan içeren özellikler ×0,7, bağımsız ölçüm ×0,9. '
                        'Fiyat 90–180 günlükse ×0,75, tarihi bilinmiyorsa ×0,5. '
                        'Büyüklük yalnız 0,5–2 kat emsal seçme bandıdır; fiyat çarpanı yok. '
                        + ('Mahalle ve tür farkları bu emsal grubundan öğrenilerek düzeltildi.' if fit else 'Mahalle/tür fiyat düzeltmesi uygulanmadı.')}
