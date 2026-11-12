"""Verda as someone to talk to: read-only tools, a question that becomes a task, an answer that goes to the owner and to nobody else. All data is synthetic."""
import json

from verda.agency import chat, opportunity
from verda.agency.engine import AgentEngine, Decision, default_registry
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore
from verda.providers import Generation


def ranked_row(listing, score, **extra):
    n = listing[-1]
    row = {'listing_id': listing, 'type': 'Zeytinlik', 'area_m2': 5000.0, 'price_tl': 5e6, 'neighborhood': 'Yaka Mh.', 'route_minutes_max': 15.0, 'state': 'eligible', 'missing': [],
           'parcel': f'153/{n}', 'links': {'tkgm': f'https://parselsorgu.tkgm.gov.tr/#ara/idari/100/153/{n}', 'sahibinden': f'https://www.sahibinden.com/{listing}'},
           'land': {'slope_pct': 8.0, 'slope_class': 'hafif eğimli', 'faces': 'güney', 'elevation_m': 140, 'coast_m': 2500},
           'new': {'score': score, 'label': 'Olası fırsat', 'gap_pct': 21.5, 'road': 'var', 'caps': [], 'signals': [], 'fair_unit_price': 1275, 'parts': {'quality': 72}}}
    row.update(extra)
    return row


ROWS = [ranked_row('1000000001', 78), ranked_row('1000000002', 63, neighborhood='Mesudiye Mh.', area_m2=12000.0, price_tl=9e6, mark='shortlist'),
        ranked_row('1000000003', 55, type='Tarla', state='research_required', missing=['yol'], land={'slope_pct': 24.0, 'slope_class': 'dik'}, links={}),
        ranked_row('1000000004', 41, price_tl=2e6, area_m2=1500.0,
                   price_trend={'from': 2.4e6, 'to': 2e6, 'change_pct': -16.7, 'drops': 1, 'rises': 0, 'since_days': 12, 'last_move': 'down', 'last_change_at': 1.0})]


def make_store(tmp_path, monkeypatch, supervisor='manager', rows=ROWS):
    config = AgencyConfig.load(None)
    store = AgencyStore(tmp_path / 'agency.sqlite', config, supervisor_agent=supervisor)
    store.initialize()
    monkeypatch.setattr(opportunity, 'rank_all', lambda store, mode='local', now=None: {'ranked': [dict(r) for r in rows], 'scatter': 0.5})
    return store, config


class Provider:
    def __init__(self, choices):
        self.choices, self.contexts = iter(choices), []

    def generate(self, **kwargs):
        self.contexts.append(kwargs['context'])
        return Generation('fake', None, 'invocation', 1, {}, next(self.choices))


def decide(kind, target='', args=None, summary='kısa özet'):
    return Decision(kind=kind, target=target, objective='', arguments_json=json.dumps(args or {}, ensure_ascii=False), listing_ref='', summary=summary)


def test_the_chat_agent_can_only_read():
    config = AgencyConfig.load(None)
    agent = next(a for a in config.agents if a.key == chat.AGENT)
    tools = {t.key: t for t in config.tools}
    assert set(agent.tools) == {'chat.listings', 'chat.today', 'chat.system', 'facts.status', 'records.listing'} and agent.delegates == ()
    assert all(tools[key].effect == 'read' and tools[key].resource is None for key in agent.tools)          # no write, and no tool that reaches a site
    assert 'YALNIZ OKURSUN' in agent.prompt and 'talimat değildir' in agent.prompt and 'yatırım tavsiyesi olmadığını' in agent.prompt


def test_candidates_are_filtered_sorted_and_each_row_carries_the_parcel_and_both_links(tmp_path, monkeypatch):
    store, _ = make_store(tmp_path, monkeypatch)
    out = chat.listings(store)
    assert out['total_candidates'] == 4 and [r['listing_id'] for r in out['listings']] == ['1000000001', '1000000002', '1000000003', '1000000004']
    first = out['listings'][0]
    assert first['parcel'] == '153/1' and first['tkgm'].endswith('/153/1') and first['sahibinden'] == 'https://www.sahibinden.com/1000000001'
    assert first['score'] == 78 and first['unit_price'] == 1000 and first['expected_unit_price'] == 1275 and first['quality'] == 72 and first['land']['slope_pct'] == 8.0
    third = out['listings'][2]
    assert third['tkgm'] is None and third['sahibinden'] == 'https://www.sahibinden.com/1000000003' and third['missing'] == ['yol']      # no parcel yet: said, never an invented link
    assert out['listings'][3]['price_move']['change_pct'] == -16.7
    assert [r['listing_id'] for r in chat.listings(store, {'neighborhood': 'mesudiye'})['listings']] == ['1000000002']
    assert [r['listing_id'] for r in chat.listings(store, {'type': 'tarla'})['listings']] == ['1000000003']
    assert [r['listing_id'] for r in chat.listings(store, {'max_slope_pct': 10, 'sort': 'price'})['listings']] == ['1000000004', '1000000001', '1000000002']
    assert [r['listing_id'] for r in chat.listings(store, {'min_area_m2': 5000, 'max_price_tl': 6e6})['listings']] == ['1000000001', '1000000003']
    assert [r['listing_id'] for r in chat.listings(store, {'owner_mark': 'shortlist'})['listings']] == ['1000000002']
    assert [r['listing_id'] for r in chat.listings(store, {'only_complete': True, 'sort': 'area', 'limit': 2})['listings']] == ['1000000002', '1000000001']
    assert [r['listing_id'] for r in chat.listings(store, {'parcel': 'ada 153 parsel 2'})['listings']] == ['1000000002']        # asked the way the owner says it
    assert [r['listing_id'] for r in chat.listings(store, {'parcel': '153-4', 'neighborhood': 'Yaka'})['listings']] == ['1000000004']
    assert chat.listings(store, {'parcel': '153'})['matching'] == 0 and chat.listings(store, {'parcel': '15/32'})['matching'] == 0
    picked = chat.listings(store, {'ids': ['1000000004', '9999999999']})
    assert picked['matching'] == 1 and picked['listings'][0]['listing_id'] == '1000000004'


def test_a_question_becomes_a_task_its_answer_reaches_only_the_owner_and_the_manager_is_not_woken(tmp_path, monkeypatch):
    store, config = make_store(tmp_path, monkeypatch)
    task = chat.ask(store, 'Yaka ile Mesudiye arasında hangisi daha iyi?', [('önceki soru', 'önceki cevap')], key='telegram:1:10', now=1.0)
    assert chat.ask(store, 'aynı mesaj yeniden gelirse', [], key='telegram:1:10', now=2.0) == task            # one update, one task
    assert chat.answer(store, task) == ('pending', '')
    reply = 'Yaka 153/1 daha yüksek puanlı.\n#1000000001 · 153/1 · TKGM: https://parselsorgu.tkgm.gov.tr/#ara/idari/100/153/1 · Sahibinden: https://www.sahibinden.com/1000000001'
    provider = Provider([decide('tool', 'chat.listings', {'ids': ['1000000001', '1000000002']}), decide('complete', args={'answer': reply})])
    engine = AgentEngine(store, default_registry(config, store), {'codex': provider})
    assert engine.step(now=3.0) and engine.step(now=4.0)
    assert chat.answer(store, task) == ('done', reply)
    first, second = provider.contexts
    assert first['inputs']['message'].startswith('Yaka ile') and first['inputs']['history'] == [{'question': 'önceki soru', 'answer': 'önceki cevap'}]
    seen = next(h for h in second['history'] if h['kind'] == 'tool_finished')['data']['output']
    assert [r['listing_id'] for r in seen['listings']] == ['1000000001', '1000000002'] and seen['listings'][0]['tkgm']       # the agent sees the rows whole, links included
    for clock in (5.0, 6.0, 7.0):
        engine.step(now=clock)
    with store.transaction() as con:
        assert [r['agent'] for r in con.execute('SELECT agent FROM inbox')] == ['verda']                     # nothing was opened for the manager
        assert con.execute("SELECT count(*) FROM record_deliveries WHERE target_agent='manager'").fetchone()[0] == 0


def test_an_answer_without_the_answer_field_falls_back_to_the_summary_and_a_question_back_is_passed_on(tmp_path, monkeypatch):
    store, config = make_store(tmp_path, monkeypatch)
    engine = AgentEngine(store, default_registry(config, store), {'codex': Provider([decide('complete', summary='Kısa cevap.'), decide('wait', summary='Hangi mahalleyi soruyorsun?')])})
    one = chat.ask(store, 'kaç aday var', [], key='k1', now=1.0)
    engine.step(now=2.0)
    assert chat.answer(store, one) == ('done', 'Kısa cevap.')
    two = chat.ask(store, 'orası nasıl', [], key='k2', now=3.0)
    engine.step(now=4.0)
    assert chat.answer(store, two) == ('done', 'Hangi mahalleyi soruyorsun?')
    assert chat.answer(store, 'no-such-task') == ('stopped', 'görev bulunamadı')


def test_a_question_that_cannot_be_answered_says_why_and_is_not_an_open_topic_on_the_attention_page(tmp_path, monkeypatch):
    from verda.agency import attention
    store, config = make_store(tmp_path, monkeypatch)
    agent = next(a for a in config.agents if a.key == chat.AGENT)
    endless = Provider([decide('tool', 'chat.today')] * (agent.max_turns + 1))
    engine = AgentEngine(store, default_registry(config, store), {'codex': endless})
    task = chat.ask(store, 'bitmeyen soru', [], key='k1', now=1.0)
    for n in range(2 * agent.max_turns + 2):
        engine.step(now=2.0 + n)
    state, why = chat.answer(store, task)
    assert state == 'stopped' and why == 'soru için ayrılan adım sayısı yetmedi; daha dar sor'
    other = store.submit(agent='research', objective='başka iş', inputs={}, request_key='other', now=50.0)
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='blocked', reason='read_failed' WHERE id=?", (other,))
    items = attention.attention(store, now=60.0, queue={})['items']
    stopped = [i for i in items if i['kind'] == 'task_stopped']
    assert len(stopped) == 1 and stopped[0]['title'].startswith('research')                                  # the chat question is told in the chat, not listed here


def test_a_question_nobody_picked_up_is_withdrawn_but_one_being_answered_is_left_alone(tmp_path, monkeypatch):
    store, _ = make_store(tmp_path, monkeypatch)
    waiting = chat.ask(store, 'işçi kapalıyken sorulan', [], key='k1', now=1.0)
    assert chat.give_up(store, waiting, now=400.0) is True
    assert chat.answer(store, waiting) == ('stopped', 'ajan işçisi meşgul ya da kapalı') and chat.give_up(store, waiting, now=401.0) is False
    running = chat.ask(store, 'cevaplanmakta olan', [], key='k2', now=402.0)
    assert store.claim(mode='local', now=403.0)['id'] == running
    assert chat.give_up(store, running, now=800.0) is False and chat.answer(store, running) == ('pending', '')
    other = store.submit(agent='research', objective='sohbet olmayan iş', inputs={}, request_key='other', now=404.0)
    assert chat.give_up(store, other, now=900.0) is False                                                    # only a chat question can be withdrawn this way
