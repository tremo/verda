"""The official sit check: outline against the ministry's layer, then a fact, then the decision. All data is synthetic."""
import json

from test_facts import ID, client, fake_net, listing_payload, parcel_feature, python_listing_store, read, status, table
from verda.agency import says
from verda.facts.extract import extract

PARCEL = [[27.5, 36.7], [27.6, 36.7], [27.6, 36.8], [27.5, 36.8], [27.5, 36.7]]


def area(status_text, x0=27.45, x1=27.65, y0=36.65, y1=36.85):
    return {'type': 'Feature', 'properties': {'SitDurum': status_text, 'KararNo': '7377', 'KararTarih': '21.07.2011', 'Kisitlar': '728 nolu İlke Kararı'},
            'geometry': {'type': 'Polygon', 'coordinates': [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}}


def layer_of(*features):
    return {'features': list(features)}


def test_the_share_of_the_parcel_inside_each_degree_decides_the_class():
    assert says.classify(says.overlap(PARCEL, layer_of())['fractions']) == 'none'
    assert says.classify(says.overlap(PARCEL, layer_of(area('2.Derece Doğal Sit Alanı')))['fractions']) == 'second'
    # the whole parcel in a second-degree area and a corner in a first-degree one: the stricter degree counts once it is more than a sliver
    corner = area('1.Derece Doğal Sit Alanı', 27.45, 27.53, 36.65, 36.85)
    assert says.classify(says.overlap(PARCEL, layer_of(area('2.Derece Doğal Sit Alanı'), corner))['fractions']) == 'first'
    sliver = area('2.Derece Doğal Sit Alanı', 27.45, 27.505, 36.65, 36.85)
    assert says.classify(says.overlap(PARCEL, layer_of(sliver))['fractions']) == 'boundary'          # 5 % of the width: not a sit
    # areas that are not natural-sit degrees (sensitive-area zones) never make a sit
    assert says.classify(says.overlap(PARCEL, layer_of(area('Kesin Korunacak Hassas Alan')))['fractions']) == 'none'


def test_only_a_clear_answer_becomes_a_natural_sit_fact():
    got = lambda c: extract('sit.says', 'o1', 'local', {'listing_id': ID, 'classification': c, 'layer': 'tabiatvarlik:VatandasSitAlani'}, ID)
    assert [(p.field, p.value, p.proposer) for p in got('second')] == [('natural_sit', 'second_degree', 'code:says_overlay@1')]
    assert got('none')[0].value == 'none' and got('first')[0].value == 'first_degree'
    assert got('boundary') == [] and got('no_geometry') == []


def official_store(tmp_path, features):
    box = {}
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'x', fetch=fake_net(parcel_feature())).lookup,
        'sit.says': lambda a: says.check(box['store'], a, now=5.0, fetch=lambda url: json.dumps(layer_of(*features)).encode())})
    box['store'] = store
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    read(store, registry, 'sit', tool='sit.says', agent='parcel', start=20)
    return store


def test_a_first_degree_sit_on_the_official_map_eliminates_before_anyone_is_asked(tmp_path):
    store = official_store(tmp_path, [area('1.Derece Doğal Sit Alanı')])
    view = status(store)
    assert view['facts']['natural_sit'] == {**view['facts']['natural_sit'], 'value': 'first_degree', 'grade': 'resmi', 'source_kind': 'official'}
    assert view['decision']['state'] == 'excluded' and 'first_degree_natural_sit' in view['decision']['reasons']
    assert view['seen']['says_checked'] is True


def test_an_official_no_sit_overrides_nothing_but_a_seller_who_says_otherwise_stays_visible(tmp_path):
    store = official_store(tmp_path, [])
    view = status(store)
    assert view['facts']['natural_sit']['value'] == 'none' and view['facts']['natural_sit']['grade'] == 'resmi'
    assert 'natural_sit' not in view['decision']['reasons']          # the archaeological question is still open, the natural one is settled
    assert table(store, "SELECT tool FROM observations WHERE tool='sit.says'")


def test_the_official_sit_check_is_queued_before_any_question_goes_out(tmp_path):
    from verda.agency.queue import work_queue
    box = {}
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'x', fetch=fake_net(parcel_feature())).lookup,
        'sit.says': lambda a: says.check(box['store'], a, now=5.0, fetch=lambda url: json.dumps(layer_of()).encode())})
    box['store'] = store
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    before = work_queue(store, {}, mode='local')
    assert [g['action'] for g in before['groups'] if g['action'] == 'sit.says'] == ['sit.says']
    assert ID in before['skipped_ids'].get('waits_for_says', []) or not any(g['action'] == 'sahibinden.send_message' for g in before['groups'])
    read(store, registry, 'sit', tool='sit.says', agent='parcel', start=20)
    after = work_queue(store, {}, mode='local')
    assert not any(g['action'] == 'sit.says' for g in after['groups'])


def test_every_deed_of_a_sale_is_overlaid_and_a_changed_parcel_is_checked_again(tmp_path):
    """A second deed (ring to the east) lies in a first-degree area: the sale is first degree although the main parcel is clear.
    After the parcels change, the old overlay no longer covers the sale."""
    east = [[27.7, 36.7], [27.8, 36.7], [27.8, 36.8], [27.7, 36.8], [27.7, 36.7]]
    box = {}
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': lambda a: client(tmp_path / ('x' + a['parcel_key'].replace('/', '_')),
                                        fetch=fake_net(parcel_feature(parsel=a['parcel_key'].split('/')[1], ring=east if a['parcel_key'] == '123/46' else None))).lookup(a),
        'sit.says': lambda a: says.check(box['store'], a, now=5.0, fetch=lambda url: json.dumps(layer_of(area('1.Derece Doğal Sit Alanı', 27.65, 27.85, 36.65, 36.85))).encode())})
    box['store'] = store
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    assert says.stale(store, ID, 'local', ['123/45']) is True                     # never checked
    read(store, registry, 'sit', tool='sit.says', agent='parcel', start=20)
    assert status(store)['facts']['natural_sit']['value'] == 'none' and says.stale(store, ID, 'local', ['123/45']) is False
    with store.transaction() as con:
        store.facts.decide(con, ID, 'local', 'sale_parcels', ['123/45', '123/46'], 'ikinci tapu', 30.0)
    view = status(store)
    assert view['parcels'] == ['123/45', '123/46'] and view['next_action'] == {'action': 'tkgm.lookup', 'why': 'sale_parcel_unchecked', 'parcel': '123/46', 'part': True}
    read(store, registry, 'tkgm2', tool='tkgm.lookup', agent='parcel', start=40, inputs={'parcel_key': '123/46', 'neighborhood': 'Kızlan', 'part': True})
    assert says.stale(store, ID, 'local', ['123/45', '123/46']) is True          # the sale grew: the earlier overlay does not cover it
    read(store, registry, 'sit2', tool='sit.says', agent='parcel', start=50)
    view = status(store)
    assert view['decision']['state'] == 'excluded' and 'first_degree_natural_sit' in view['decision']['reasons']
    total = view['facts']['sale_area_m2']
    assert total['value'] == 3020.8 and total['source_ref'] == '123/45 + 123/46' and len(total['parts']) == 2      # 1510.40 per deed in the fake
