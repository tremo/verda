"""What a parcel of this size in this place is usually asked for, and how the land's quality weighs a low price. All data is synthetic."""
import math

import pytest

from verda.agency import comparables, opportunity, value
from verda.agency.opportunity import LAND, _solve, listing_view, purpose_fit

HOODS = {'Yaka Mh.': -0.25, 'Mesudiye Mh.': 0.45, 'Cumalı Mh.': 0.0, 'Yazı Mh.': -0.40}
SIZE = -0.55


def market(noise=0.25, per_hood=16):
    """Price rows that follow a known rule: 3.000 ₺/m² for 3.000 m² in an average place, less per m² for a larger parcel, a level per neighbourhood."""
    rows, n = [], 0
    for hood, level in HOODS.items():
        for i in range(per_hood):
            area = 1000 * 1.22 ** i                                                  # 1.000 to about 20.000 m²
            wobble = noise * math.sin(1.7 * n)                                        # the same "noise" every run
            rate = 3000 * math.exp(SIZE * math.log(area / 3000) + level + wobble)
            rows.append({'id': str(1000000000 + n), 'price': rate * area, 'area': area, 'type': LAND[n % 3], 'neighborhood': hood, 'parcel': f'{n + 1}/1'})
            n += 1
    return rows


def model(rows, land=None):
    return value.Model(value.rows_from(rows, LAND, land or {}), _solve)


def test_the_expected_price_follows_size_and_place_as_the_market_does():
    m = model(market())
    at = lambda area, hood: m.estimate('Tarla', area, hood)['fair']
    assert at(3000, 'Cumalı Mh.') == pytest.approx(3000, rel=0.15)
    assert at(6000, 'Cumalı Mh.') / at(3000, 'Cumalı Mh.') == pytest.approx(2 ** SIZE, rel=0.08)          # twice the size: about a third less per m²
    assert at(3000, 'Mesudiye Mh.') / at(3000, 'Yaka Mh.') == pytest.approx(math.exp(0.70), rel=0.20)      # the dear place against the cheap one
    seen = m.estimate('Zeytinlik', 7706, 'Yaka Mh.')
    assert seen['tier'] == 'model' and seen['method'] == value.METHOD and seen['confidence'] == 'medium' and seen['low'] < seen['fair'] < seen['high']
    drivers = {d['key']: d['pct'] for d in seen['drivers']}
    assert drivers['size'] < -30 and drivers['hood'] < -10 and 'coast' not in drivers                      # what moved the expectation, and by how much
    assert seen['size_elasticity'] == pytest.approx(SIZE, abs=0.08) and 'satış fiyatı değildir' in seen['note']


def test_a_listing_is_judged_by_a_fit_that_has_not_seen_its_own_price():
    rows = market()
    plain = model(rows).estimate('Tarla', rows[5]['area'], rows[5]['neighborhood'], rows[5]['id'])['fair']
    absurd = [{**r, 'price': r['price'] * (0.05 if r['id'] == rows[5]['id'] else 1)} for r in rows]       # its own price falls to a twentieth
    after = model(absurd).estimate('Tarla', rows[5]['area'], rows[5]['neighborhood'], rows[5]['id'])['fair']
    assert after == pytest.approx(plain, rel=0.03)
    twice = rows + [{**rows[5], 'id': '2000000000'}]                                                       # the same parcel under a second listing counts once
    assert len(value.rows_from(twice, LAND, {})) == len(rows)


def test_too_little_to_go_on_gives_no_number_and_a_thin_neighbourhood_says_so():
    few = model(market(per_hood=5))
    assert few.estimate('Tarla', 3000, 'Yaka Mh.')['tier'] == 'none' and few.estimate('Tarla', 3000, 'Yaka Mh.')['fair'] is None
    m = model(market() + [{'id': '3000000001', 'price': 9e6, 'area': 3000.0, 'type': 'Tarla', 'neighborhood': 'Emecik Mh.', 'parcel': '9/9'}])
    thin = m.estimate('Tarla', 3000, 'Emecik Mh.')
    assert thin['confidence'] == 'low' and any('mahalle düzeyi öğrenilemedi' in w for w in thin['warnings']) and thin['fair'] is not None
    assert m.estimate('Tarla', None, 'Yaka Mh.')['tier'] == 'none' and m.estimate('Tarla', 3000, '')['tier'] == 'none'
    rows = value.rows_from(market() + [{'id': 'x', 'price': 1.0, 'area': 3000.0, 'type': 'Tarla', 'neighborhood': 'Yaka Mh.', 'parcel': None},       # an absurd rate
                                       {'id': 'y', 'price': 9e6, 'area': 3000.0, 'type': 'Arsa / imarlı', 'neighborhood': 'Yaka Mh.', 'parcel': None}], LAND, {})
    assert len(rows) == len(market())


def test_the_sea_and_the_height_move_the_expectation_only_where_they_were_measured():
    rows = market(noise=0.05)
    land = {}
    for n, r in enumerate(rows):                                               # nearer the sea, dearer: the rule the fit should find
        coast = 300 * 1.06 ** n
        land[r['id']] = {'coast_m': coast, 'elevation_m': 120}
        r['price'] *= math.exp(-0.2 * math.log(coast / 2000))
    m = model(rows, land)
    near = m.estimate('Tarla', 3000, 'Cumalı Mh.', land={'coast_m': 400, 'elevation_m': 120})
    far = m.estimate('Tarla', 3000, 'Cumalı Mh.', land={'coast_m': 4000, 'elevation_m': 120})
    assert near['fair'] > far['fair'] * 1.2 and {d['key'] for d in near['drivers']} >= {'size', 'hood', 'coast', 'elevation'}
    unmeasured = m.estimate('Tarla', 3000, 'Cumalı Mh.')
    assert far['fair'] < unmeasured['fair'] < near['fair'] and any('Arazi ölçümü' in w for w in unmeasured['warnings']) and unmeasured['confidence'] == 'medium'


def test_neighbourhood_names_are_one_place_however_the_page_writes_them():
    assert {comparables.neighborhood(x) for x in ('Mesudiye Mh.', 'Köyler\nMesudiye Mh.', 'KöylerMesudiye Mh.', 'Köyler / Mesudiye Mahallesi')} == {'mesudiye'}


# ---- the land's quality, and how it weighs a low price ------------------------------------------------------------------------------
GOOD = {'slope_pct': 5.0, 'faces': 'güney', 'coast_m': 800, 'elevation_m': 60}
POOR = {'slope_pct': 34.0, 'faces': 'kuzeybatı', 'coast_m': 3800, 'elevation_m': 300}


def test_quality_is_what_the_price_does_not_show_and_the_unmeasured_sits_in_the_middle():
    good = purpose_fit('Zeytinlik', 4000, 'var', 'kuyu ve elektrik var', True, land=GOOD)[1]
    poor = purpose_fit('Zeytinlik', 4000, 'var', '', True, land=POOR)[1]
    unknown = purpose_fit('Zeytinlik', 4000, 'var', '', True)[1]
    assert good['quality'] > unknown['quality'] > poor['quality'] and good['quality'] - poor['quality'] >= 15
    assert (good['slope'], good['aspect'], good['coast']) == (100, 100, 90) and (poor['slope'], poor['aspect'], poor['coast']) == (25, 45, 52)
    assert (unknown['slope'], unknown['aspect'], unknown['coast']) == (55, 55, 55) and unknown['measured'] is False and good['measured'] is True
    flat = purpose_fit('Tarla', 4000, 'var', '', True, land={'slope_pct': 2.0, 'faces': None, 'coast_m': 800})[1]
    assert flat['aspect'] == 85                                                 # flat land faces no way: nearly as good as south
    assert opportunity.price_credit(80) == 1.0 and opportunity.price_credit(60) == pytest.approx(0.5) and opportunity.price_credit(30) == opportunity.CREDIT_FLOOR


def test_a_low_price_is_an_opportunity_on_good_land_and_only_partly_on_poor_land():
    estimate = {'tier': 'model', 'method': value.METHOD, 'fair': 2000.0, 'low': 1250.0, 'high': 3200.0, 'scatter': 0.47, 'confidence': 'medium', 'n': 200, 'drivers': []}
    row = {'type': 'Zeytinlik', 'area_m2': 4000.0, 'price_tl': 4000 * 1000.0, 'route_minutes_max': 15.0, 'penalties': {}}        # half the expected price
    good = listing_view(row, estimate, 0.5, 'kuyu var', {'value': 'direct'}, True, land=GOOD)
    poor = listing_view(row, estimate, 0.5, '', {'value': 'corridor_conditional'}, True, land=POOR)
    assert good['label'] == 'Güçlü fırsat' and good['caps'] == [] and good['gap_pct'] == 50.0
    assert poor['label'] == 'Olası fırsat' and any('arazi kalitesi düşük' in c for c in poor['caps'])
    assert good['opportunity'] > poor['opportunity'] > 50 and good['score'] - poor['score'] >= 12
    assert good['parts']['quality'] > 75 > 62 > poor['parts']['quality']
    dear = {**row, 'price_tl': 4000 * 3000.0}                                   # dear stays dear whatever the land
    assert listing_view(dear, estimate, 0.5, '', {'value': 'direct'}, True, land=GOOD)['opportunity'] == listing_view(dear, estimate, 0.5, '', {'value': 'direct'}, True, land=POOR)['opportunity'] < 50
    thin = listing_view(row, {**estimate, 'confidence': 'low'}, 0.5, 'kuyu var', {'value': 'direct'}, True, land=GOOD)
    assert thin['label'] == 'Olası fırsat' and thin['opportunity'] < good['opportunity'] and any('az veriye' in c for c in thin['caps'])
    assert good['basis']['method'] == value.METHOD and good['basis']['size_adjusted'] is True


def test_the_ranking_rows_carry_the_land_and_the_pair_that_started_this_comes_out_in_order():
    # Two listings as the records had them on 12 Nov 2026 (rounded): the larger, gentler, south-facing parcel with a road and a well, cheaper in all;
    # and the smaller, steep, north-west-facing one with an unconfirmed road, cheaper against its own dear neighbourhood. The first must rank clearly higher.
    yaka = {'type': 'Zeytinlik', 'area_m2': 7706.0, 'price_tl': 6_500_000.0, 'route_minutes_max': 14.0, 'penalties': {}}
    mesudiye = {'type': 'Zeytinlik', 'area_m2': 3758.0, 'price_tl': 6_900_000.0, 'route_minutes_max': 28.8, 'penalties': {}}
    est = lambda fair: {'tier': 'model', 'method': value.METHOD, 'fair': fair, 'low': fair * 0.62, 'high': fair * 1.6, 'scatter': 0.47, 'confidence': 'medium', 'n': 261, 'drivers': []}
    first = listing_view(yaka, est(1206.0), 0.47, 'su kuyusu mevcut', {'value': 'direct'}, True, land={'slope_pct': 13.8, 'faces': 'güney', 'coast_m': 1900})
    second = listing_view(mesudiye, est(3441.0), 0.47, '', {'value': 'corridor_conditional'}, True, land={'slope_pct': 34.2, 'faces': 'kuzeybatı', 'coast_m': 1705})
    assert second['gap_pct'] > first['gap_pct']                                 # against its own neighbourhood the second is the cheaper one
    assert first['score'] >= second['score'] + 8 and first['parts']['quality'] >= second['parts']['quality'] + 15
