"""The land itself: slope, the side it faces, height and the distance to the sea, from the outline and a height model. All data is synthetic."""
import json
import re
import urllib.error

import pytest

from test_facts import ID, client, fake_net, listing_payload, parcel_feature, python_listing_store, read, table
from verda.agency import terrain
from verda.agency.corridor import Plane
from verda.agency.engine import AgentEngine
from verda.agency.registry import ResourceBlocked, TransientToolError

RING = [[27.5000, 36.7000], [27.5010, 36.7000], [27.5010, 36.7008], [27.5000, 36.7008], [27.5000, 36.7000]]      # about 89 m by 88 m


def heights(surface, calls=None):
    """An elevation service that answers `surface(x east, y north)` in metres around the ring's middle."""
    lon0, lat0 = terrain.centre(RING)
    plane = Plane(lon0, lat0)

    def fetch(url):
        if calls is not None:
            calls.append(url)
        points = [tuple(map(float, p.split(','))) for p in re.search(r'locations=(.+)$', url).group(1).split('|')]
        return {'status': 'OK', 'results': [{'elevation': surface(*plane.to_xy(lon, lat)), 'location': {'lat': lat, 'lng': lon}} for lat, lon in points]}
    return fetch


def test_a_plane_through_the_heights_gives_the_slope_and_the_side_the_land_faces():
    calls = []
    south = terrain.of_ring(RING, heights(lambda x, y: 100 + 0.10 * y, calls))        # higher to the north: the ground falls towards the south
    assert south['slope_pct'] == pytest.approx(10, abs=0.2) and south['faces'] == 'güney' and south['slope_class'] == 'hafif eğimli'
    assert south['elevation_m'] == 100 and south['samples'] == 25 and south['span_m'] == 120
    assert len(calls) == 1 and calls[0].count('|') == 24                                  # one request for the whole grid
    west = terrain.of_ring(RING, heights(lambda x, y: 50 + 0.30 * x))
    assert west['faces'] == 'batı' and west['slope_class'] == 'dik' and west['slope_pct'] == pytest.approx(30, abs=0.3)
    flat = terrain.of_ring(RING, heights(lambda x, y: 12 + 0.01 * x))
    assert flat['slope_class'] == 'düz' and flat['faces'] is None and flat['aspect_deg'] is None        # under 4 % the direction is noise
    steep = terrain.of_ring(RING, heights(lambda x, y: 300 - 0.5 * y))
    assert steep['faces'] == 'kuzey' and steep['slope_class'] == 'çok dik' and steep['relief_m'] >= 25


def test_the_height_service_refusing_stops_the_source_and_an_outage_is_only_tried_again():
    def refusing(code):
        def fetch(url):
            raise urllib.error.HTTPError(url, code, 'x', {}, None)
        return fetch
    with pytest.raises(ResourceBlocked, match='opentopodata_http_429'):
        terrain.of_ring(RING, refusing(429))
    with pytest.raises(TransientToolError):
        terrain.of_ring(RING, refusing(503))
    with pytest.raises(TransientToolError):
        terrain.of_ring(RING, lambda url: {'status': 'OK', 'results': []})           # fewer answers than points
    assert terrain.of_ring(RING, lambda url: {'status': 'OK', 'results': [{'elevation': None}] * 25}) is None     # no values there: nothing is made up
    with pytest.raises(ValueError):
        terrain.elevations([(36.7, 27.5)] * 101)


def test_the_distance_to_the_sea_comes_from_the_shore_points_and_ends_with_the_mapped_region(tmp_path):
    shore = tmp_path / 'shore.json'
    shore.write_text(json.dumps({'cell_m': 400, 'points': [[27.5, 36.70], [27.6, 36.70]]}))
    assert terrain.coast_m(27.5, 36.70, shore) == 0
    assert terrain.coast_m(27.5, 36.72, shore) == pytest.approx(2211 - 200, abs=15)      # 0.02 degrees north, less half a cell
    assert terrain.coast_m(29.0, 37.5, shore) is None
    points, cell = terrain.shore_points()                                                  # the file that ships with the code
    assert len(points) > 300 and cell == 400 and terrain.coast_m(27.512604, 36.676853) == 0    # the destination on the shore of Palamutbükü


def terrain_store(tmp_path, surface=lambda x, y: 100 + 0.2 * y, calls=None, ring=None):
    box = {}
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'x', fetch=fake_net(parcel_feature(ring=ring or RING))).lookup,
        'geo.terrain': lambda a: terrain.listing_terrain(box['store'], a, mode='local', fetch=heights(surface, calls)),
        'terrain.sweep': lambda a: terrain.sweep(box['store'], a, mode='local', now=box.get('now'))})
    box['store'] = store
    return store, registry, box


def test_the_tool_measures_the_sales_own_parcel_and_says_so_when_there_is_no_outline(tmp_path):
    store, registry, _ = terrain_store(tmp_path)
    read(store, registry, 'listing')
    assert terrain.listing_terrain(store, {'listing_id': ID}, mode='local', fetch=lambda url: 1 / 0)['reason'] == 'official_parcel_with_geometry_required'
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    read(store, registry, 'terrain', tool='geo.terrain', agent='research', start=20)
    measured = terrain.stored(store)[ID]
    assert measured['parcel_key'] == '123/45' and measured['faces'] == 'güney' and measured['slope_pct'] == pytest.approx(20, abs=0.3)
    assert measured['dataset'] == terrain.DATASET and isinstance(measured['coast_m'], (int, float)) and len(measured['centre']) == 2
    assert table(store, "SELECT listing_ref FROM observations WHERE tool='geo.terrain'") == [{'listing_ref': ID}]


def test_the_sweep_hands_each_unmeasured_parcel_to_the_tool_once_without_a_model(tmp_path):
    calls = []
    store, registry, box = terrain_store(tmp_path, calls=calls)
    read(store, registry, 'listing')
    assert terrain.sweep(store, {}, now=100.0) == {'submitted': 0, 'waiting': 0, 'measured_total': 0}        # no outline yet: nothing to measure
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    assert terrain.waiting(store) == [ID]
    box['now'] = 1000.0
    assert terrain.sweep(store, {}, now=1000.0)['submitted'] == 1
    assert terrain.sweep(store, {}, now=1001.0)['submitted'] == 0                                              # already handed out
    engine = AgentEngine(store, registry, providers={})                                                         # no provider: a model call would fail
    for n in range(4):
        engine.step(now=1010.0 + n)
    assert len(calls) == 1 and ID in terrain.stored(store)
    assert table(store, "SELECT state, executor_tool FROM inbox WHERE executor_tool='geo.terrain'") == [{'state': 'complete', 'executor_tool': 'geo.terrain'}]
    assert terrain.sweep(store, {}, now=2000.0) == {'submitted': 0, 'waiting': 0, 'measured_total': 1}
