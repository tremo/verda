"""Official natural-sit check: the parcel's TKGM outline against the ministry's sit layer (SAYS, layer tabiatvarlik:VatandasSitAlani).

The same method the old system used (full-polygon overlay), done again here so the result is a v2 observation with its own provenance.
One request fetches the whole region's layer (58 areas); it is kept for a day. Read-only; the tool never decides anything: a rule turns
the numbers into a natural-sit value (facts.extract) and the rule engine does the rest.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from verda.agency.registry import ResourceBlocked

LAYER_URL = 'https://tvksaysgis.csb.gov.tr/geoserver/tabiatvarlik/ows'
LAYER = 'tabiatvarlik:VatandasSitAlani'
BBOX = '27.20,36.55,28.00,37.00'                     # the Datça-Marmaris peninsulas
CACHE_SECONDS = 86400
COUNTS_FROM = 0.05                                   # less than 5 % of the parcel inside a sit area is a boundary case, not a sit
DEGREES = {'1': 'first', '2': 'second', '3': 'third'}


BLOCKING_CODES = {401, 403, 429}          # the ministry's server asks for a login (401, since about 23 Oct 2026) or says slow down: the source stops, nothing is tried around it


def _get(url, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': 'verda/2 (read-only sit overlay)'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code in BLOCKING_CODES:
            raise ResourceBlocked(f'says_http_{error.code}') from None      # one incident on the status page instead of a failed call per listing
        raise


def layer(cache_path, now=None, fetch=_get):
    """The layer as parsed GeoJSON, from the day's cache or one request."""
    now = time.time() if now is None else now
    cache = Path(cache_path)
    if cache.is_file() and now - cache.stat().st_mtime < CACHE_SECONDS:
        return json.loads(cache.read_text())
    query = urllib.parse.urlencode({'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature', 'typeName': LAYER, 'outputFormat': 'application/json',
                                    'srsName': 'EPSG:4326', 'bbox': BBOX + ',EPSG:4326'})
    raw = fetch(f'{LAYER_URL}?{query}')
    data = json.loads(raw)
    if not isinstance(data.get('features'), list):
        raise ValueError('says_layer_unreadable')
    cache.write_text(raw.decode() if isinstance(raw, bytes) else raw)
    return data


def _inside(x, y, rings):
    """Even-odd test over a polygon's rings (outer ring and holes)."""
    hit = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
    return hit


def _polygons(feature):
    geometry = feature.get('geometry') or {}
    if geometry.get('type') == 'Polygon':
        return [geometry['coordinates']]
    if geometry.get('type') == 'MultiPolygon':
        return geometry['coordinates']
    return []


def _bounds(rings):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _degree(status):
    text = str(status or '')
    return DEGREES.get(text[:1]) if text[:1].isdigit() and 'Doğal Sit' in text else None


def overlap(ring, data, steps=48):
    """Share of the parcel (sampled on a grid) inside each sit degree, plus the areas touched.
    -> {'fractions': {'first': f, ...}, 'areas': [feature properties], 'inside': samples inside the parcel}"""
    x0, y0, x1, y1 = _bounds([ring])
    parcel = [ring]
    points = []
    for i in range(steps):
        for j in range(steps):
            x, y = x0 + (i + 0.5) * (x1 - x0) / steps, y0 + (j + 0.5) * (y1 - y0) / steps
            if _inside(x, y, parcel):
                points.append((x, y))
    covered = {'first': set(), 'second': set(), 'third': set()}
    touched = []
    for feature in data.get('features', []):
        degree = _degree((feature.get('properties') or {}).get('SitDurum'))
        hits = set()
        for polygon in _polygons(feature):
            bx0, by0, bx1, by1 = _bounds(polygon[:1])
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            hits |= {n for n, (x, y) in enumerate(points) if bx0 <= x <= bx1 and by0 <= y <= by1 and _inside(x, y, polygon)}
        if hits:
            touched.append({**(feature.get('properties') or {}), 'share': round(len(hits) / max(1, len(points)), 3)})
            if degree:
                covered[degree] |= hits
    inside = max(1, len(points))
    return {'fractions': {k: round(len(v) / inside, 3) for k, v in covered.items()}, 'areas': touched, 'inside': len(points)}


def classify(fractions):
    """The most restrictive degree that covers at least COUNTS_FROM of the parcel; a sliver is only a boundary case."""
    for degree in ('first', 'second', 'third'):
        if fractions[degree] >= COUNTS_FROM:
            return degree
    return 'boundary' if any(fractions.values()) else 'none'


def parcel_ring(store, listing_id, mode, parcel=None):
    """The outline from the newest TKGM lookup that has one (of `parcel`, when named)."""
    with store.transaction() as con:
        rows = con.execute("SELECT payload FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref=? ORDER BY captured_at DESC, id DESC", (mode, listing_id)).fetchall()
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except ValueError:
            continue
        if parcel and payload.get('parcel_key') != parcel:
            continue
        geometry = payload.get('geometry') or {}
        ring = geometry.get('ring')
        if geometry.get('type') == 'Polygon' and isinstance(ring, list) and len(ring) >= 4:
            return [[float(x), float(y)] for x, y in ring]
    return None


def sale_parcels(store, listing_id, mode):
    """The parcels of the sale as the facts stand (main first); empty when no parcel is known."""
    from verda.agency.research import fact_status
    facts = fact_status(store, listing_id, mode=mode).get('facts') or {}
    main = (facts.get('parcel_key') or {}).get('value') if isinstance(facts.get('parcel_key'), dict) else None
    listed = (facts.get('sale_parcels') or {}).get('value') if isinstance(facts.get('sale_parcels'), dict) else None
    return ([main] if main else []) + [p for p in (listed or []) if p != main]


ORDER = ('first', 'second', 'third', 'boundary', 'none')


def check(store, args, mode='local', now=None, fetch=_get):
    """Agent tool `sit.says`. Every parcel of the sale is overlaid; the most restrictive answer stands for the sale. Without the outline of
    every parcel the answer says so and nothing is concluded."""
    listing = args['listing_id']
    now = time.time() if now is None else now
    parcels = sale_parcels(store, listing, mode) or [None]
    base = {'listing_id': listing, 'source': LAYER_URL, 'layer': LAYER, 'observed_at': now, **({'parcel_key': parcels[0]} if parcels[0] else {})}
    rings = {p: parcel_ring(store, listing, mode, p) for p in parcels}
    if any(r is None for r in rings.values()):
        return {**base, 'classification': 'no_geometry', 'missing': [p for p, r in rings.items() if r is None and p]}
    cache = store.path.parent / (store.path.name + '.says-layer.json')
    data = layer(cache, now, fetch)
    per = []
    for p, ring in rings.items():
        result = overlap(ring, data)
        per.append((p, classify(result['fractions']), result))
    parcel, classification, result = min(per, key=lambda x: ORDER.index(x[1]))
    strongest = {'first': '1.', 'second': '2.', 'third': '3.'}.get(classification)
    areas = [a for a in result['areas'] if strongest and str(a.get('SitDurum', '')).startswith(strongest)] if strongest else result['areas']
    return {**base, 'classification': classification, 'fractions': result['fractions'], 'samples': result['inside'], 'feature_count': len(data['features']),
            'layer_sha256': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16],
            **({'parcels': [{'parcel_key': p, 'classification': c, 'fractions': r['fractions']} for p, c, r in per]} if len(per) > 1 or per[0][0] else {}),
            'areas': [{'status': a.get('SitDurum'), 'decision_no': a.get('KararNo'), 'decision_date': a.get('KararTarih'), 'restrictions': a.get('Kisitlar'), 'share': a['share']}
                      for a in areas[:6]]}


def stale(store, listing_id, mode, parcels):
    """True when no sit overlay covers every parcel of the sale that has an official outline (none yet, or the parcels changed since)."""
    with store.transaction() as con:
        last = con.execute("SELECT payload, captured_at FROM observations WHERE tool='sit.says' AND mode=? AND listing_ref=? ORDER BY captured_at DESC, id DESC LIMIT 1",
                           (mode, listing_id)).fetchone()
        lookups = [(r['captured_at'], json.loads(r['payload'])) for r in con.execute(
            "SELECT payload, captured_at FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref=? ORDER BY captured_at", (mode, listing_id))]
    outlined = {p.get('parcel_key') for _, p in lookups if (p.get('geometry') or {}).get('ring')}
    wanted = {p for p in parcels if p in outlined}
    if last is None:
        return bool(outlined)
    payload = json.loads(last['payload'])
    covered = {x['parcel_key'] for x in payload.get('parcels') or []} or {payload.get('parcel_key')}
    if covered == {None}:                                # an overlay from before parcels were named: the main parcel looked up last before it
        mains = [p.get('parcel_key') for at, p in lookups if at <= last['captured_at'] and p.get('found') and not p.get('part')]
        covered = {mains[-1]} if mains else set()
    return bool(wanted - covered)
