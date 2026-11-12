"""Price moves, the seller and the owner's marks; and the day's few decisions built from them. All data is synthetic."""
import json

from verda.agency import decisions, opportunity, signals

DAY = 86400.0
NOW = 100 * DAY


def test_a_price_history_keeps_each_change_once_and_the_trend_looks_at_the_last_weeks():
    seen = [(10 * DAY, 8.4e6), (11 * DAY, 8.4e6), (80 * DAY, 7.45e6), (81 * DAY, 7.45e6), (90 * DAY, 6.75e6), (98 * DAY, 6.5e6), (99 * DAY, 6.5e6)]
    history = signals.steps(seen)
    assert history == [(10 * DAY, 8.4e6), (80 * DAY, 7.45e6), (90 * DAY, 6.75e6), (98 * DAY, 6.5e6)]
    moved = signals.trend(history, NOW)
    assert moved['from'] == 8.4e6 and moved['to'] == 6.5e6 and moved['change_pct'] == -22.6 and moved['drops'] == 3 and moved['rises'] == 0
    assert moved['last_move'] == 'down' and moved['last_change_at'] == 98 * DAY and moved['since_days'] == 45           # the price held when the window opened
    assert signals.describe(moved) == '45 günde %22,6 indi (3 indirim)' and signals.bonus(moved) == 5
    assert signals.trend([(10 * DAY, 8.4e6)], NOW) is None and signals.trend([], NOW) is None
    assert signals.trend([(10 * DAY, 9e6), (20 * DAY, 8e6)], NOW) is None                                              # a cut older than the window is old news


def test_only_a_price_that_keeps_coming_down_earns_points_and_never_more_than_a_few():
    cut = lambda *prices: signals.trend(signals.steps([(90 * DAY + i * DAY, p) for i, p in enumerate(prices)]), NOW)
    assert signals.bonus(cut(100, 97)) == 0                                    # 3 %: rounding, not a signal
    assert signals.bonus(cut(100, 90)) == 2 and signals.bonus(cut(100, 95, 90)) == 3          # two cuts say more than one
    assert signals.bonus(cut(100, 40, 30, 20)) == signals.MAX_BONUS
    assert signals.bonus(cut(100, 80, 90)) == 0 and signals.describe(cut(100, 80, 90)) is not None      # back up: told, not rewarded
    assert signals.bonus(None) == 0 and signals.describe(None) is None
    assert signals.describe(cut(100, 120)).endswith('%20 arttı')


def _row(listing, score=50, **extra):
    return {'listing_id': listing, 'type': 'Zeytinlik', 'area_m2': 5000.0, 'price_tl': 5e6, 'neighborhood': 'Yaka Mh.', 'route_minutes_max': 15.0, 'penalties': {}, **extra,
            'new': {'score': score, 'label': 'Adil fiyat', 'gap_pct': 3.0, 'road': 'var', 'caps': [], 'signals': [], 'fair_unit_price': 1000, **(extra.get('new') or {})}}


def test_the_falling_price_shows_in_the_score_as_its_own_line_and_stops_at_the_caps():
    moved = signals.trend(signals.steps([(90 * DAY, 10e6), (95 * DAY, 9e6), (98 * DAY, 8e6)]), NOW)
    row = {'type': 'Zeytinlik', 'area_m2': 5000.0, 'price_tl': 8e6, 'route_minutes_max': 15.0, 'penalties': {}}
    estimate = {'tier': 'local', 'fair': 1600.0, 'low': 1400.0, 'high': 1900.0, 'n': 5, 'comps': [], 'note': 'x'}
    plain = opportunity.listing_view(row, estimate, 0.5, '', {'value': 'direct'}, True)
    with_cut = opportunity.listing_view(row, estimate, 0.5, '', {'value': 'direct'}, True, moved=moved)
    assert with_cut['score'] == plain['score'] + signals.bonus(moved) == plain['score'] + 5
    assert with_cut['signals'] == [{'kind': 'price_drop', 'text': 'fiyat 10 günde %20 indi (2 indirim)', 'points': 5}] and plain['signals'] == []
    dear = {**row, 'price_tl': 10e6, 'area_m2': 8000.0}                        # above the ideal budget, 22 % under the fair price where 30 % is asked: capped at 60, cut or no cut
    assert opportunity.listing_view(dear, estimate, 0.5, '', {'value': 'direct'}, True)['score'] == 60
    assert opportunity.listing_view(dear, estimate, 0.5, '', {'value': 'direct'}, True, moved=moved)['score'] == 60
    cheap = {**row, 'price_tl': 1e6}                                           # too good to believe: the cut earns nothing more
    doubted = opportunity.listing_view(cheap, estimate, 0.5, '', {'value': 'direct'}, True, moved=moved)
    assert doubted['signals'][0]['points'] == 0 and any('çok ucuz' in c for c in doubted['caps'])
    unknown = opportunity.listing_view(row, None, 0.5, '', {'value': 'direct'}, True, moved=moved)      # no fair price: still never above 49
    assert unknown['score'] <= 49 and unknown['signals'][0]['points'] == 5


def test_the_seller_is_read_from_the_page_an_office_by_name_an_owner_without_one():
    office = {'attributes': {'kimden': 'Emlak Ofisinden'}, 'page_text': 'İlan ile İlgili Şikayetim Var\n12.\nYIL\nÖRNEK EMLAK DATÇA\nHesap Açma Tarihi: 2014\nDeniz Kaan\nTüm ilanları'}
    assert signals.seller_of(office) == {'kind': 'agency', 'store': 'ÖRNEK EMLAK DATÇA'}
    assert signals.seller_of({'attributes': {'kimden': 'Sahibinden'}, 'page_text': 'Hesap açma tarihi: Ekim 2019\nCep\nMesaj gönder'}) == {'kind': 'owner', 'store': None}
    assert signals.seller_of({'attributes': {'kimden': 'Emlak Ofisinden'}, 'page_text': 'YIL\nbaşka bir şey'}) == {'kind': 'agency', 'store': None}      # no office card: not guessed
    assert signals.seller_of({'attributes': {}}) is None and signals.seller_of(None) is None


def test_the_owners_newest_mark_per_listing_is_what_counts(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    assert signals.user_marks(store) == {}
    marks = ['1000000001:decision=shortlist@2026-09-26T21:19:26.413Z', '1000000002:decision=maybe@2026-10-01T10:00:00.000Z', '1000000002:decision=excluded@2026-10-05T10:00:00.000Z',
             '1000000003:decision=excluded@2026-10-02T10:00:00.000Z', '1000000003:decision=removed@2026-10-13T19:59:09.600Z', '1000000004:road=cadastral@2026-10-01T10:00:00.000Z']
    with store.transaction() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        con.execute("INSERT INTO runtime_settings VALUES('feedback:imported', ?, 1.0)", (json.dumps(marks),))
    assert signals.user_marks(store) == {'1000000001': 'shortlist', '1000000002': 'excluded'}


def test_the_days_decisions_are_few_ordered_and_each_names_its_listing_with_both_links(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    cut = signals.trend(signals.steps([(90 * DAY, 8.4e6), (98 * DAY, 6.5e6)]), NOW)
    old_cut = signals.trend(signals.steps([(60 * DAY, 8.4e6), (70 * DAY, 6.5e6)]), NOW)
    links = lambda n: {'tkgm': f'https://parselsorgu.tkgm.gov.tr/#ara/idari/100/1/{n}', 'sahibinden': f'https://www.sahibinden.com/100000000{n}'}
    ranked = [
        _row('1000000001', 74, mark='shortlist', price_trend=cut, parcel='153/57', links=links(1)),
        _row('1000000002', 72, provisional=False, parcel='1/2', links=links(2)),
        _row('1000000003', 70, provisional=False, parcel='1/3', links=links(3)),
        _row('1000000004', 68, provisional=False, parcel='1/4', links=links(4)),                 # a third finished listing: only two of a kind are shown
        _row('1000000005', 64, price_trend=cut, parcel='1/5', links=links(5), new={'caps': ['çok ucuz görünüyor: fiyat ve alan doğrulanmalı']}),
        _row('1000000006', 40, provisional=True, price_trend=cut, parcel='1/6', links=links(6)),
        _row('1000000007', 90, mark='excluded', price_trend=cut, provisional=False, parcel='1/7', links=links(7)),      # the owner said no: never put forward again
        _row('1000000008', 80, mark='maybe', price_trend=old_cut, parcel='1/8', links=links(8)),  # a cut from a month ago is not today's news
    ]
    queue = {'skipped': {'waits_for_sender': 3}, 'skipped_ids': {'waits_for_sender': ['1000000006']}}
    assert decisions.today(store, now=NOW, ranked=ranked, queue=queue)['items'][-1]['kind'] != 'sender_off'      # sending is not switched off: nothing to say about it
    from verda.agency.tuning import apply
    apply(store, [{'key': 'sender.mode', 'value': 'off'}])
    out = decisions.today(store, now=NOW, ranked=ranked, queue=queue)
    assert [(i['kind'], i['listingId']) for i in out['items']] == [
        ('marked_price_drop', '1000000001'), ('verify_cheap', '1000000005'), ('ready', '1000000002'), ('ready', '1000000003'), ('price_drop', '1000000006'), ('sender_off', None)]
    first = out['items'][0]
    assert first['title'] == 'Kısa listendeki ilanın fiyatı düştü' and '8,4 M ₺ → 6,5 M ₺' in first['why'] and 'son değişiklik 2 gün önce' in first['why']
    assert first['parcel'] == '153/57' and first['links'] == {'tkgm': links(1)['tkgm'], 'listing': links(1)['sahibinden']}
    assert out['counts']['ready'] == 3 and out['counts']['waits_for_sender'] == 3 and 'İlk 30 puandaki 1 ilan' in out['items'][-1]['why']
    text = decisions.as_text(out)
    line = next(l for l in text.splitlines() if '#1000000001' in l)
    assert '153/57' in line and 'TKGM: ' + links(1)['tkgm'] in line and 'Sahibinden: ' + links(1)['sahibinden'] in line       # number, parcel and both links on one line
    unlooked = decisions.as_text({'items': [{**first, 'links': {'tkgm': None, 'listing': links(1)['sahibinden']}}]})
    assert 'TKGM: resmî sorgu henüz yapılmadı · Sahibinden: ' in unlooked                              # no official record yet: said, never left out or made up
    cheap = next(i for i in out['items'] if i['kind'] == 'verify_cheap')
    assert cheap['why'].startswith('1.000 ₺/m², beklenen ≈ 1.000 ₺/m²')
    assert decisions.as_text({'items': []}) == 'Bugün senden bekleyen bir karar yok.'
    assert decisions.money(6_500_000) == '6,5 M ₺' and decisions.money(950_000) == '950.000 ₺' and decisions.money(None) == '—'
