"""Cadastral road-frontage evidence from official parcel geometry (TKGM), by coordinate probes.

The old system's rule, kept: sample sparse points just outside every boundary edge of the
identity-verified parcel; a returned parcel counts as a neighbour only if its polygon really shares
a boundary segment; an empty answer is *missing evidence*, never a road by itself. A corridor is a
candidate only when a gap touches an edge over a stretch AND a numbered parcel sits on its far bank.
Never concludes "no road": absence of evidence stays unknown. Vehicle passability, width, the
road-versus-creek function and the connection to an existing road are separate, unverified.
"""
import json
import math
import time
import urllib.error

from verda.agency.registry import ResourceBlocked, TransientToolError
from verda.agency.tkgm import BASE, fetch_json

NEAR_M = 2.5                 # first probe: just outside the edge
FAR_M = (8, 15, 25, 40)      # across a gap: where is the other bank?
MAX_REQUESTS = 60
PROBE_DELAY = 2.5            # seconds between two probes of one call; the tool's own wait only spaces whole calls
BACKOFF_CAP = 30.0           # every failed answer in a row doubles the wait for the next probe, up to this
MAX_FAILURES_IN_A_ROW = 3    # then the call stops asking and reports what it has: a struggling source is not pushed further
M_LAT = 110540.0
M_LON = 111320.0


class Plane:
    """Local metres around the parcel; fine for a few hundred metres."""
    def __init__(self, lon0, lat0):
        self.lon0, self.lat0, self.k = lon0, lat0, math.cos(math.radians(lat0))

    def to_xy(self, lon, lat):
        return (lon - self.lon0) * M_LON * self.k, (lat - self.lat0) * M_LAT

    def to_lonlat(self, x, y):
        return self.lon0 + x / (M_LON * self.k), self.lat0 + y / M_LAT


def _seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p; dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_ring(p, ring):
    return min(_seg_dist(p, ring[i], ring[i + 1]) for i in range(len(ring) - 1))


def shared_length(a, b, ring, step=0.5, tol=1.0) -> float:
    """Length of the subject edge a-b that lies within `tol` metres of the neighbour's boundary."""
    length = math.dist(a, b)
    n = max(1, int(length / step))
    hits = sum(dist_to_ring((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n), ring) <= tol for i in range(n + 1))
    return round(hits * length / (n + 1), 1)


def _orientation(ring):
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1))


def point_url(lon, lat):
    return f'{BASE}/parsel/{lat:.7f}/{lon:.7f}/'


def identity(feature) -> tuple | None:
    props = feature.get('properties') if isinstance(feature, dict) else None
    if isinstance(props, dict) and props.get('adaNo') is not None and props.get('parselNo') is not None:
        return str(props['adaNo'] or '0'), str(props['parselNo']), str(props.get('mahalleId'))      # an empty ada is ada 0, as in the subject's key
    return None


ROAD_WORDS = ('yol', 'patika', 'geçit')


def is_road_type(nitelik) -> bool:
    """A numbered neighbour whose own land type is a road is road frontage, not a neighbour."""
    return isinstance(nitelik, str) and any(w in nitelik.lower() for w in ROAD_WORDS)


def probe(fetch, lon, lat):
    """('parcel', feature) | ('gap', None) | ('failed', reason). 403/429 stop the shared source."""
    try:
        data = fetch(point_url(lon, lat))
    except urllib.error.HTTPError as error:
        if error.code in {403, 429}:
            raise ResourceBlocked(f'tkgm_http_{error.code}') from None
        return ('gap', None) if error.code == 404 else ('failed', f'http_{error.code}')
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 'failed', 'unreachable'
    return ('parcel', data) if identity(data) else ('gap', None)


def ring_xy(feature, plane):
    geometry = feature.get('geometry') if isinstance(feature, dict) else None
    if isinstance(geometry, dict) and geometry.get('type') == 'Polygon' and geometry.get('coordinates'):
        return [plane.to_xy(*p[:2]) for p in geometry['coordinates'][0]]
    return None


def analyse(subject: dict, fetch=fetch_json, sleep=time.sleep, delay=PROBE_DELAY, max_requests=MAX_REQUESTS) -> dict:
    """subject: {'parcel_key': 'ada/parsel', 'neighborhood_id': int, 'ring': [[lon, lat], ...]}"""
    lons = [p[0] for p in subject['ring']]; lats = [p[1] for p in subject['ring']]
    plane = Plane(sum(lons) / len(lons), sum(lats) / len(lats))
    ring = [plane.to_xy(*p[:2]) for p in subject['ring']]
    sign = 1 if _orientation(ring) > 0 else -1
    ada, parsel = subject['parcel_key'].split('/')
    own = (ada, parsel, str(subject['neighborhood_id']))
    requests = {'n': 0, 'failed_in_a_row': 0}
    probes, edges = [], []

    def ask(x, y):
        if requests['n'] >= max_requests:
            return ('failed', 'request_cap'), None
        if requests['failed_in_a_row'] >= MAX_FAILURES_IN_A_ROW:
            return ('failed', 'stopped_after_failures'), None
        if requests['n']:
            sleep(min(delay * 2 ** requests['failed_in_a_row'], max(delay, BACKOFF_CAP)))
        requests['n'] += 1
        lon, lat = plane.to_lonlat(x, y)
        answer = probe(fetch, lon, lat)
        requests['failed_in_a_row'] = requests['failed_in_a_row'] + 1 if answer[0] == 'failed' else 0
        return answer, (round(lat, 7), round(lon, 7))

    for index in range(len(ring) - 1):
        a, b = ring[index], ring[index + 1]
        length = math.dist(a, b)
        if length < 2:
            continue
        nx, ny = sign * (b[1] - a[1]) / length, -sign * (b[0] - a[0]) / length      # outward unit normal
        positions = (0.5,) if length < 15 else (0.25, 0.5, 0.75)
        results = []
        for t in positions:
            mx, my = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            (kind, found), where = ask(mx + nx * NEAR_M, my + ny * NEAR_M)
            row = {'edge': index, 't': t, 'offset_m': NEAR_M, 'at': where}
            if kind == 'parcel' and identity(found) == own:
                row['result'] = 'inside_subject'                                      # sampling landed in the parcel itself
            elif kind == 'parcel':
                other = ring_xy(found, plane)
                shared = shared_length(a, b, other) if other else None
                row.update(result='neighbor' if shared is None or shared >= 1 else 'thin_gap', parcel='/'.join(identity(found)[:2]),
                           neighborhood_id=identity(found)[2], shared_m=shared, nitelik=((found.get('properties') or {}).get('nitelik')))
            else:
                row['result'] = 'gap' if kind == 'gap' else 'failed'
                if kind == 'failed':
                    row['reason'] = found
            probes.append(row); results.append(row)
        edge = {'edge': index, 'length_m': round(length, 1), 'results': [r['result'] for r in results]}
        gaps = [r for r in results if r['result'] == 'gap']
        if gaps and len(gaps) >= min(2, len(results)):
            mid = results[len(results) // 2]
            mx, my = a[0] + (b[0] - a[0]) * mid['t'], a[1] + (b[1] - a[1]) * mid['t']
            for d in FAR_M:
                (kind, found), where = ask(mx + nx * d, my + ny * d)
                far = {'edge': index, 't': mid['t'], 'offset_m': d, 'at': where, 'result': 'gap' if kind == 'gap' else 'failed' if kind == 'failed' else 'parcel'}
                if kind == 'parcel' and identity(found) != own:
                    far.update(parcel='/'.join(identity(found)[:2]), neighborhood_id=identity(found)[2])
                    probes.append(far)
                    edge['other_bank'] = {'parcel': far['parcel'], 'within_m': d}
                    break
                probes.append(far)
        edges.append(edge)

    by_edge = {}
    for row in probes:
        if row.get('result') == 'neighbor' and is_road_type(row.get('nitelik')):
            by_edge.setdefault(row['edge'], row['parcel'])
    for edge in edges:
        if edge['edge'] in by_edge:
            edge['road_parcel'] = by_edge[edge['edge']]
    corridor = [e for e in edges if 'other_bank' in e or 'road_parcel' in e]
    failed = sum(r['result'] == 'failed' for r in probes)
    if corridor:
        verdict = 'corridor_candidate'
    elif edges and all(set(e['results']) <= {'neighbor', 'inside_subject'} for e in edges):
        verdict = 'all_edges_numbered_neighbors'
    else:
        verdict = 'inconclusive'
    return {'parcel_key': subject['parcel_key'], 'verdict': verdict, 'requests': requests['n'], 'failed_probes': failed,
            **({'stopped': 'repeated_failures'} if requests['failed_in_a_row'] >= MAX_FAILURES_IN_A_ROW else {}),
            'edges': edges, 'probes': probes,
            'corridor': None if not corridor else {'edges': [e['edge'] for e in corridor],
                                                   'other_bank': sorted({e['other_bank']['parcel'] for e in corridor if 'other_bank' in e}),
                                                   **({'road_parcels': sorted(set(by_edge.values()))} if by_edge else {})},
            'note': ('Aday: sınıra temas eden numarasız boşluk ve karşı yakada numaralı parsel. Yolun hukuki niteliği, araç geçişi, genişlik, '
                     'mevcut yola bağlantı ve yol/dere ayrımı doğrulanmadı; yolun manuel değerlendirilmesi tavsiye edilir.'
                     if corridor else 'Boş yanıt yol kanıtı değildir; kanıt bulunamadı ≠ yol yok.')}


def listing_corridor(store, args, *, mode, fetch=fetch_json, sleep=time.sleep):
    listing = args['listing_id']
    with store.transaction() as con:
        row = con.execute("""SELECT payload FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref=? AND json_extract(payload,'$.part') IS NULL
            ORDER BY captured_at DESC LIMIT 1""", (mode, listing)).fetchone()      # the sale's main parcel, not a further deed
    parcel = json.loads(row['payload']) if row else None
    ring = ((parcel or {}).get('geometry') or {}).get('ring')
    if not parcel or parcel.get('found') is not True or not ring:
        return {'listing_id': listing, 'reason': 'official_parcel_with_geometry_required'}
    return {'listing_id': listing, **analyse({'parcel_key': parcel['parcel_key'], 'neighborhood_id': parcel['neighborhood_id'], 'ring': ring}, fetch, sleep)}


def neighbor_types(store, args, *, mode, fetch=fetch_json, sleep=time.sleep, delay=PROBE_DELAY, limit=12):
    """For a listing whose earlier corridor check recorded only parcel numbers: the land type of each numbered neighbour, so a neighbour that is
    itself a road parcel is found. One request per distinct neighbour; the verdict is positive only when a road-type neighbour exists."""
    listing = args['listing_id']
    with store.transaction() as con:
        row = con.execute("SELECT payload FROM observations WHERE tool='tkgm.corridor' AND mode=? AND listing_ref=? ORDER BY captured_at DESC, id DESC LIMIT 1", (mode, listing)).fetchone()
    if not row:
        return {'listing_id': listing, 'reason': 'corridor_check_required'}
    probes = json.loads(row['payload']).get('probes') or []
    wanted = {}
    for p in probes:
        if p.get('result') == 'neighbor' and p.get('parcel') and p.get('at') and p.get('nitelik') is None:
            wanted.setdefault(p['parcel'], p['at'])
    known = {p['parcel']: p.get('nitelik') for p in probes if p.get('result') == 'neighbor' and p.get('parcel') and p.get('nitelik') is not None}
    neighbors, failed = dict(known), 0
    for index, (parcel, (lat, lon)) in enumerate(list(wanted.items())[:limit]):
        if index:
            sleep(delay)
        kind, found = probe(fetch, lon, lat)
        if kind == 'parcel':
            neighbors[parcel] = (found.get('properties') or {}).get('nitelik') or '?'
        else:
            failed += 1
    road = sorted(p for p, t in neighbors.items() if is_road_type(t))
    verdict = 'road_neighbor' if road else 'incomplete' if (failed or len(wanted) > limit) else 'no_road_neighbor'
    return {'listing_id': listing, 'verdict': verdict, 'neighbors': [{'parcel': p, 'nitelik': t} for p, t in sorted(neighbors.items())], 'road_neighbors': road,
            'requests': min(len(wanted), limit), 'failed': failed,
            'note': 'Komşu parselin kendi niteliği yol ise parsel yola cepheli sayılır. Yol çıkmaması geçiş hakkı olmadığı anlamına gelmez.'}
