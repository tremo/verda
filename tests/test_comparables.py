import math
import random
import statistics

import pytest

from verda.agency.comparables import Estimator, MIN_LOCAL
from verda.agency.opportunity import LAND, _solve, hood_of, label, listing_view


def peer(i, hood, area, rate, kind='Tarla', road='ok'):
    return {'ids': {str(i)}, 'type': kind, 'area': area, 'rate': rate, 'neighborhood': hood + ' Mh.', 'road': road}


def market():
    """Asking prices vary with size, but the estimator must not impose that trend on a subject."""
    random.seed(3)
    out, n = [], 0
    for hood, level in (('Mesudiye', 3000), ('Sındı', 1500), ('Yazı', 1400)):
        for area in (2000, 2500, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 12000):
            n += 1
            out.append(peer(n, hood, area, level * (area / 5000) ** -0.55 * math.exp(random.gauss(0, 0.05))))
    return out


def test_enough_local_comparables_give_the_median_with_their_links():
    peers = market()
    estimate = Estimator(peers, hood_of, LAND, _solve).estimate('Tarla', 6000, 'Sındı Mh.', '999')
    assert estimate['tier'] == 'local' and estimate['n'] >= MIN_LOCAL
    rates = [p['rate'] for p in peers if p['neighborhood'] == 'Sındı Mh.' and 3000 <= p['area'] <= 12000]
    assert estimate['fair'] == statistics.median(rates)
    assert estimate['low'] <= estimate['fair'] <= estimate['high']
    assert all(c['ids'] for c in estimate['comps'])
    assert all(c['adjusted'] == c['rate'] for c in estimate['comps'])


def test_same_local_comparables_do_not_get_a_size_discount_or_premium():
    peers = [peer(i, 'Sındı', area, rate) for i, (area, rate) in enumerate(
        [(3000, 1000), (3500, 2000), (4000, 3000), (5000, 4000)])]
    estimator = Estimator(peers, hood_of, LAND, _solve)
    for area in (4000, 5000):
        estimate = estimator.estimate('Tarla', area, 'Sındı Mh.', '999')
        assert estimate['tier'] == 'local' and estimate['fair'] == 2500
        row = {'type': 'Tarla', 'area_m2': area, 'price_tl': area * 1500, 'route_minutes_max': 15, 'penalties': {}}
        view = listing_view(row, estimate, 0.5, '', None, True)
        assert view['gap_pct'] == 40 and view['basis']['size_adjusted'] is False


def test_a_parcel_without_a_known_road_is_never_a_local_comparable_and_a_bad_one_is_in_no_step():
    peers = market()
    for p in peers:
        if p['neighborhood'].startswith('Sındı'):
            p['road'] = None
    est = Estimator(peers, hood_of, LAND, _solve).estimate('Tarla', 6000, 'Sındı Mh.', '999')
    assert est['tier'] == 'region' and est['n'] == 0                                  # same neighbourhood, but nothing with a road: the regional step
    for p in peers:
        if p['neighborhood'].startswith('Sındı'):
            p['road'] = 'bad'
    assert Estimator(peers, hood_of, LAND, _solve).estimate('Tarla', 6000, 'Sındı Mh.', '999')['tier'] == 'none'    # too few rows left to know the neighbourhood


def test_the_regional_step_carries_the_neighbourhood_level_and_a_wider_range():
    peers = market()
    for p in peers:
        if p['neighborhood'].startswith('Sındı'):
            p['road'] = None
    estimator = Estimator(peers, hood_of, LAND, _solve)
    est = estimator.estimate('Tarla', 6000, 'Sındı Mh.', '999')
    expected = math.exp(statistics.mean(math.log(p['rate']) for p in peers if p['neighborhood'] == 'Sındı Mh.'))
    assert est['fair'] == pytest.approx(expected, rel=1e-5)
    assert est['effect_pct'] < -40 and est['reference'] in ('Mesudiye', 'Sındı', 'Yazı')
    assert est['high'] / est['low'] > 1.2
    assert estimator.estimate('Tarla', 3000, 'Sındı Mh.', '999')['fair'] == est['fair']
    assert estimator.estimate('Tarla', 9000, 'Sındı Mh.', '999')['fair'] == est['fair']


def test_regional_fit_excludes_the_subjects_own_price_and_all_duplicate_listing_ids():
    peers = market()
    for p in peers:
        p['road'] = None
    mine = peers[12]
    mine['ids'].add('duplicate')
    mine['rate'] *= 10
    rates = [p['rate'] for p in peers if p['neighborhood'] == mine['neighborhood'] and p is not mine]
    expected = math.exp(statistics.mean(math.log(rate) for rate in rates))
    estimator = Estimator(peers, hood_of, LAND, _solve)
    for listing in mine['ids']:
        estimate = estimator.estimate(mine['type'], mine['area'], mine['neighborhood'], listing)
        assert estimate['tier'] == 'region' and estimate['fair'] == pytest.approx(expected, rel=1e-5)


def test_regional_type_and_neighbourhood_effects_survive_removing_area():
    peers = []
    for hood, level in (('Mesudiye', 2000), ('Sındı', 1000)):
        for kind, factor in (('Tarla', 1.0), ('Zeytinlik', 1.1), ('Bademlik', 1.2)):
            for area in (2000, 3000, 4000, 5000, 6000):
                peers.append(peer(len(peers), hood, area, level * factor, kind=kind, road=None))
    estimator = Estimator(peers, hood_of, LAND, _solve)
    for hood, level in (('Mesudiye', 2000), ('Sındı', 1000)):
        for kind, factor in (('Tarla', 1.0), ('Zeytinlik', 1.1), ('Bademlik', 1.2)):
            for area in (2500, 5500):
                estimate = estimator.estimate(kind, area, hood, 'new')
                assert estimate['tier'] == 'region' and estimate['fair'] == pytest.approx(level * factor, rel=1e-5)


def test_no_number_is_made_up_for_an_unknown_neighbourhood_or_a_size_far_outside_the_data():
    estimator = Estimator(market(), hood_of, LAND, _solve)
    assert estimator.estimate('Tarla', 6000, 'Bilinmeyen Mh.', '999')['fair'] is None
    assert estimator.estimate('Tarla', 90000, 'Sındı Mh.', '999')['fair'] is None      # local needs half to twice the size; the fit stops at twice the largest
    assert estimator.estimate('Tarla', None, 'Sındı Mh.', '999')['fair'] is None


def test_the_listing_is_not_its_own_comparable():
    peers = market()
    mine = peers[12]
    est = Estimator(peers, hood_of, LAND, _solve).estimate('Tarla', mine['area'], mine['neighborhood'], next(iter(mine['ids'])))
    assert all(next(iter(mine['ids'])) not in c['ids'] for c in est['comps'])


def test_labels_name_the_real_gap():
    assert label(-0.3, 0.47) == 'Adilin üstünde' and label(-0.05, 0.47) == 'Adil fiyat' and label(-0.5, 0.47) == 'Pahalı'


def test_a_regional_estimate_never_makes_a_strong_opportunity():
    row = {'type': 'Tarla', 'area_m2': 5000, 'price_tl': 2_500_000, 'neighborhood': 'Sındı Mh.', 'route_minutes_max': 15, 'penalties': {}}
    region = {'tier': 'region', 'fair': 1500.0, 'low': 900.0, 'high': 2400.0, 'n': 0, 'comps': []}
    local = {'tier': 'local', 'fair': 1500.0, 'low': 1400.0, 'high': 1600.0, 'n': 4, 'comps': []}
    assert listing_view(row, region, 0.47, '', None, True)['label'] == 'Adil fiyat'     # 500 vs 1500 per m2 would read "strong" from four neighbours
    assert listing_view(row, local, 0.47, '', None, True)['label'] == 'Güçlü fırsat'
    assert listing_view(row, {'tier': 'none', 'fair': None}, 0.47, '', None, True)['label'] == 'Belirsiz'
