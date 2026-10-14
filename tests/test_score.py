from verda.agency.score import benchmark, market_type, peers_from, proximity, purchase_score, trim_outliers


def _rows(n, price_per_m2, neighborhood='Yaka', kind='Zeytinlik', area=2000.0, start=0):
    return [{'id': str(start + i), 'price': price_per_m2 * area, 'area': area, 'type': kind, 'neighborhood': neighborhood, 'parcel': f'{i + 1}/1'} for i in range(n)]


def test_market_type_and_proximity_follow_the_old_model():
    assert market_type('Zeytinlik', None) == 'Zeytinlik' and market_type(None, 'Badem bahçesi') == 'Bademlik'
    assert market_type('Tarla', None) == 'Tarla' and market_type('imarlı arsa', None) == 'Arsa / imarlı' and market_type(None, None) == 'Diğer / belirsiz'
    assert proximity(10) == 100 and proximity(45) == 0 and proximity(20) == 71 and proximity(60) == 0 and proximity(None) == 20


def test_a_local_benchmark_gives_value_through_the_discount_and_the_score_follows_the_weights():
    peers = peers_from(_rows(8, 1000.0))                         # eight parcels at 1000 TL/m²
    subject = {'id': 'S', 'price': 1800.0 * 1000.0 / 1000.0 * 1000.0, 'area': 2000.0, 'type': 'Zeytinlik', 'neighborhood': 'Yaka'}
    subject['price'] = 1_800_000.0                               # 900 TL/m²: 10 % under the 1000 benchmark
    v = benchmark(subject, peers)
    assert v['discount_pct'] == 10.0 and v['value_score'] == 70 and v['scope'] == 'Yaka · Zeytinlik' and v['confidence'] == 'Yüksek'
    out = purchase_score(subject, peers, 20, {})
    assert out['value_score'] == 70 and out['proximity_score'] == 71 and out['base_score'] == round(0.7 * 70 + 0.3 * 71) == 70 and out['score'] == 70
    # penalties come off the base exactly: natural sit -30 and an existing right of way -10
    assert purchase_score(subject, peers, 20, {'natural_sit_second_degree': 30, 'access_existing_easement': 10})['score'] == 30


def test_without_enough_peers_the_value_is_30_and_the_score_is_capped_at_49():
    subject = {'id': 'S', 'price': 1_000_000.0, 'area': 2000.0, 'type': 'Zeytinlik', 'neighborhood': 'Yaka'}
    out = purchase_score(subject, peers_from(_rows(3, 1000.0)), 10, {})
    assert out['value'] is None and out['valuation'] == 'no_benchmark_capped_49' and out['base_score'] == 49
    assert purchase_score({**subject, 'type': 'Diğer / belirsiz'}, peers_from(_rows(20, 1000.0)), 10, {})['base_score'] == 49


def test_peer_rules_same_type_area_ratio_other_neighborhoods_and_the_subject_itself():
    subject = {'id': 'S', 'price': 2_000_000.0, 'area': 2000.0, 'type': 'Zeytinlik', 'neighborhood': 'Yaka'}
    other_area = peers_from(_rows(10, 1000.0, area=5000.0))                  # ratio 2.5: out of range
    other_type = peers_from(_rows(10, 1000.0, kind='Tarla'))
    assert benchmark(subject, other_area) is None and benchmark(subject, other_type) is None
    # eight peers elsewhere in Datça: a general, low-confidence benchmark; fewer than five local ones do not count as local
    general = peers_from(_rows(8, 1000.0, neighborhood='Cumalı'))
    v = benchmark(subject, general)
    assert v['scope'] == 'Datça geneli · Zeytinlik' and v['confidence'] == 'Düşük'
    # the subject never benchmarks against itself
    itself = peers_from([{**r, 'id': 'S'} if i == 0 else r for i, r in enumerate(_rows(8, 1000.0, neighborhood='Cumalı'))])
    assert benchmark(subject, itself) is None or benchmark(subject, itself)['comparables'] <= 7


def test_a_parcel_listed_twice_counts_once_and_outliers_are_trimmed():
    twice = _rows(1, 1000.0) + [{**_rows(1, 1200.0)[0], 'id': 'dup'}]
    assert len(peers_from(twice)) == 1
    peers = [{'rate': r} for r in (1000, 1010, 990, 1005, 995, 1000, 5000)]
    assert 5000 not in [p['rate'] for p in trim_outliers(peers)]


def test_ranking_scores_listings_that_are_not_excluded_and_marks_unsettled_ones_provisional(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    from verda.agency.score import ranking
    import sqlite3, uuid
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    con = sqlite3.connect(store.path)
    with con:
        for listing in ('1111111111', '2222222222', '3333333333'):
            con.execute("INSERT INTO assessments(id,listing_ref,mode,policy_version,input_fingerprint,facts,basis,conflicts,state,reasons,conditions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, listing, 'local', 'p', uuid.uuid4().hex, '{}', '{}', '[]', 'x', '[]', '[]', 1.0))
        con.execute("INSERT INTO observations(id,call_id,task_id,agent,trace_id,listing_ref,mode,tool,kind,captured_at,arguments,payload,payload_hash,fields) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('o1', 'c1', 't1', 'a', 'tr', '1111111111', 'local', 'sahibinden.read_listing', 'source', 1.0, '{}', '{"title": "PALAMUTBÜKÜ ARSA"}', 'h', '[]'))
    con.close()
    def facts(price, state, penalties=None, route=(20.0,)):
        return {'facts': {'price_tl': {'value': price}, 'sale_area_m2': {'value': 2000.0}, 'property_type': {'value': 'Tarla'},
                          'route_minutes': [{'value': m} for m in route]},
                'decision': {'state': state, 'reasons': ['route'] if state == 'research_required' else [], 'conditions': [], 'penalties': penalties or {}}}
    plan = {'1111111111': facts(2_000_000.0, 'eligible'), '2222222222': facts(1_000_000.0, 'research_required', route=()),
            '3333333333': facts(500_000.0, 'excluded')}
    out = ranking(store, {'limit': 10}, mode='local', status=lambda l: plan[l])
    ids = [r['listing_id'] for r in out['ranked']]
    assert ids == ['1111111111', '2222222222'] and '3333333333' not in ids        # settled first, excluded never ranked
    assert out['ranked'][0]['provisional'] is False and out['ranked'][1]['provisional'] is True and out['settled'] == 1
    assert out['ranked'][1]['valuation'] == 'no_benchmark_capped_49' and out['ranked'][1]['score'] <= 49
    assert out['ranked'][0]['title'] == 'PALAMUTBÜKÜ ARSA' and out['ranked'][1]['title'] is None     # the list shows the title of the last read
