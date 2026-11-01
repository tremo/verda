"""Drive time from a parcel to the destination, from open routing engines.

Two different engines (OSRM and Valhalla) over the same OpenStreetMap data: a cross-check of the
engines, not independent evidence of the roads. No traffic. The start is the parcel centroid
snapped to the nearest road; the snap distance is recorded as the "last metres" uncertainty.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from verda.agency.registry import ResourceBlocked, TransientToolError
from verda.facts.service import main_lookup

OSRM = 'https://router.project-osrm.org/route/v1/driving/{o};{d}?overview=false'
VALHALLA = 'https://valhalla1.openstreetmap.de/route?json={json}'
UA = 'Verda-route/2.0 (personal land research; low volume)'
NOTE = ('Trafik içermez; iki motor aynı OpenStreetMap yol verisini kullanır (motor çapraz kontrolü, yol için bağımsız kanıt değil); '
        'çıkış noktası parsel merkezinin en yakın yola izdüşümüdür.')


def get_json(url, timeout=25.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def straight_km(a, b) -> float:
    la1, la2 = math.radians(a['lat']), math.radians(b['lat'])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b['lon'] - a['lon']) / 2) ** 2
    return round(2 * 6371.0088 * math.asin(math.sqrt(h)), 2)


def _osrm(fetch, o, d):
    data = fetch(OSRM.format(o=f"{o['lon']},{o['lat']}", d=f"{d['lon']},{d['lat']}"))
    route = data['routes'][0]
    snaps = [round(w.get('distance', 0)) for w in data.get('waypoints', [])]
    return {'minutes': round(route['duration'] / 60, 1), 'km': round(route['distance'] / 1000, 2),
            'origin_snap_m': snaps[0] if snaps else None, 'destination_snap_m': snaps[-1] if snaps else None}


def _valhalla(fetch, o, d):
    body = json.dumps({'locations': [{'lon': o['lon'], 'lat': o['lat']}, {'lon': d['lon'], 'lat': d['lat']}], 'costing': 'auto', 'units': 'kilometers'})
    summary = fetch(VALHALLA.format(json=urllib.parse.quote(body)))['trip']['summary']
    return {'minutes': round(summary['time'] / 60, 1), 'km': round(summary['length'], 2), 'origin_snap_m': None, 'destination_snap_m': None}


ENGINES = {'osrm': _osrm, 'valhalla': _valhalla}
NEAREST = 'https://router.project-osrm.org/nearest/v1/driving/{o}?number=1'


class NoRoad(Exception):
    """The engine found no road near the start point."""


def _attempt(name, fetch, origin, destination):
    try:
        return ENGINES[name](fetch, origin, destination)
    except urllib.error.HTTPError as error:
        if error.code == 400:                      # OSRM NoSegment / Valhalla "no suitable edges near location"
            raise NoRoad from None
        raise
    except (KeyError, IndexError):
        raise NoRoad from None


def nearest_road(fetch, point):
    """The closest road point to `point` (OSRM nearest), or None."""
    try:
        waypoint = fetch(NEAREST.format(o=f"{point['lon']},{point['lat']}"))['waypoints'][0]
        lon, lat = waypoint['location']
        return {'lat': lat, 'lon': lon, 'distance_m': round(waypoint.get('distance', 0))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, IndexError, ValueError, TypeError):
        return None


def route(origin, destination, fetch=get_json, sleep=time.sleep, delay=1.0) -> dict:
    results, snapped = [], None
    for index, name in enumerate(ENGINES):
        if index:
            sleep(delay)
        try:
            try:
                got = _attempt(name, fetch, origin, destination)
            except NoRoad:
                # No road near the point: route from the nearest road instead, and say so.
                snapped = snapped or nearest_road(fetch, origin) or {}
                if not snapped:
                    results.append({'engine': name, 'ok': False, 'error': 'no_road_near_origin'})
                    continue
                sleep(delay)
                got = {**_attempt(name, fetch, snapped, destination), 'fallback': 'nearest_road', 'origin_snap_m': snapped['distance_m']}
            results.append({'engine': name, 'ok': True, **got})
        except NoRoad:
            results.append({'engine': name, 'ok': False, 'error': 'no_road_near_origin'})
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                raise ResourceBlocked(f'{name}_http_{error.code}') from None
            results.append({'engine': name, 'ok': False, 'error': f'http_{error.code}'})
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            results.append({'engine': name, 'ok': False, 'error': 'unreachable'})
        except (ValueError, TypeError):
            results.append({'engine': name, 'ok': False, 'error': 'unexpected_response'})
    if not any(r['ok'] for r in results):
        if all(r.get('error') == 'no_road_near_origin' for r in results):
            return {'origin': origin, 'destination': destination, 'straight_line_km': straight_km(origin, destination),
                    'engines': results, 'note': 'Başlangıç noktasına yakın yol bulunamadı; elle inceleme gerekir.'}
        raise TransientToolError('routing_unavailable')
    return {'origin': origin, 'destination': destination, 'straight_line_km': straight_km(origin, destination),
            'engines': results, 'note': NOTE}


def listing_route(store, args, *, mode, policy=None, fetch=get_json, sleep=time.sleep):
    """Route for a listing whose official parcel record is already stored."""
    from verda.policy import Policy
    policy = policy or Policy()
    listing = args['listing_id']
    from verda.agency.research import fact_status
    parcels = fact_status(store, listing, mode=mode).get('parcels')
    with store.transaction() as con:
        parcel = main_lookup(con, listing, mode, parcels)      # the sale's own parcel (not an extra deed, not a number the user has corrected away)
    if not parcel or parcel.get('found') is not True:
        return {'listing_id': listing, 'engines': [], 'reason': 'official_parcel_required'}
    lon, lat = parcel['geometry']['centroid']
    dest = policy.destination
    origin = {'lat': lat, 'lon': lon, 'basis': 'parcel_centroid', 'parcel_key': parcel['parcel_key']}
    return {'listing_id': listing, **route(origin, {'lat': dest['lat'], 'lon': dest['lon'], 'name': dest['name']}, fetch, sleep)}
