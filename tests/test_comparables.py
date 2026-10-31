import math
import random

from verda.agency.comparables import Estimator, MIN_LOCAL
from verda.agency.opportunity import LAND, _solve, hood_of, label, listing_view


def peer(i, hood, area, rate, kind='Tarla', road='ok'):
    return {'ids': {str(i)}, 'type': kind, 'area': area, 'rate': rate, 'neighborhood': hood + ' Mh.', 'road': road}


def market():
    """Mesudiye asks twice what Sındı does; every price falls 0.55 per doubling of the area."""
    random.seed(3)
    out, n = [], 0
    for hood, level in (('Mesudiye', 3000), ('Sındı', 1500), ('Yazı', 1400)):
        for area in (2000, 2500, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 12000):
            n += 1
            out.append(peer(n, hood, area, level * (area / 5000) ** -0.55 * math.exp(random.gauss(0, 0.05))))
    return out


def test_enough_local_comparables_give_the_median_with_their_links():
    estimate = Estimator(market(), hood_of, LAND, _solve).estimate('Tarla', 6000, 'Sındı Mh.', '999')
    assert estimate['tier'] == 'local' and estimate['n'] >= MIN_LOCAL
    assert abs(estimate['fair'] / (1500 * (6000 / 5000) ** -0.55) - 1) < 0.1          # brought to this size with the size slope
    assert estimate['low'] <= estimate['fair'] <= estimate['high']
    assert all(c['ids'] for c in estimate['comps'])


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
    assert abs(est['fair'] / (1500 * (6000 / 5000) ** -0.55) - 1) < 0.15
    assert est['effect_pct'] < -40 and est['reference'] in ('Mesudiye', 'Sındı', 'Yazı')
    assert est['high'] / est['low'] > 1.2


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
