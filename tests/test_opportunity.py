

def test_house_potential_follows_the_bag_evi_thresholds_not_building_claims():
    from verda.agency.opportunity import house_potential, tree_density
    assert house_potential('Tarla', 5738, 'imar izni alınabilir') == 0            # under 10 dönüm: no route, a claim in the text changes nothing
    assert house_potential('Tarla', 20000) == 80 and house_potential('Zeytinlik', 20000) == 40      # zeytinlik: 3573 applies, half
    assert house_potential('Bademlik', 11000) == 25                                # dikili would do from 10 dönüm, tree count unknown
    assert house_potential('Bademlik', 11000, '400 adet badem ağacı') == 50        # 400 / (27 x 11) is above the minimum density
    assert house_potential('Bademlik', 11000, '100 adet badem ağacı') == 0         # well under it: not dikili
    assert house_potential('Zeytinlik', 11000, '400 zeytin') == 25


def test_tree_density_reads_counts_and_ignores_text_without_them():
    from verda.agency.opportunity import tree_density
    assert round(tree_density('Taşınmaz içinde 70 adet zeytin ağacı, 10 adet badem ağacı vardır', 5738), 2) == 0.5
    assert tree_density('zeytinlik, manzaralı', 5000) is None and tree_density('70 zeytin', 0) is None


def _ranked_row(listing, old_score, new_score, **new):
    view = {'score': new_score, 'opportunity': 81, 'purpose': 61, 'proximity': 74, 'label': 'Güçlü fırsat', 'road': 'var', 'fair_unit_price': 3265, 'gap_pct': 40.8, 'caps': [],
            'parts': {'investment': 76, 'house': 0, 'production': 76, 'building_hint': False, 'water_hint': False, 'power_hint': False},
            'basis': {'tier': 'local', 'n': 6, 'note': 'Cumalı mahallesinde, yolu bilinen 6 benzer parselin fiyatlarının ortancası'}, **new}
    return {'listing_id': listing, 'score': old_score, 'provisional': False, 'state': 'eligible', 'price_tl': 2_650_000.0, 'area_m2': 1370.0, 'new': view}


def test_every_screen_and_agent_reads_the_same_opportunity_score(monkeypatch):
    from verda.agency import opportunity
    rows = [_ranked_row('1000000001', 97, 72), _ranked_row('1000000002', 40, 55, label='Adil fiyat', gap_pct=3.0, caps=['bütçe'], basis={'tier': 'region', 'n': 1, 'note': 'bölge'}),
            {**_ranked_row('1000000003', 30, 41, label='Belirsiz', gap_pct=None, fair_unit_price=None, opportunity=None, basis=None), 'provisional': True}]
    monkeypatch.setattr(opportunity, 'rank_all', lambda store, mode='local': {'ranked': rows, 'scatter': 0.5})
    out = opportunity.one_ranking(None, {'limit': 2})
    assert [r['listing_id'] for r in out['ranked']] == ['1000000001', '1000000002'] and out['total'] == 3 and out['settled'] == 2
    first, second = out['ranked']
    assert first['score'] == 72 and first['purchase_score'] == 97 and 'new' not in first               # the older number is kept, not shown as "the score"
    assert first['label'] == 'Güçlü fırsat' and first['gap_pct'] == 40.8 and first['fair_tier'] == 'local' and first['fair_n'] == 6
    assert first['parts']['opportunity'] == 81 and first['parts']['purpose'] == 61 and first['parts']['proximity'] == 74 and first['parts']['house'] == 0
    assert second['fair_tier'] == 'region' and second['caps'] == ['bütçe']
    assert opportunity.one_ranking(None, {'limit': 10})['ranked'][2]['fair_tier'] is None             # a row without a fair price has no basis to name


def test_the_panels_ranking_is_computed_once_a_minute_and_the_publisher_ranks_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from verda.agency import opportunity
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    from verda.api import create_app
    from verda.publish import projection
    from verda.storage import connect, initialize
    calls = {'n': 0}

    def rank_all(store, mode='local', now=None):
        calls['n'] += 1
        return {'ranked': [_ranked_row('1000000001', 97, 72)], 'scatter': 0.5}
    monkeypatch.setattr(opportunity, 'rank_all', rank_all)
    store = AgencyStore(tmp_path / 'agency.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    engine = connect(f"sqlite:///{tmp_path / 'v2.sqlite'}")
    initialize(engine)
    with TestClient(create_app(engine, 't' * 32, viewer_nonce='one-use', agency_store=store), base_url='https://testserver') as client:
        assert client.get('/control/unlock/one-use', follow_redirects=False).status_code == 303
        first = client.get('/control/private/ranking?limit=200')
        assert first.status_code == 200 and first.json()['ranked'][0]['score'] == 72 and first.json()['ranked'][0]['label'] == 'Güçlü fırsat'
        assert client.get('/control/private/ranking?limit=5').json()['total'] == 1 and calls['n'] == 1          # the list and each listing page share one calculation
        client.get('/control/private/ranking?limit=5&fresh=true')
        assert calls['n'] == 2
    engine.dispose()
    # the publisher builds its listing rows from the same pass: the older ranking is not computed a second time
    monkeypatch.setattr(projection, 'ranking', lambda *a, **k: 1 / 0)
    monkeypatch.setattr(projection, 'listing_document', lambda store, row, now_iso, mode='local', new=None: {'state': row['state'], 'score': row['score'], 'opp': new and new['score']})
    monkeypatch.setattr(projection, 'closed_documents', lambda *a, **k: [])
    before = calls['n']
    docs = projection.build(store, now=1000.0)
    listing = next(d for c, key, d in docs if c == projection.COLLECTION)
    assert calls['n'] == before + 1 and listing == {'state': 'eligible', 'score': 97, 'opp': 72}
    meta = next(d for c, key, d in docs if (c, key) == ('meta', 'v2'))
    assert meta['total'] == 1 and meta['settled'] == 1
