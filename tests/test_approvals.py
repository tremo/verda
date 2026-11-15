"""The owner's word per listing on seller questions (Soru onayı) and what the score becomes once the seller answers. Synthetic data."""
import pytest

from verda.agency import approvals, message, opportunity
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore


@pytest.fixture
def store(tmp_path):
    s = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    s.initialize()
    return s


def test_the_latest_choice_decides_and_every_choice_is_kept(store):
    approvals.choose(store, '1000000001', 'hold', now=1.0)
    approvals.choose(store, '1000000001', 'ask', now=2.0)
    approvals.choose(store, '1000000002', 'ask', now=3.0)
    approvals.choose(store, '1000000002', 'clear', now=4.0)
    with store.transaction() as con:
        assert approvals.approved(con, '1000000001') and not approvals.approved(con, '1000000002') and not approvals.approved(con, '1000000003')
        assert {k: v['choice'] for k, v in approvals.choices(con).items()} == {'1000000001': 'ask'}
        assert con.execute('SELECT count(*) FROM ask_choices').fetchone()[0] == 4
    with pytest.raises(ValueError):
        approvals.choose(store, '1000000001', 'send_now')
    with pytest.raises(ValueError):
        approvals.choose(store, 'abc', 'ask')


def test_drop_is_the_owners_own_elimination_and_ends_any_approval(store):
    approvals.choose(store, '1000000001', 'ask', now=1.0)
    out = approvals.choose(store, '1000000001', 'exclude', note='uzak', now=2.0)
    assert out['choice'] == 'exclude'
    with store.transaction() as con:
        assert not approvals.approved(con, '1000000001')
        row = con.execute("SELECT value, proposer, status FROM claims WHERE listing_ref='1000000001' AND field='user_excluded'").fetchone()
    assert row['proposer'] == 'user' and row['status'] == 'accepted'


ROW = {'listing_id': '1000000001', 'type': 'Zeytinlik', 'area_m2': 6000.0, 'price_tl': 4.2e6, 'route_minutes_max': 15.0, 'penalties': {}}


def test_scenarios_show_the_score_after_a_good_an_easement_and_a_sit_answer_and_what_eliminates(store):
    est = {'fair': 900.0, 'scatter': 0.3, 'tier': 'model'}
    out = opportunity.answer_scenarios(ROW, est, 0.3, '', {}, False, None, {'slope_pct': 8.0, 'faces': 'güney', 'coast_m': 2500})
    good, easement, sit = out['cases']
    assert good['label'] == 'Cevaplar iyi gelirse' and good['detail'] == 'kadastral yol var, sit yok'
    assert good['score'] > out['now'] and easement['score'] == good['score'] - 10 and sit['score'] == good['score'] - 30
    assert out['eliminates'] == ['ne yol ne geçiş hakkı', '1. derece doğal sit', 'arkeolojik sit (her derece)']
    enclosed = opportunity.answer_scenarios(ROW, est, 0.3, '', {}, True, None, None)
    assert enclosed['cases'][0]['detail'] == 'geçiş hakkı var, sit yok' and [c['label'] for c in enclosed['cases']] == ['Cevaplar iyi gelirse', '2. ya da 3. derece doğal sit']
    assert enclosed['now'] < enclosed['cases'][0]['score']                          # an enclosed parcel carries −40 until the easement is answered
    only_sit = opportunity.answer_scenarios(ROW, est, 0.3, '', {'access': {'value': 'corridor_conditional'}}, False, None, None, asks=['archaeological_sit'])
    assert [c['label'] for c in only_sit['cases']] == ['Cevaplar iyi gelirse'] and only_sit['cases'][0]['detail'] == 'sit yok'        # the road is not asked: no road case
    assert only_sit['eliminates'] == ['arkeolojik sit (her derece)']
    assert opportunity.answer_scenarios(ROW, est, 0.3, '', {}, False, None, None, asks=['parcel_key'])['cases'] == []
    known = {'access': {'value': 'direct'}, 'natural_sit': {'value': 'none'}, 'archaeological_sit': {'value': False}}
    assert opportunity.answer_scenarios(ROW, est, 0.3, '', known, False, None, None)['cases'] == []


def test_the_board_lists_listings_waiting_for_a_question_and_those_already_chosen(store, monkeypatch):
    ranked = [{**ROW, 'listing_id': i, 'title': 't', 'neighborhood': 'Yaka Mh.', 'missing': ['yol'], 'parcel': f'153/{i[-1]}',
               'links': {'tkgm': 'https://parselsorgu.tkgm.gov.tr/#ara/idari/100/153/' + i[-1], 'sahibinden': 'https://www.sahibinden.com/' + i},
               'new': {'score': s, 'label': 'Adil fiyat', 'road': 'bilinmiyor', 'caps': ['çok ucuz görünüyor: fiyat ve alan doğrulanmalı']}, 'if_answered': {'now': s, 'cases': [], 'eliminates': []}}
              for i, s in (('1000000001', 50), ('1000000002', 70), ('1000000003', 60), ('1000000004', 40))]
    steps = {'1000000001': 'ask:sit', '1000000002': 'ask:access', '1000000003': 'tkgm.lookup', '1000000004': 'none'}

    def rank_all(store, mode='local', now=None, scenarios=None):
        for i, a in steps.items():
            scenarios(i, {'next_action': {'action': a}})
        return {'ranked': ranked, 'scatter': 0.3}
    monkeypatch.setattr(opportunity, 'rank_all', rank_all)
    monkeypatch.setattr(message, 'draft', lambda store, listing, mode, now, current=None: {'ok': True, 'fields': ['access'], 'text': 'soru ' + listing}
                        if listing != '1000000001' else {'ok': False, 'reason': 'waiting_for_reply', 'message': 'cevap bekleniyor'})
    approvals.choose(store, '1000000003', 'hold', now=1.0)
    out = approvals.board(store, now=10.0)
    assert [r['listing_id'] for r in out['rows']] == ['1000000002', '1000000001', '1000000003']      # undecided and askable first; no step and no choice: left out
    first = out['rows'][0]
    assert first['caps'] == ['çok ucuz görünüyor: fiyat ve alan doğrulanmalı']                        # why the score may mislead is said beside it
    assert first['asks_tr'] == ['kadastral yol'] and first['text'] == 'soru 1000000002' and first['links']['tkgm'].endswith('/153/2') and first['choice'] is None
    assert out['rows'][1]['why_not'] == 'cevap bekleniyor' and out['rows'][2]['choice'] == 'hold' and out['rows'][2]['why_code'] == 'no_ask_step'
    assert out['counts'] == {'ask': 0, 'hold': 1, 'undecided': 1, 'not_askable_now': 2} and out['sender_mode'] in message.MODES


def test_the_panel_page_shows_the_board_and_records_a_choice_only_with_the_csrf_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from verda.api import create_app
    from verda.storage import connect, initialize
    monkeypatch.setattr(approvals, 'board', lambda store, mode='local', now=None: {'rows': [], 'sender_mode': 'off', 'counts': {}})
    s = AgencyStore(tmp_path / 'agency.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    s.initialize()
    engine = connect(f"sqlite:///{tmp_path / 'v2.sqlite'}")
    initialize(engine)
    with TestClient(create_app(engine, 't' * 32, viewer_nonce='one-use', agency_store=s), base_url='https://testserver') as client:
        assert client.get('/control/asks').status_code == 200
        assert client.get('/control/private/asks').status_code == 401                                   # the viewer link first
        assert client.get('/control/unlock/one-use', follow_redirects=False).status_code == 303
        data = client.get('/control/private/asks').json()
        assert data['rows'] == [] and data['csrf_token']
        assert client.post('/control/private/asks/1000000001', json={'choice': 'ask'}).status_code in (401, 403)
        headers = {'X-Verda-CSRF': data['csrf_token'], 'Origin': 'https://testserver'}
        assert client.post('/control/private/asks/1000000001', json={'choice': 'ask'}, headers=headers).json() == {'choice': 'ask'}
        assert client.post('/control/private/asks/1000000001', json={'choice': 'send'}, headers=headers).status_code == 422
        assert client.post('/control/private/asks/abc', json={'choice': 'ask'}, headers=headers).status_code == 404
    with s.transaction() as con:
        assert approvals.approved(con, '1000000001')
