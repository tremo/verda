"""The land itself, from the official outline and an open elevation model: height, slope and the side it faces.

Open Topo Data (SRTM, cells of about 30 m, a few metres of height noise) is asked once per parcel for a small grid of points around it; a plane
fitted through them gives the slope and its direction. This is the terrain's general lie, good for telling flat from steep: it is not a survey,
and a difference of a few percent means nothing. Read-only; 403/429 stop the shared source like every other, nothing is tried around them.
"""
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.request

from verda.agency.corridor import Plane
from verda.agency.registry import ResourceBlocked, TransientToolError

API = 'https://api.opentopodata.org/v1/srtm30m?locations={points}'
DATASET = 'SRTM 30 m (Open Topo Data)'
UA = 'Verda-terrain/2.0 (personal land research; low volume)'
GRID = 5                              # GRID x GRID points per parcel: one request
MIN_HALF_M, MAX_HALF_M = 60.0, 150.0  # the square the grid covers reaches at least 60 m and at most 150 m from the parcel's middle
MAX_POINTS = 100                      # the service's own limit per request
FLAT_BELOW = 4.0                      # under this slope the direction it faces is noise
CLASSES = ((6, 'düz'), (12, 'hafif eğimli'), (20, 'eğimli'), (35, 'dik'), (math.inf, 'çok dik'))
SIDES = ('kuzey', 'kuzeydoğu', 'doğu', 'güneydoğu', 'güney', 'güneybatı', 'batı', 'kuzeybatı')
NOTE = 'Uydu yükseklik modelinden (30 m) tahmindir; yerinde ölçüm değildir. Birkaç puanlık eğim farkı anlam taşımaz.'


SHORE = Path(__file__).with_name('shore-datca.json')     # sea cells that touch land, from the same height model on a 400 m grid (see the file's own note)
SHORE_REACH_M = 30_000                                    # farther than this from every shore point the parcel is outside the mapped region
_shore = {}


def shore_points(path=SHORE):
    if path not in _shore:
        data = json.loads(Path(path).read_text())
        _shore[path] = ([(float(x), float(y)) for x, y in data['points']], float(data.get('cell_m', 400)))
    return _shore[path]


def coast_m(lon, lat, path=SHORE):
    """Metres from the point to the sea, to the nearest 200 m or so; None outside the mapped region. A shore cell's middle lies up to half a cell
    out at sea, so half a cell is taken off."""
    points, cell = shore_points(path)
    k = math.cos(math.radians(lat))
    nearest = min(math.hypot((lon - x) * 111320.0 * k, (lat - y) * 110540.0) for x, y in points)
    return None if nearest > SHORE_REACH_M else round(max(0.0, nearest - cell / 2))


def get_json(url, timeout=25.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def centre(ring):
    """Area-weighted middle of the outline as (lon, lat); the plain average for a degenerate one."""
    plane = Plane(ring[0][0], ring[0][1])
    pts = [plane.to_xy(*p[:2]) for p in ring]
    twice = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:]))
    if abs(twice) < 1e-6:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    cx = sum((x0 + x1) * (x0 * y1 - x1 * y0) for (x0, y0), (x1, y1) in zip(pts, pts[1:])) / (3 * twice)
    cy = sum((y0 + y1) * (x0 * y1 - x1 * y0) for (x0, y0), (x1, y1) in zip(pts, pts[1:])) / (3 * twice)
    return plane.to_lonlat(cx, cy)


def _inside(x, y, ring):
    hit, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def grid(ring):
    """-> (plane, half size in metres, [(x, y)] in metres around the middle): GRID x GRID points on a square that covers the parcel."""
    lon, lat = centre(ring)
    plane = Plane(lon, lat)
    xy = [plane.to_xy(*p[:2]) for p in ring]
    reach = max(max(abs(x) for x, _ in xy), max(abs(y) for _, y in xy))
    half = max(MIN_HALF_M, min(MAX_HALF_M, reach))
    step = 2 * half / (GRID - 1)
    return plane, half, [(-half + i * step, -half + j * step) for j in range(GRID) for i in range(GRID)]


def elevations(points, fetch=get_json):
    """Heights for [(lat, lon)] (at most MAX_POINTS), None where the model has no value. One request."""
    if len(points) > MAX_POINTS:
        raise ValueError('too many points for one request')
    try:
        data = fetch(API.format(points='|'.join(f'{lat:.6f},{lon:.6f}' for lat, lon in points)))
    except urllib.error.HTTPError as error:
        if error.code in {403, 429}:
            raise ResourceBlocked(f'opentopodata_http_{error.code}') from None
        raise TransientToolError('elevation_unavailable') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise TransientToolError('elevation_unavailable') from None
    results = data.get('results') if isinstance(data, dict) else None
    if data.get('status') != 'OK' or not isinstance(results, list) or len(results) != len(points):
        raise TransientToolError('elevation_unexpected_response')
    return [float(r['elevation']) if isinstance(r.get('elevation'), (int, float)) else None for r in results]


def fit(xy, zs):
    """Least-squares plane z = a + b x + c y through the points -> (a, b, c, rms of what the plane leaves over)."""
    pts = [(x, y, z) for (x, y), z in zip(xy, zs) if z is not None]
    if len(pts) < 6:
        return None
    n = len(pts)
    mx, my, mz = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts); syy = sum((p[1] - my) ** 2 for p in pts); sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    sxz = sum((p[0] - mx) * (p[2] - mz) for p in pts); syz = sum((p[1] - my) * (p[2] - mz) for p in pts)
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        return None
    b, c = (sxz * syy - syz * sxy) / det, (syz * sxx - sxz * sxy) / det
    a = mz - b * mx - c * my
    rms = math.sqrt(sum((z - (a + b * x + c * y)) ** 2 for x, y, z in pts) / n)
    return a, b, c, rms


def slope_class(percent):
    return next(name for limit, name in CLASSES if percent < limit)


def measure(ring, plane, half, xy, zs):
    """What the heights say about the parcel: -> {elevation_m, min_m, max_m, relief_m, slope_pct, slope_class, aspect_deg, faces, roughness_m, samples}."""
    plane_fit = fit(xy, zs)
    if plane_fit is None:
        return None
    a, b, c, rms = plane_fit
    slope = 100 * math.hypot(b, c)
    aspect = (math.degrees(math.atan2(-b, -c)) + 360) % 360           # the compass direction the ground falls towards (x east, y north)
    outline = [plane.to_xy(*p[:2]) for p in ring]
    inside = [z for (x, y), z in zip(xy, zs) if z is not None and _inside(x, y, outline)]
    heights = inside if len(inside) >= 3 else [z for z in zs if z is not None]      # a small parcel holds few grid points: its surroundings speak for it
    return {'elevation_m': round(a), 'min_m': round(min(heights)), 'max_m': round(max(heights)), 'relief_m': round(max(heights) - min(heights)),
            'slope_pct': round(slope, 1), 'slope_class': slope_class(slope), 'aspect_deg': round(aspect) if slope >= FLAT_BELOW else None,
            'faces': SIDES[int((aspect + 22.5) // 45) % 8] if slope >= FLAT_BELOW else None, 'roughness_m': round(rms, 1),
            'samples': sum(z is not None for z in zs), 'samples_inside': len(inside), 'span_m': round(2 * half)}


def of_ring(ring, fetch=get_json):
    """The whole measurement for one outline ([[lon, lat], ...]); None when the model has too few values there."""
    plane, half, xy = grid(ring)
    points = [tuple(reversed(plane.to_lonlat(x, y))) for x, y in xy]
    return measure(ring, plane, half, xy, elevations(points, fetch))


def listing_terrain(store, args, *, mode, fetch=get_json, now=None):
    """Agent tool `geo.terrain`: the terrain of the sale's own parcel, from its stored official outline."""
    from verda.agency.research import fact_status
    from verda.facts.service import main_lookup
    listing = args['listing_id']
    parcels = fact_status(store, listing, mode=mode).get('parcels')
    with store.transaction() as con:
        parcel = main_lookup(con, listing, mode, parcels)
    ring = ((parcel or {}).get('geometry') or {}).get('ring')
    base = {'listing_id': listing, 'dataset': DATASET, 'observed_at': time.time() if now is None else now, 'note': NOTE}
    if not parcel or parcel.get('found') is not True or not isinstance(ring, list) or len(ring) < 4:
        return {**base, 'reason': 'official_parcel_with_geometry_required'}
    measured = of_ring(ring, fetch)
    if measured is None:
        return {**base, 'parcel_key': parcel['parcel_key'], 'reason': 'no_elevation_values'}
    lon, lat = centre(ring)
    return {**base, 'parcel_key': parcel['parcel_key'], 'centre': [round(lon, 6), round(lat, 6)], 'coast_m': coast_m(lon, lat), **measured}


def stored(store, mode='local'):
    """{listing id: the newest terrain measurement that has a slope}"""
    out = {}
    with store.transaction() as con:
        for row in con.execute("SELECT listing_ref, payload FROM observations WHERE tool='geo.terrain' AND mode=? AND listing_ref IS NOT NULL ORDER BY captured_at, id", (mode,)):
            try:
                payload = json.loads(row['payload'])
            except ValueError:
                continue
            if isinstance(payload.get('slope_pct'), (int, float)):
                out[row['listing_ref']] = payload
    return out



SWEEP_LIMIT = 40                # parcels handed out per sweep: one request each, two seconds apart


def waiting(store, mode='local'):
    """Listings that have an official outline and no terrain measurement yet; the ones still in the running first."""
    with store.transaction() as con:
        outlined = [r[0] for r in con.execute("SELECT DISTINCT listing_ref FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref IS NOT NULL "
                                              "AND json_extract(payload, '$.found') AND json_extract(payload, '$.geometry.ring') IS NOT NULL ORDER BY listing_ref", (mode,))]
        measured = {r[0] for r in con.execute("SELECT DISTINCT listing_ref FROM observations WHERE tool='geo.terrain' AND mode=? AND listing_ref IS NOT NULL", (mode,))}
        running = {r[0] for r in con.execute("SELECT a.listing_ref FROM assessments a JOIN (SELECT listing_ref, max(seq) m FROM assessments WHERE mode=? GROUP BY 1) x "
                                             "ON a.listing_ref=x.listing_ref AND a.seq=x.m WHERE a.mode=? AND a.state IN ('research_required','conditional','eligible')", (mode, mode))}
        busy = {r[0] for r in con.execute("SELECT listing_ref FROM inbox WHERE executor_tool='geo.terrain' AND state IN ('queued','running') AND listing_ref IS NOT NULL")}
    todo = [l for l in outlined if l not in measured and l not in busy]
    return sorted(todo, key=lambda l: (l not in running, l))


def sweep(store, args=None, *, mode='local', now=None):
    """Agent tool `terrain.sweep` (run by a timer, no model): hands the next unmeasured parcels to `geo.terrain`, one static task each, so every
    measurement is its own paced call with its own record. A listing is offered once a day at most."""
    now = time.time() if now is None else now
    limit = int((args or {}).get('limit') or SWEEP_LIMIT)
    todo = waiting(store, mode)
    day = time.strftime('%Y%m%d', time.localtime(now))
    submitted = 0
    for listing in todo[:limit]:
        store.submit(agent='research', objective='Arazi ölçümü: parselin eğimi, bakısı, rakımı ve denize uzaklığı', inputs={'listing_id': listing}, listing_ref=listing,
                     request_key=f'terrain:{listing}:{day}', source='sweep:terrain', mode=mode, priority=15, executor_tool='geo.terrain', now=now)
        submitted += 1
    return {'submitted': submitted, 'waiting': len(todo) - submitted, 'measured_total': len(stored(store, mode))}

