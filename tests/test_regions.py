"""Searches outside Datça (agency/regions.py): their own pages and filters, and never a trace in the Datça flow."""
import pytest

from verda.agency import regions
from verda.agency.browser import BrowserBridge, RegionSearchReceipt, SearchReceipt
from verda.agency.engine import AgentEngine
from verda.agency.jev import run_worker, verify_receipt
from test_management import setup

MILAS = 'https://www.sahibinden.com/satilik-arsa/mugla-milas?price_max=9000000&a507_min=20000&sorting=date_desc'
CARD = {'listing_id': '9000000077', 'title': 'Synthetic olive grove', 'url': 'https://www.sahibinden.com/ilan/test-9000000077/detay',
        'price_tl': 4_000_000.0, 'area_m2': 30_000.0}


def page_receipt(url=MILAS, cards=(CARD,)):
    return {'source_url': url, 'total_reported': len(cards), 'page': 1, 'has_more': False,
            'filters': {'max_price_tl': 9_000_000, 'min_area_m2': 20_000}, 'listings': [dict(c) for c in cards]}


def test_a_region_search_opens_only_its_own_town_pages_with_its_own_filters():
    assert regions.search_url({'region': 'latmos', 'town': 'milas', 'page': 1, 'sort': 'newest'}) == MILAS
    assert 'pagingOffset=40' in regions.search_url({'region': 'latmos', 'town': 'cine', 'page': 3})
    for bad in ({'region': 'latmos', 'town': 'datca'}, {'region': 'nowhere', 'town': 'milas'}):
        with pytest.raises(ValueError):
            regions.search_url(bad)


def test_the_datca_receipt_still_accepts_only_datca_and_a_region_receipt_only_region_pages():
    RegionSearchReceipt.model_validate(page_receipt())
    datca = 'https://www.sahibinden.com/satilik-arsa/mugla-datca?price_max=9000000&a507_min=20000'
    with pytest.raises(ValueError):
        RegionSearchReceipt.model_validate(page_receipt(datca))
    with pytest.raises(ValueError):
        SearchReceipt.model_validate(page_receipt())                                       # the Datça flow never takes a Milas page


def test_the_reader_checks_the_town_page_and_the_filters():
    job = {'tool': 'region.search', 'arguments': {'region': 'latmos', 'town': 'milas', 'page': 1, 'sort': 'newest'}}
    raw = {'url': MILAS, 'blocker': None, 'listings': [dict(CARD)], 'total_reported': 1, 'detail': None}
    assert verify_receipt(job, raw)['listings'][0]['listing_id'] == CARD['listing_id']
    for url in (MILAS.replace('mugla-milas', 'aydin-cine'), MILAS.replace('a507_min=20000', 'a507_min=1000')):
        with pytest.raises(ValueError):
            verify_receipt(job, {**raw, 'url': url})
    small = {**CARD, 'area_m2': 5000.0}
    with pytest.raises(ValueError):
        verify_receipt(job, {**raw, 'listings': [small]})


def test_a_region_read_goes_through_the_shared_browser_but_never_reaches_the_datca_flow(tmp_path):
    store, registry = setup(tmp_path)
    bridge = BrowserBridge(store); bridge.select('jev')
    engine = AgentEngine(store, registry, providers={})
    opened = []

    class Reader:
        def __init__(self, answer):
            self.answer = answer
        def ready(self):
            return None
        def read(self, url, job, report):
            opened.append(url)
            report('observed'); report('DONE'); report('verified')
            return self.answer

    def run(key, tool, inputs, answer):
        with store.transaction() as con:
            con.execute("UPDATE resources SET next_at=0")       # the pace between reads is tested elsewhere
        task = store.submit(agent='region', objective='Bölge okuması', inputs=inputs, request_key=key, executor_tool=tool)
        assert engine.step()
        assert next(t for t in store.overview()['tasks'] if t['id'] == task)['state'] == 'waiting_browser'
        assert run_worker(store, reader=Reader(answer))['completed'] == 1
        assert engine.step()
        assert next(t for t in store.overview()['tasks'] if t['id'] == task)['state'] == 'complete'

    run('p1', 'region.search', {'region': 'latmos', 'town': 'milas', 'page': 1, 'sort': 'newest'}, page_receipt())
    detail = {**CARD, 'neighborhood': 'Synthetic Mh.', 'description': 'zeytinlik', 'attributes': {'ada no': '1', 'parsel no': '2'}}
    run('d1', 'region.read_listing', {'region': 'latmos', 'ilan': CARD['listing_id']}, detail)
    assert opened[0].startswith('https://www.sahibinden.com/satilik-arsa/mugla-milas?') and opened[1] == CARD['url']   # only the card's own address

    with store.transaction() as con:
        rows = con.execute("SELECT tool, listing_ref FROM observations WHERE tool LIKE 'region.%'").fetchall()
        assert {r['tool'] for r in rows} == {'region.search', 'region.read_listing'} and all(r['listing_ref'] is None for r in rows)
        assert con.execute("SELECT count(*) FROM observations WHERE listing_ref=?", (CARD['listing_id'],)).fetchone()[0] == 0
    from verda.agency.discovery import baseline
    from verda.agency.listing_dates import collect
    assert CARD['listing_id'] not in baseline(store)
    assert CARD['listing_id'] not in collect(store)


def test_a_detail_is_read_only_for_a_listing_a_region_search_showed(tmp_path):
    from verda.agency.jev import JevStopped, target_url
    store, _ = setup(tmp_path)
    with pytest.raises(JevStopped):
        target_url(store, {'tool': 'region.read_listing', 'arguments': {'region': 'latmos', 'ilan': '9000000078'}})


def test_cards_are_remembered_with_their_price_history_and_new_or_changed_ones_are_read(tmp_path):
    db = regions.region_db(tmp_path / 'r.sqlite')
    assert regions.remember_cards(db, 'latmos', 'milas', [CARD], now=1.0) == [CARD['listing_id']]
    db.execute("UPDATE listings SET detail_at=2 WHERE listing_id=?", (CARD['listing_id'],))
    assert regions.remember_cards(db, 'latmos', 'milas', [CARD], now=3.0) == []                    # read, nothing changed
    cheaper = {**CARD, 'price_tl': 3_500_000.0}
    assert regions.remember_cards(db, 'latmos', 'milas', [cheaper], now=4.0) == [CARD['listing_id']]
    assert [r['price_tl'] for r in db.execute('SELECT price_tl FROM prices ORDER BY at')] == [4_000_000.0, 3_500_000.0]


def test_a_scan_stops_at_the_first_job_that_does_not_complete(tmp_path, monkeypatch):
    submitted = []
    fake = type('Store', (), {'submit': lambda self, **kwargs: submitted.append(kwargs)})()
    monkeypatch.setattr(regions, '_state', lambda store, key: {'id': 'x', 'state': 'blocked', 'reason': 'unusual_access'})
    result = regions.scan(fake, 'latmos', db=regions.region_db(tmp_path / 'r.sqlite'), sleep=lambda s: None)
    assert result['stopped'] == 'milas p1: unusual_access' and len(submitted) == 1          # nothing is tried around a stop
