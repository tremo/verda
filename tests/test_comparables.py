import pytest

from verda.agency import comparables as c
from verda.agency.opportunity import LAND, _solve, hood_of, listing_view

NOW = 20_000_000.0


def add(data, i, hood='Yaka', kind='Zeytinlik', area=4000, rate=2000, **overrides):
    rows, statuses, dates = data
    i = str(i)
    values = {'parcel_key': f'100/{i}', 'price_tl': rate * area, 'sale_area_m2': area,
              'sale_scope_resolved': True, 'access': 'direct', 'shared_title': False,
              'natural_sit': 'none', 'archaeological_sit': False, 'property_type': kind}
    values.update(overrides)
    facts = {k: {'value': v, 'grade': 'resmi', 'claim_id': f'{i}:{k}'} for k, v in values.items() if v is not None}
    rows.append({'id': i, 'price': rate * area, 'area': area, 'type': kind, 'neighborhood': hood + ' Mh.', 'parcel': values['parcel_key']})
    statuses[i] = {'facts': facts, 'parcels': [values['parcel_key']], 'conflicts': []}
    dates[f'{i}:price_tl'] = NOW - 86400
    return rows[-1]


def estimator(data):
    rows, statuses, dates = data
    return c.Estimator(c.prepare(rows, statuses, dates, NOW), hood_of, LAND, _solve)


def small():
    data = [], {}, {}
    add(data, 999)
    for i, rate in enumerate((1000, 2000, 3000, 4000), 1):
        add(data, i, area=3000 + i * 500, rate=rate)
    return data


def market():
    data = [], {}, {}
    add(data, 999)
    i = 0
    for hood, level in [('Yaka', 2000), ('Mesudiye', 3000), ('Cumalı', 1500)]:
        for kind, factor in [('Tarla', 1.0), ('Zeytinlik', 1.1), ('Bademlik', 1.05)]:
            for n in range(12):
                i += 1
                add(data, i, hood, kind, area=3000 + n * 100, rate=level * factor)
    return data


def estimate(data, area=4000, listing='999'):
    return estimator(data).estimate('Zeytinlik', area, 'Yaka', listing)


def test_same_peers_give_same_median_at_different_subject_areas_without_any_size_adjustment():
    data = small()
    for area in (4000, 5000):
        v = estimate(data, area)
        assert v['tier'] == 'datca' and v['n'] == 4 and v['fair'] == 2500
        assert all(p['rate'] == p['adjusted'] for p in v['comps'])
        assert v['size_adjusted'] is False


def test_all_datca_remains_in_the_pool_when_a_fourth_local_peer_arrives():
    data = [], {}, {}
    add(data, 999)
    for i in range(1, 4):
        add(data, i, rate=3000)
    for i in range(4, 14):
        add(data, i, hood='Cumalı', rate=1000)
    before = estimate(data)
    add(data, 14, rate=3000)
    after = estimate(data)
    assert before['tier'] == after['tier'] == 'datca'
    assert before['n'] == 13 and after['n'] == 14
    assert {p['neighborhood'] for p in after['comps']} == {'Yaka Mh.', 'Cumalı Mh.'}
    assert before['fair'] < 3000 and after['fair'] < 3000  # no switch to the local-only median


@pytest.mark.parametrize('field,value', [
    ('access', None), ('access', 'none'),
    ('shared_title', None), ('shared_title', True), ('sale_scope_resolved', None),
    ('natural_sit', None), ('natural_sit', 'first_degree'),
    ('archaeological_sit', None), ('archaeological_sit', True),
    ('price_tl', None), ('sale_area_m2', None), ('user_excluded', True),
])
def test_unconfirmed_or_ineligible_peers_do_not_enter_price_or_coefficient_learning(field, value):
    data = small()
    r = add(data, 10, rate=1000000, **{field: value})
    r['road'] = 'ok'                 # an old archive flag cannot override current conditional/unknown access
    v = estimate(data)
    assert v['fair'] == 2500 and v['n'] == 4
    assert all('10' not in p['ids'] for p in v['comps'])


def test_direct_access_does_not_mix_with_easements_or_different_sit_states():
    data = small()
    add(data, 10, rate=100, access='existing_easement')
    add(data, 11, rate=100, natural_sit='second_degree')
    v = estimate(data)
    assert v['fair'] == 2500 and v['n'] == 4
    assert next(x['n'] for x in v['exclusions'] if x['reason'] == 'conditions') == 2
    # The same rule works for an easement subject, with its own comparable group.
    data[1]['999']['facts']['access']['value'] = 'existing_easement'
    assert estimate(data)['fair'] is None


def test_conditional_subject_gets_a_reference_from_its_own_access_group_only():
    data = small()
    data[1]['999']['facts']['access']['value'] = 'corridor_conditional'
    for i, rate in enumerate((1000, 2000, 3000, 4000), 10):
        add(data, i, rate=rate, access='corridor_conditional')
    v = estimate(data)
    assert v['fair'] == 2500 and v['n'] == 4 and v['confidence'] == 'low'
    assert v['conditions']['access'] == 'corridor_conditional'
    assert all(p['access'] == 'corridor_conditional' for p in v['comps'])
    assert {p['ids'][0] for p in v['comps']} == {'10', '11', '12', '13'}
    assert any('Yol erişimi koşullu' in warning for warning in v['warnings'])
    # A later negative finding removes that parcel, rather than preserving its tentative price.
    data[1]['10']['facts']['access']['value'] = 'none'
    assert estimate(data)['n'] == 3
    data[1]['999']['facts']['access']['value'] = 'none'
    assert estimate(data)['fair'] is None


def test_conditional_peers_do_not_change_a_confirmed_road_reference():
    data = small()
    add(data, 10, rate=1000000, access='corridor_conditional')
    v = estimate(data)
    assert v['fair'] == 2500 and v['n'] == 4
    assert all(p['access'] == 'direct' for p in v['comps'])


def test_conditional_road_warning_survives_sparse_data_and_a_well_supported_fit():
    data = market()
    for status in data[1].values():
        status['facts']['access']['value'] = 'corridor_conditional'
    v = estimate(data)
    assert v['fit_n'] == 108 and v['fair'] is not None and v['confidence'] == 'low'
    sparse = estimate(data, area=50000)
    assert sparse['fair'] is None and any('Yol erişimi koşullu' in w for w in sparse['warnings'])


def test_unknown_subject_access_still_requires_a_known_access_state():
    data = small()
    data[1]['999']['facts'].pop('access')
    v = estimate(data)
    assert v['fair'] is None and v['subject_issues'] == ['access']


def test_missing_identity_and_scope_conflicts_block_the_subject():
    data = small()
    data[1]['999']['conflicts'] = ['sale_scope']
    assert estimate(data)['fair'] is None
    data[1]['999']['conflicts'] = []
    data[1]['999']['parcels'] = []
    data[0][0]['parcel'] = None
    assert estimate(data)['fair'] is None


def test_other_parcel_is_counted_once_across_neighbourhood_and_parcel_spellings():
    data = small()
    add(data, 10, hood='Yaka', parcel_key='Yaka:100/1', area=3500, rate=1000)
    data[0][-1]['neighborhood'] = 'Köyler Yaka Mahallesi'
    v = estimate(data)
    assert v['n'] == 4 and v['fair'] == 2500
    assert {'1', '10'} == set(next(p['ids'] for p in v['comps'] if '1' in p['ids']))


def test_subject_price_and_all_its_aliases_are_excluded_even_from_learned_coefficients():
    data = market()
    before = estimate(data)
    assert before['fit_n'] > 0
    data[1]['999']['facts']['price_tl']['value'] *= 1000
    add(data, 998, parcel_key='Yaka:100/999', rate=1000000)
    data[0][-1]['neighborhood'] = 'Yaka Mahallesi'
    after = estimate(data)
    alias = estimate(data, listing='998')
    assert before['fair'] == pytest.approx(after['fair']) == pytest.approx(alias['fair'])
    assert before['comps'] == after['comps'] == alias['comps']
    assert all(not {'999', '998'} & set(p['ids']) for p in after['comps'])


def test_overlapping_sale_packages_and_contradictory_duplicate_access_are_not_two_comparables():
    data = small()
    add(data, 10, parcel_key='100/1', area=3500, rate=1000)
    data[1]['10']['parcels'] = ['100/1', '100/50']
    v = estimate(data)
    assert v['n'] == 3 and all(not {'1', '10'} & set(p['ids']) for p in v['comps'])
    data[1]['10']['parcels'] = ['100/1']
    data[1]['10']['facts']['access']['value'] = 'corridor_conditional'
    assert estimate(data)['n'] == 3


def test_age_changes_weight_and_stale_or_removed_ads_are_excluded():
    data = small()
    data[2]['1:price_tl'] = NOW - 181 * 86400
    data[2]['2:price_tl'] = None
    data[2]['3:price_tl'] = NOW - 100 * 86400
    v = estimate(data)
    assert v['n'] == 3 and v['confidence'] == 'low'
    cs = {p['ids'][0]: p for p in v['comps']}
    assert cs['2']['weight'] < cs['3']['weight'] < cs['4']['weight']
    assert cs['2']['age_days'] is None
    data[1]['4']['seen'] = {'listing_removed': True}
    assert estimate(data)['fair'] is None


def test_price_age_uses_observation_time_not_import_or_claim_creation_time():
    from contextlib import contextmanager
    import sqlite3
    from types import SimpleNamespace
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.executescript('''CREATE TABLE claims(id, created_at, source_kind, observation_id, mode, field, status);
        CREATE TABLE observations(id, mode, tool, captured_at);''')
    con.executemany('INSERT INTO observations VALUES(?,?,?,?)', [
        ('ad', 'local', 'sahibinden.read_listing', 100), ('archive', 'local', 'legacy.import', 900)])
    con.executemany('INSERT INTO claims VALUES(?,?,?,?,?,?,?)', [
        ('observed', 900, 'listing', 'ad', 'local', 'price_tl', 'accepted'),
        ('imported', 900, 'listing', 'archive', 'local', 'price_tl', 'accepted'),
        ('edited', 800, 'user', None, 'local', 'price_tl', 'accepted'),
        ('rejected', 900, 'listing', 'ad', 'local', 'price_tl', 'rejected')])

    @contextmanager
    def transaction():
        yield con
    try:
        assert c.price_dates(SimpleNamespace(transaction=transaction), 'local') == {'observed': 100, 'imported': None, 'edited': 800}
    finally:
        con.close()


def test_only_verified_fields_set_reliability_and_the_basis_exposes_every_peer_weight():
    data = small()
    data[1]['1']['facts']['access']['grade'] = 'beyan'
    v = estimate(data)
    assert sum(p['weight_pct'] for p in v['comps']) == pytest.approx(100, abs=0.2)
    cs = {p['ids'][0]: p for p in v['comps']}
    assert cs['1']['weight'] < cs['2']['weight']
    assert cs['1']['grades']['access'] == 'beyan'
    assert v['low'] <= v['fair'] <= v['high']
    assert 'eğim' in v['unmodeled'][0]


def test_learning_shares_information_across_land_types_and_regions_without_claiming_exact_factors():
    data = market()
    v = estimate(data)
    assert v['tier'] == 'datca' and v['fit_n'] == 108 and v['confidence'] == 'medium'
    assert v['fair'] == pytest.approx(2200, rel=0.15)
    assert {p['type'] for p in v['comps']} == set(LAND)
    by_hood = {p['neighborhood']: p for p in v['comps'] if p['type'] == 'Zeytinlik'}
    assert by_hood['Mesudiye Mh.']['region_factor'] < 1 < by_hood['Cumalı Mh.']['region_factor']
    assert estimate(data, area=5000)['fair'] == v['fair']  # identical band membership, identical result
    assert estimate(data, area=50000)['fair'] is None


def test_fit_cannot_confuse_a_type_only_seen_in_one_isolated_hood_with_a_location_premium():
    data = [], {}, {}
    add(data, 999)
    for i in range(1, 26):
        add(data, i, hood='Yaka', kind='Zeytinlik', rate=2000)
    for i in range(26, 51):
        add(data, i, hood='Cumalı', kind='Bademlik', rate=500)
    v = estimate(data)
    assert v['fit_n'] == 0 and v['confidence'] == 'low'
    assert any(p['region_factor'] is None for p in v['comps'])


def test_extreme_peer_does_not_pull_the_weighted_median_towards_it():
    data = market()
    before = estimate(data)
    add(data, 200, hood='Cumalı', kind='Tarla', rate=1_000_000)
    after = estimate(data)
    assert after['fair'] == pytest.approx(before['fair'], rel=0.05)


def test_these_peers_are_shown_beside_the_expected_price_and_a_thin_reference_never_reads_as_a_strong_opportunity():
    # Since 12 Nov 2026 the score's yardstick is value.Model (size, neighbourhood, sea, height); these peers are listed beside it for the reader.
    # Handed to the score on its own, a low-confidence reference counts at half weight, is never "strong" and raises no "very cheap" flag.
    v = estimate(small())
    row = {'type': 'Zeytinlik', 'area_m2': 4000, 'price_tl': 400_000, 'route_minutes_max': 15, 'penalties': {}}
    shown = listing_view(row, v, 0.5, '', {'value': 'direct'}, True)
    assert shown['gap_pct'] == 96 and shown['label'] == 'Olası fırsat'
    assert shown['basis']['method'] == c.METHOD and shown['basis']['confidence'] == 'low' and shown['basis']['comps'] == v['comps']
    assert all('çok ucuz' not in cap for cap in shown['caps']) and any('az veriye' in cap for cap in shown['caps'])
    sure = listing_view(row, {**v, 'confidence': 'medium'}, 0.5, '', {'value': 'direct'}, True)
    assert shown['opportunity'] <= sure['opportunity'] and any('çok ucuz' in cap for cap in sure['caps'])
