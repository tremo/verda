"""A listing that says "Ada No: Belirtilmemiş" and gives a parsel no: the official record is tried under ada 0, then ada 1, and believed only on the
advertised area. All data is synthetic."""
import urllib.error

import jsonschema
import pytest

from test_facts import ID, admin, fake_net, listing_payload, parcel_feature, python_listing_store, read, status, table
from verda.agency.engine import default_registry
from verda.agency.tkgm import TkgmClient
from verda.facts.extract import ada_unstated_parsel, extract, parcel_from_listing
from verda.facts.model import area_matches

ADALESS = {'parcel_key': 'Belirtilmemiş/778', 'area_m2': 5127.0, 'neighborhood': 'Kızlan',
           'attributes': {'m²': '5.127', 'tapu durumu': 'Müstakil Tapulu', 'ada no': 'Belirtilmemiş', 'parsel no': '778', 'imar durumu': 'Konut'}}


def ada_net(parcels, calls=None):
    """TKGM answering for {(ada, parsel): area text}; any other parcel is a 404. Ada 0 comes back with an empty adaNo, as the real service does."""
    administrative = fake_net()

    def fetch(url):
        if '/parsel/' not in url:
            return administrative(url)
        ident, ada, parsel = url.rsplit('/parsel/', 1)[1].split('/')
        (calls if calls is not None else []).append(f'{ada}/{parsel}')
        if (ada, parsel) not in parcels:
            raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
        return parcel_feature(area=parcels[(ada, parsel)], ada='' if ada == '0' else ada, parsel=parsel)
    return fetch


def client(tmp_path, parcels, calls=None):
    tmp_path.mkdir(exist_ok=True)
    return TkgmClient(tmp_path / 'admin.json', fetch=ada_net(parcels, calls), sleep=lambda s: None)


ARGS = {'neighborhood': 'Kızlan', 'parsel_no': '778', 'listing_id': ID}


def test_a_dotted_count_is_a_thousands_separator_and_an_unstated_ada_is_recognised():
    assert parcel_from_listing({'attributes': {'ada no': '12', 'parsel no': '1.757'}}) == '12/1757'
    assert parcel_from_listing({'parcel_key': '1.025/3', 'attributes': {}}) == '1025/3'
    assert parcel_from_listing({'parcel_key': '12/1.75', 'attributes': {}}) is None                      # not a thousands group
    assert parcel_from_listing(ADALESS) is None                                                         # no ada, no key
    assert ada_unstated_parsel(ADALESS) == '778'
    assert ada_unstated_parsel({'attributes': {'ada no': 'BELİRTİLMEMİŞ', 'parsel no': '1.757'}}) == '1757'
    for attributes in ({'ada no': '0', 'parsel no': '778'}, {'ada no': '12', 'parsel no': '778'}, {'parsel no': '778'},
                       {'ada no': 'Belirtilmemiş'}, {'ada no': 'Belirtilmemiş', 'parsel no': 'Belirtilmemiş'}, {'ada no': 'Belirtilmemiş', 'parsel no': '0'},
                       {'ada no': 'Belirtilmemiş', 'parsel no': '1.75'}):
        assert ada_unstated_parsel({'attributes': attributes}) is None


def test_whole_square_metres_match_the_official_decimals_and_nothing_else_does():
    assert area_matches(5127.77, 5127.0) and area_matches(1650.7, 1650.0) and area_matches(1120.0, 1120.0) and area_matches(1119.5, 1120.0)
    assert not area_matches(5128.5, 5127.0) and not area_matches(2000.0, 5127.0) and not area_matches(5127.0, None) and not area_matches(None, 5127.0)


def test_ada_0_is_asked_first_and_nothing_more_is_asked_when_it_matches(tmp_path):
    calls = []
    got = client(tmp_path, {('0', '778'): '5.127,77', ('1', '778'): '5.127,00'}, calls).lookup_ada_unstated(ARGS, 5127.0)
    assert got['found'] and got['parcel_key'] == '0/778' and got['official_area_m2'] == 5127.77 and got['parcel_url'].endswith('/0/778')
    assert calls == ['0/778']
    assert [t['parcel_key'] for t in got['ada_guess']['tried']] == ['0/778'] and got['ada_guess']['advertised_area_m2'] == 5127.0


def test_ada_1_is_tried_when_ada_0_is_not_that_land(tmp_path):
    calls = []
    other_land = client(tmp_path / 'a', {('0', '778'): '900,00', ('1', '778'): '5.127,40'}, calls).lookup_ada_unstated(ARGS, 5127.0)
    assert other_land['parcel_key'] == '1/778' and calls == ['0/778', '1/778']
    assert [(t['parcel_key'], t['found']) for t in other_land['ada_guess']['tried']] == [('0/778', True), ('1/778', True)]
    assert other_land['ada_guess']['tried'][0]['official_area_m2'] == 900.0                     # what was ruled out stays on the record
    missing = client(tmp_path / 'b', {('1', '778'): '5.127,40'}).lookup_ada_unstated(ARGS, 5127.0)
    assert missing['parcel_key'] == '1/778'                                                     # ada 0 does not exist at all


def test_a_parsel_number_that_fits_no_ada_is_not_found_and_says_what_was_tried(tmp_path):
    wrong_area = client(tmp_path / 'a', {('0', '778'): '900,00', ('1', '778'): '3.000,00'}).lookup_ada_unstated(ARGS, 5127.0)
    nothing = client(tmp_path / 'b', {}).lookup_ada_unstated(ARGS, 5127.0)
    for got, found in ((wrong_area, [True, True]), (nothing, [False, False])):
        assert got['found'] is False and got['parcel_key'] is None
        assert [t['found'] for t in got['ada_guess']['tried']] == found and [t['parcel_key'] for t in got['ada_guess']['tried']] == ['0/778', '1/778']


def test_a_guessed_ada_is_believed_only_on_the_advertised_area_even_if_the_tool_said_found():
    payload = {'found': True, 'parcel_key': '0/778', 'official_area_m2': 5127.77, 'property_type': 'Konut', 'observed_at': 1.0,
               'ada_guess': {'parsel_no': '778', 'advertised_area_m2': 5127.0, 'tried': []}}
    fields = lambda area, body=payload: {p.field for p in extract('tkgm.lookup', 'o1', 'local', body, ID, area)}
    assert {'parcel_key', 'sale_area_m2', 'parcel_area_m2', 'sale_scope_resolved', 'property_type'} <= fields(5127.0)
    assert fields(3000.0) == set() and fields(None) == set()                                      # the facts decide, not the arguments the tool was given
    plain = {k: v for k, v in payload.items() if k != 'ada_guess'}
    assert 'parcel_key' in fields(3000.0, plain)                                                  # a lookup of a stated parcel is unchanged


def test_the_tool_takes_a_parsel_no_with_a_listing_or_a_parcel_key_but_not_neither():
    schema = next(t for t in default_registry(__import__('verda.agency.registry', fromlist=['x']).AgencyConfig.load(None)).config.tools if t.key == 'tkgm.lookup').input_schema
    for ok in ({'parcel_key': '12/3', 'neighborhood': 'Yaka'}, {'parsel_no': '778', 'neighborhood': 'Yaka', 'listing_id': ID}):
        jsonschema.Draft202012Validator(schema).validate(ok)
    for bad in ({'neighborhood': 'Yaka'}, {'parsel_no': '778', 'neighborhood': 'Yaka'}, {'parsel_no': '0', 'neighborhood': 'Yaka', 'listing_id': ID},
                {'parsel_no': '12/3', 'neighborhood': 'Yaka', 'listing_id': ID}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(bad)


def pipeline(tmp_path, monkeypatch, parcels, **over):
    """The default registry (its context handler reads the advertised area from the facts) with the listing read served by a python handler."""
    original = TkgmClient.__init__
    monkeypatch.setattr(TkgmClient, '__init__', lambda self, cache_path=None, **kw: original(self, cache_path, fetch=ada_net(parcels), sleep=lambda s: None))
    store, served = python_listing_store(tmp_path, [listing_payload(**{**ADALESS, **over})])
    registry = default_registry(served.config, store)
    registry.handlers.update(served.handlers)
    read(store, registry, 'listing')
    return store, registry


def test_a_listing_without_an_ada_is_looked_up_before_the_seller_is_asked_and_takes_the_matching_ada(tmp_path, monkeypatch):
    store, registry = pipeline(tmp_path, monkeypatch, {('0', '778'): '5.127,77'})
    assert 'parcel_key' not in status(store)['facts']
    assert status(store)['next_action'] == {'action': 'tkgm.lookup', 'why': 'ada_not_stated_try_0_then_1', 'parsel_no': '778'}
    from verda.agency.queue import work_queue
    batch = {g['action']: g['batch'] for g in work_queue(store, {}, mode='local')['groups']}['tkgm.lookup']
    assert batch == [{'listing_id': ID, 'why': 'ada_not_stated_try_0_then_1', 'parsel_no': '778', 'neighborhood': 'Kızlan'}]

    read(store, registry, 'probe', tool='tkgm.lookup', agent='parcel', start=10, inputs=ARGS)
    view = status(store)
    assert view['parcels'] == ['0/778'] and view['facts']['parcel_key']['grade'] == 'resmi' and view['facts']['sale_area_m2']['value'] == 5127.77
    assert view['official_parcel']['found'] and view['links']['tkgm'].endswith('/100/0/778')
    assert view['next_action']['action'] != 'ask:parcel_identity' and 'tkgm.lookup' != view['next_action']['action']
    assert [r['status'] for r in table(store, "SELECT status FROM claims WHERE field='parcel_key' AND grade='resmi'")] == ['accepted']


def test_a_parsel_number_that_fits_no_ada_goes_back_to_asking_the_seller_and_records_nothing_false(tmp_path, monkeypatch):
    store, registry = pipeline(tmp_path, monkeypatch, {('0', '778'): '900,00', ('1', '778'): '3.000,00'})
    read(store, registry, 'probe', tool='tkgm.lookup', agent='parcel', start=10, inputs=ARGS)
    view = status(store)
    assert 'parcel_key' not in view['facts'] and view['parcels'] == [] and view['official_parcel'] == {'found': False}
    assert view['next_action']['action'] == 'ask:parcel_identity'                                   # asked once, never looked up again
    assert not table(store, "SELECT 1 FROM claims WHERE grade='resmi'")


def test_a_wrong_area_handed_to_the_tool_cannot_make_a_wrong_parcel_a_fact(tmp_path, monkeypatch):
    store, registry = pipeline(tmp_path, monkeypatch, {('0', '778'): '900,00'})
    # the tool reads the area from the facts, so whatever the model passes along changes nothing; extra arguments are refused by the schema
    with pytest.raises(jsonschema.ValidationError):
        registry.validate('parcel', 'tkgm.lookup', {**ARGS, 'area_m2': 900.0})
    read(store, registry, 'probe', tool='tkgm.lookup', agent='parcel', start=10, inputs=ARGS)
    assert 'parcel_key' not in status(store)['facts']


def test_the_probe_is_not_offered_without_an_advertised_area_and_not_when_a_parcel_is_known(tmp_path, monkeypatch):
    store, _ = pipeline(tmp_path, monkeypatch, {}, area_m2=None, attributes={**ADALESS['attributes'], 'm²': 'Belirtilmemiş'})
    assert status(store)['next_action']['action'] == 'ask:parcel_identity'
    store2, _ = pipeline(tmp_path / 'known', monkeypatch, {}, attributes={**ADALESS['attributes'], 'ada no': '12'}, parcel_key='12/778')
    assert status(store2)['next_action'] == {'action': 'tkgm.lookup', 'why': 'official_area_and_scope_needed', 'also': ['sahibinden.read_thread', 'geo.route']}
