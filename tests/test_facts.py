"""Facts layer: the model proposes, plain code accepts. All data here is synthetic."""
import json
import sqlite3

import pytest

from test_agency import Provider, decision
from verda.agency.browser import BrowserBridge, Listing
from verda.agency.engine import AgentEngine, default_registry
from verda.agency.registry import AgencyConfig, Registry
from verda.agency.store import AgencyStore
from verda.facts.assemble import assemble
from verda.facts.extract import extract
from verda.facts.model import Proposal
from verda.facts.text import fold, locate
from verda.facts.verify import verify
from verda.policy import Policy

ID = '9000000489'


# ---- pure helpers -----------------------------------------------------------------------------
def listing_payload(**over):
    payload = {'listing_id': ID, 'title': "Datça Kızlan'da 1.510 m² Tarla",
               'url': f'https://www.sahibinden.com/ilan/emlak-arsa-satilik-datca-kizlan-{ID}/detay',
               'price_tl': 8250000.0, 'area_m2': 1510.0, 'neighborhood': 'Kızlan', 'parcel_key': '123/45',
               'description': 'Ada 123 parsel 45. Parselin doğrudan kadastral yola cephesi vardır. Sit yoktur.',
               'attributes': {'m²': '1.510', 'tapu durumu': 'Müstakil Parsel', 'ada no': '123', 'parsel no': '45'}}
    payload.update(over)
    return payload


def obs(tool='sahibinden.read_listing', payload=None, *, mode='local', listing_ref=ID, obs_id='obs-1'):
    return {'id': obs_id, 'mode': mode, 'tool': tool, 'listing_ref': listing_ref,
            'payload': listing_payload() if payload is None else payload}


def thread(*messages, listing=ID):
    return obs('sahibinden.read_thread', {'listing_id': listing, 'messages': list(messages)}, obs_id='obs-thread')


def seller(text, id='m2', **extra):
    return {'id': id, 'role': 'seller', 'text': text, **extra}


def propose(field, value, *, proposer='llm:manager', **kw):
    return Proposal(listing_ref=kw.pop('listing_ref', ID), field=field, value=value, proposer=proposer,
                    mode=kw.pop('mode', 'local'), observation_id=kw.pop('observation_id', 'obs-1'), **kw)


def sit_claim(text, field='natural_sit', value='none', quote=None, **extra):
    return verify(propose(field, value, quote=quote or text.rstrip('.'), source_ref='message:m2', **extra),
                  thread(seller(text)))


# ---- text -------------------------------------------------------------------------------------
def test_matching_treats_turkish_dotted_and_dotless_i_as_one_letter():
    assert fold('SİT DURUMU BULUNMAMAKTADIR.') == fold('sit durumu bulunmamaktadır')
    assert locate('sit yoktur', 'Merhaba. SİT YOKTUR!') is not None
    assert locate('doğal sit', 'dogal sit yok') is None      # other diacritics are kept


# ---- verifier: the five checks ----------------------------------------------------------------
def test_accepts_a_verbatim_quote_from_the_seller_at_the_source_grade():
    verdict = sit_claim('Sit durumu yoktur.')
    assert (verdict.status, verdict.grade) == ('accepted', 'beyan')


@pytest.mark.parametrize('proposal,observation,reason', [
    # the quote is not in the message
    (propose('natural_sit', 'none', quote='sit yoktur', source_ref='message:m2'), thread(seller('Tapu müstakil.')), 'quote_not_in_source'),
    # a message id that does not exist and a quote that is nowhere in the seller's messages
    (propose('natural_sit', 'none', quote='sit yoktur', source_ref='message:m9'), thread(seller('Tapu müstakil.')), 'quote_not_in_source'),
    # no message cited for a conversation
    (propose('natural_sit', 'none', quote='sit yoktur'), thread(seller('Sit yoktur.')), 'source_ref_required'),
    # the user's own question is not the seller's answer
    (propose('natural_sit', 'none', quote='sit var mı', source_ref='message:m1'),
     thread({'id': 'm1', 'role': 'user', 'text': 'Sit var mı?'}), 'quote_not_from_seller'),
    # truncated quote, truncated message, preview flagged as cut
    (propose('natural_sit', 'none', quote='sit yoktur…', source_ref='message:m2'), thread(seller('Sit yoktur.')), 'quote_truncated'),
    (propose('natural_sit', 'none', quote='sit yoktur', source_ref='message:m2'), thread(seller('Sit yoktur...')), 'source_truncated'),
    (propose('natural_sit', 'none', quote='sit yoktur', source_ref='message:m2'), thread(seller('Sit yoktur', truncated=True)), 'source_truncated'),
    # a model must always quote
    (propose('natural_sit', 'none'), thread(seller('Sit yoktur.')), 'quote_required'),
    # a quote about one sit type cannot support the other
])
def test_rejections(proposal, observation, reason):
    verdict = verify(proposal, observation)
    assert (verdict.status, verdict.reason) == ('rejected', reason)


def test_a_wrong_message_id_does_not_sink_a_quote_that_is_really_in_the_sellers_messages():
    verdict = verify(propose('natural_sit', 'none', quote='sit yoktur', source_ref='message:m9'), thread(seller('Sit yoktur.')))
    assert (verdict.status, verdict.grade) == ('accepted', 'beyan')


def test_a_hedged_sentence_goes_to_the_user_even_when_the_quote_is_clean():
    verdict = sit_claim('Sanırım sit durumu yoktur, emin değilim.', quote='sit durumu yoktur')
    assert (verdict.status, verdict.reason) == ('needs_review', 'hedged')
    # the hedge is judged on the sentence, but only that sentence
    assert sit_claim('Emin değilim. Sit durumu yoktur.', quote='sit durumu yoktur').status == 'accepted'


def test_a_quote_too_short_to_mean_anything_goes_to_the_user():
    assert sit_claim('Yok.', quote='yok').reason == 'quote_too_short'


def test_exclusion_needs_the_parcel_itself_to_be_the_subject():
    source = obs(payload=listing_payload(description='Bu parsel hisseli tapudur. Komşu parselde sit var.'))
    own = verify(propose('shared_title', True, quote='hisseli tapudur', source_ref='description'), source)
    assert (own.status, own.grade) == ('accepted', 'beyan')
    other = verify(propose('shared_title', True, quote='hisseli tapudur', source_ref='description', subject='neighbor'), source)
    assert (other.status, other.reason) == ('needs_review', 'subject_not_this_parcel')
    # the same doubt costs nothing when the claim only keeps a listing alive
    assert verify(propose('shared_title', False, quote='hisseli tapudur', source_ref='description', subject='neighbor'), source).status == 'accepted'


def test_a_model_never_extracts_numbers_from_tool_results():
    search = obs('sahibinden.search', {'listings': [{'listing_id': ID, 'title': 'Tarla', 'price_tl': 1.0}]})
    verdict = verify(propose('price_tl', 1.0, quote='tarla price 1 TL'), search)
    assert verdict.reason == 'model_not_allowed_for_tool'


def test_listing_identity_follows_the_content_not_the_label():
    mislabeled = obs(payload=listing_payload(listing_id='9000000999'), listing_ref=ID)
    assert verify(propose('price_tl', 1.0, proposer='code:x@1'), mislabeled).reason == 'listing_mismatch'
    assert verify(propose('price_tl', 1.0, proposer='code:x@1', listing_ref='9000000999'), mislabeled).status == 'accepted'


def test_mode_tool_and_value_boundaries():
    assert verify(propose('price_tl', 1.0, proposer='code:x@1', mode='synthetic'), obs()).reason == 'mode_mismatch'
    assert verify(propose('route_minutes', 41.0, proposer='code:x@1'), obs()).reason == 'tool_not_allowed_for_field'
    assert verify(propose('parcel_key', '0/0', proposer='code:x@1'), obs()).reason == 'parcel_key_malformed'
    assert verify(propose('price_tl', True, proposer='code:x@1'), obs()).reason == 'value_not_positive_number'
    assert verify(propose('price_tl', -5, proposer='code:x@1'), obs()).reason == 'value_not_positive_number'
    assert verify(propose('natural_sit', 'maybe', proposer='code:x@1'), obs()).reason == 'value_not_in_vocabulary'
    assert verify(propose('shared_title', 1, proposer='code:x@1'), obs()).reason == 'value_not_boolean'
    assert verify(propose('price_tl', 1.0, proposer='code:x@1', observation_id=None), None).reason == 'observation_missing'


def test_a_user_decision_needs_no_observation_and_carries_the_user_grade():
    verdict = verify(Proposal(listing_ref=ID, field='destination_verified', value=True, proposer='user'), None)
    assert (verdict.status, verdict.grade) == ('accepted', 'kullanici')


# ---- extractors: structured fields only -------------------------------------------------------
def fields(proposals):
    return {p.field: p.value for p in proposals}


def test_extractors_read_structured_fields_and_leave_free_text_to_the_model():
    got = fields(extract('sahibinden.read_listing', 'obs-1', 'local', listing_payload()))
    assert got == {'price_tl': 8250000.0, 'sale_area_m2': 1510.0, 'parcel_key': '123/45', 'shared_title': False}
    # "Sit yoktur." and "yola cephesi" are in the description; code does not read them.
    assert 'natural_sit' not in got and 'access' not in got


@pytest.mark.parametrize('status,expected,basis', [('Hisseli Tapu', True, 'statement'), ('Müstakil Parsel', False, 'statement'),
                                                    ('Tahsis', False, 'absence')])   # unknown wording: the listing does not say hisseli
def test_title_status_row(status, expected, basis):
    proposals = extract('sahibinden.read_listing', 'obs-1', 'local', listing_payload(attributes={'tapu durumu': status}))
    shared = [p for p in proposals if p.field == 'shared_title']
    assert [(p.value, p.basis) for p in shared] == [(expected, basis)]


def test_placeholders_and_junk_establish_nothing():
    assert 'parcel_key' not in fields(extract('sahibinden.read_listing', 'o', 'local', listing_payload(parcel_key='0/0', attributes={})))
    from_attributes = listing_payload(parcel_key=None, attributes={'ada no': '137', 'parsel no': '104'})
    assert fields(extract('sahibinden.read_listing', 'o', 'local', from_attributes))['parcel_key'] == '137/104'
    assert extract('sahibinden.read_listing', 'o', 'local', 'not a dict') == []
    assert extract('sahibinden.read_listing', 'o', 'local', {'title': 'no id'}) == []


def test_search_rows_become_card_claims_per_listing():
    rows = [{'listing_id': ID, 'title': 'A', 'price_tl': 1.0, 'area_m2': 2000.0}, {'listing_id': '9000000002', 'title': 'B', 'price_tl': 2.0}]
    proposals = extract('sahibinden.search', 'obs-s', 'local', {'listings': rows})
    assert {(p.listing_ref, p.field) for p in proposals} == {(ID, 'price_tl'), (ID, 'sale_area_m2'), ('9000000002', 'price_tl')}


# ---- assembler --------------------------------------------------------------------------------
def claim(field, value, grade, *, seq, kind='listing', key=None, scope=None, quote=None):
    return {'id': f'c{seq}', 'seq': seq, 'field': field, 'value': value, 'grade': grade, 'source_kind': kind,
            'key': key, 'scope': scope or ('parcel' if kind in ('official', 'calc') else 'listing'), 'quote': quote}


def test_a_weaker_grade_than_the_policy_allows_is_only_a_lead():
    got = assemble([claim('route_minutes', 41.0, 'beyan', seq=1), claim('natural_sit', 'none', 'beyan', seq=2)])
    assert 'route_minutes' not in got.values and got.leads['route_minutes'][0]['grade'] == 'beyan'
    assert got.values['natural_sit'] == 'none'          # statements may screen sit


def test_newer_claims_of_the_same_source_replace_older_ones_without_a_conflict():
    got = assemble([claim('price_tl', 9e6, 'beyan', seq=1), claim('price_tl', 8e6, 'beyan', seq=2),
                    claim('price_tl', 7e6, 'rapor', seq=3)])        # card data never contradicts the detail page
    assert got.values['price_tl'] == 8e6 and not got.conflicts and not got.contradictions


def test_same_grade_from_different_sources_that_disagree_is_a_conflict_that_withholds_the_field():
    got = assemble([claim('shared_title', True, 'beyan', seq=1, kind='listing'), claim('shared_title', False, 'beyan', seq=2, kind='thread')])
    assert 'shared_title' not in got.values and got.conflicts == ['shared_title']
    agree = assemble([claim('shared_title', False, 'beyan', seq=1, kind='listing'), claim('shared_title', False, 'beyan', seq=2, kind='thread')])
    assert agree.values['shared_title'] is False and not agree.conflicts


def test_official_evidence_corrects_a_statement_as_a_listing_error_but_the_user_decision_leads():
    official = assemble([claim('natural_sit', 'first_degree', 'bagimsiz', seq=1, kind='calc'), claim('natural_sit', 'none', 'beyan', seq=2)])
    assert official.values['natural_sit'] == 'first_degree'
    assert not official.contradictions and official.errors[0]['other']['grade'] == 'beyan'      # the statement was wrong
    user = assemble([claim('natural_sit', 'none', 'kullanici', seq=3, kind='user'), claim('natural_sit', 'first_degree', 'bagimsiz', seq=1, kind='calc')])
    assert user.values['natural_sit'] == 'none'           # the working assumption...
    assert user.contradictions[0]['reopen'] and not user.errors     # ...but the official result stays visible


def test_a_disputed_parcel_withholds_parcel_bound_facts_only():
    got = assemble([claim('parcel_key', '1/1', 'beyan', seq=1), claim('parcel_key', '2/2', 'beyan', seq=2, kind='thread'),
                    claim('price_tl', 8e6, 'beyan', seq=3), claim('natural_sit', 'none', 'beyan', seq=4),
                    claim('sale_scope_resolved', True, 'bagimsiz', seq=5, kind='calc')])
    assert got.conflicts == ['parcel_identity']
    assert got.values.get('price_tl') == 8e6 and got.values.get('natural_sit') == 'none'    # what the listing itself says
    assert 'sale_scope_resolved' not in got.values                                          # computed from the parcel


def test_route_measurements_from_several_engines_are_all_kept():
    got = assemble([claim('route_minutes', 41.0, 'bagimsiz', seq=1, kind='calc', key='osrm'),
                    claim('route_minutes', 48.0, 'bagimsiz', seq=2, kind='calc', key='valhalla'),
                    claim('route_minutes', 40.0, 'bagimsiz', seq=3, kind='calc', key='osrm')])
    assert got.values['route_minutes'] == [40.0, 48.0] and not got.conflicts


def test_policy_table_is_data_not_code():
    strict = Policy(field_grades={**Policy().field_grades, 'natural_sit': ('resmi', 'bagimsiz')})
    got = assemble([claim('natural_sit', 'none', 'beyan', seq=1)], strict)
    assert 'natural_sit' not in got.values and 'natural_sit' in got.leads


# ---- database: the whole path -----------------------------------------------------------------
def python_listing_store(tmp_path, payloads, tools=('sahibinden.read_listing',), extra=None):
    """The default configuration, with the browser reads served by plain python handlers."""
    data = AgencyConfig.load().model_dump(mode='json')
    for tool in data['tools']:
        if tool['key'] in tools or tool['key'] == 'geo.route':
            tool.update(transport='python', resource=None, min_interval_seconds=0)
    config = AgencyConfig.model_validate(data)
    store = AgencyStore(tmp_path / 'agency.sqlite', config, supervisor_agent=None)
    store.initialize()
    calls = iter(payloads)
    handlers = {'sahibinden.read_listing': lambda a: next(calls), 'sahibinden.read_thread': lambda a: next(calls),
                'geo.route': lambda a: next(calls)}
    registry = Registry(config, {**{k: v for k, v in handlers.items() if k in tools or k == 'geo.route'}, **(extra or {})})
    return store, registry


def read(store, registry, key, *, tool='sahibinden.read_listing', agent='sahibinden', listing=ID, start=1, inputs=None):
    store.submit(agent=agent, objective='Read fixture', inputs=inputs or {'listing_id': listing},
                 listing_ref=listing, request_key=key, executor_tool=tool, now=start)
    engine = AgentEngine(store, registry, providers={})
    assert engine.step(now=start + 1) and engine.step(now=start + 2)


def table(store, sql, *args):
    with store.transaction() as con:
        return [dict(r) for r in con.execute(sql, args)]


def status(store, listing=ID):
    from verda.agency.research import fact_status
    return fact_status(store, listing)


def test_a_read_listing_becomes_claims_a_versioned_decision_and_a_next_action(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    claims = table(store, "SELECT field,status,grade,proposer FROM claims ORDER BY seq")
    assert {(c['field'], c['grade']) for c in claims if c['status'] == 'accepted'} == {
        ('price_tl', 'beyan'), ('sale_area_m2', 'beyan'), ('parcel_key', 'beyan'), ('shared_title', 'beyan')}
    view = status(store)
    assert view['decision']['state'] == 'research_required'
    assert set(view['decision']['reasons']) == {'sale_area', 'archaeological_sit', 'natural_sit', 'access', 'route'}
    assert view['next_action'] == {'action': 'tkgm.lookup', 'why': 'official_area_and_scope_needed',
                                   'also': ['sahibinden.read_thread', 'geo.route']}
    assert view['to_verify_officially'] == ['parcel_key', 'sale_area_m2']
    assert view['persisted_assessment']['state'] == 'research_required' and view['persisted_assessment']['current']
    events = [e['kind'] for e in store.overview()['events']]
    assert 'facts_recorded' in events and 'facts_failed' not in events


def test_a_structured_hisseli_title_excludes_without_any_model_or_message(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(attributes={'tapu durumu': 'Hisseli Tapu'})])
    read(store, registry, 'one')
    view = status(store)
    assert view['decision']['state'] == 'excluded' and view['decision']['reasons'] == ['shared_title']
    assert view['next_action']['action'] == 'none'
    assert view['facts']['shared_title']['quote'] == 'Hisseli Tapu'       # the exclusion carries its evidence


def test_rereading_an_unchanged_listing_adds_no_assessment_but_a_price_change_adds_one(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(), listing_payload(), listing_payload(price_tl=7900000.0)])
    read(store, registry, 'one'); read(store, registry, 'two', start=10)
    assert len(table(store, 'SELECT id FROM assessments')) == 1
    assert len(table(store, "SELECT id FROM claims WHERE field='price_tl'")) == 2        # provenance is kept
    read(store, registry, 'three', start=20)
    assert [r['state'] for r in table(store, 'SELECT state FROM assessments ORDER BY seq')] == ['research_required'] * 2
    assert status(store)['facts']['price_tl']['value'] == 7900000.0


def test_claims_and_assessments_are_append_only(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        for sql in ("UPDATE claims SET status='accepted'", 'DELETE FROM claims', "UPDATE assessments SET state='eligible'", 'DELETE FROM assessments'):
            with pytest.raises(sqlite3.IntegrityError, match='append_only'):
                con.execute(sql)


def test_an_extractor_failure_never_loses_the_observation(tmp_path, monkeypatch):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    monkeypatch.setattr(type(store.facts), 'submit', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    read(store, registry, 'one')
    view = store.overview()
    assert len(view['records']['observations']) == 1 and not table(store, 'SELECT id FROM claims')
    assert 'facts_failed' in [e['kind'] for e in view['events']]


def test_a_mislabeled_task_is_made_visible_and_facts_follow_the_content(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(listing_id='9000000999')])
    read(store, registry, 'one')
    assert {c['listing_ref'] for c in table(store, 'SELECT listing_ref FROM claims')} == {'9000000999'}
    assert 'facts_listing_mismatch' in [e['kind'] for e in store.overview()['events']]


def test_synthetic_observations_never_reach_local_facts(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    store.submit(agent='sahibinden', objective='Read fixture', inputs={'listing_id': ID}, listing_ref=ID, request_key='syn',
                 executor_tool='sahibinden.read_listing', mode='synthetic', now=1)
    engine = AgentEngine(store, registry, providers={})
    assert engine.step(mode='synthetic', now=2) and engine.step(mode='synthetic', now=3)
    assert {c['mode'] for c in table(store, 'SELECT mode FROM claims')} == {'synthetic'}
    assert status(store)['decision'] is None


def test_the_full_screening_story_from_listing_to_eligible_with_honest_labels(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(), {
        'listing_id': ID, 'messages': [{'id': 'm1', 'role': 'user', 'text': 'Sit durumu nedir?'},
                                       {'id': 'm2', 'role': 'seller', 'text': 'Doğal sit ve arkeolojik sit yoktur. Parsel yola cephelidir.'}]},
        {'engines': [{'engine': 'osrm', 'ok': True, 'minutes': 41.0}, {'engine': 'valhalla', 'ok': True, 'minutes': 44.0}]}], tools=('sahibinden.read_listing', 'sahibinden.read_thread'))
    read(store, registry, 'listing')
    read(store, registry, 'thread', tool='sahibinden.read_thread', start=10)
    read(store, registry, 'route', tool='geo.route', agent='research', start=20)
    thread_obs = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_thread'")[0]['id']

    def submit(p):
        with store.transaction() as con:
            return store.facts.submit(con, p, now=30)
    ok = [submit(Proposal(listing_ref=ID, field=f, value=v, proposer='llm:manager', observation_id=thread_obs,
                          source_ref='message:m2', quote=q))
          for f, v, q in (('natural_sit', 'none', 'Doğal sit ve arkeolojik sit yoktur'),
                          ('archaeological_sit', False, 'Doğal sit ve arkeolojik sit yoktur'),
                          ('access', 'direct', 'Parsel yola cephelidir'))]
    assert [r['status'] for r in ok] == ['accepted'] * 3
    with store.transaction() as con:
        store.facts.decide(con, ID, 'local', 'sale_scope_resolved', True, 'Tek parsel satışı, ilan ve TKGM alanı uyuşuyor', 31)
        store.facts.assess(con, ID, 'local', 33)
    view = status(store)
    assert view['decision']['state'] == 'eligible'
    assert view['next_action']['action'] == 'user_review'
    assert {f: (view['facts'][f]['grade'] if f != 'route_minutes' else {r['grade'] for r in view['facts'][f]}) for f in ('natural_sit', 'route_minutes', 'sale_scope_resolved', 'destination_verified')} == {
        'natural_sit': 'beyan', 'route_minutes': {'bagimsiz'}, 'sale_scope_resolved': 'kullanici', 'destination_verified': 'kullanici'}
    # The buyer's checklist: what still rests on a statement, not on an official record.
    assert view['to_verify_officially'] == ['parcel_key', 'sale_area_m2', 'archaeological_sit', 'natural_sit', 'access']


def test_review_items_are_answered_by_the_user_and_keep_the_source_grade(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(), {
        'listing_id': ID, 'messages': [{'id': 'm2', 'role': 'seller', 'text': 'Sanırım sit durumu yoktur, belediyeden sorabilirsiniz.'}]}],
        tools=('sahibinden.read_listing', 'sahibinden.read_thread'))
    read(store, registry, 'listing'); read(store, registry, 'thread', tool='sahibinden.read_thread', start=10)
    thread_obs = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_thread'")[0]['id']
    with store.transaction() as con:
        held = store.facts.submit(con, Proposal(listing_ref=ID, field='natural_sit', value='none', proposer='llm:manager',
                                                observation_id=thread_obs, source_ref='message:m2', quote='sit durumu yoktur'), now=20)
    assert (held['status'], held['reason']) == ('needs_review', 'hedged')
    view = status(store)
    assert view['review'][0]['claim_id'] == held['claim_id'] and view['next_action']['action'] == 'human_review'
    assert 'natural_sit' not in view['facts']                         # nothing was accepted behind the user's back
    with store.transaction() as con:
        accepted = store.facts.resolve_review(con, held['claim_id'], accepted=True, note='Satıcıyla telefonda teyit', now=21)
        with pytest.raises(ValueError, match='open review'):
            store.facts.resolve_review(con, held['claim_id'], accepted=False, note='again', now=22)
    assert accepted['grade'] == 'beyan'                               # confirmed, not upgraded to an official record
    view = status(store)
    assert view['facts']['natural_sit']['value'] == 'none' and not view['review']
    # rejecting instead leaves the field unknown
    store2, registry2 = python_listing_store(tmp_path / 'b', [listing_payload(), {
        'listing_id': ID, 'messages': [{'id': 'm2', 'role': 'seller', 'text': 'Sanırım sit durumu yoktur.'}]}],
        tools=('sahibinden.read_listing', 'sahibinden.read_thread'))
    read(store2, registry2, 'listing'); read(store2, registry2, 'thread', tool='sahibinden.read_thread', start=10)
    thread_obs2 = table(store2, "SELECT id FROM observations WHERE tool='sahibinden.read_thread'")[0]['id']
    with store2.transaction() as con:
        held2 = store2.facts.submit(con, Proposal(listing_ref=ID, field='natural_sit', value='none', proposer='llm:manager',
                                                  observation_id=thread_obs2, source_ref='message:m2', quote='sit durumu yoktur'), now=20)
        store2.facts.resolve_review(con, held2['claim_id'], accepted=False, note='Emin değil', now=21)
    assert 'natural_sit' not in status(store2)['facts']


def test_user_decisions_are_events_so_changing_your_mind_back_still_counts(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        for at, value in ((10, True), (11, False), (12, True)):
            store.facts.decide(con, ID, 'local', 'destination_verified', value, 'Mertur girişi', at)   # same note on purpose
    assert status(store)['facts']['destination_verified']['value'] is True


def test_legacy_exclusion_overrides_any_new_action(tmp_path):
    from verda.agency.research import fact_status
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        view = store.facts.status(con, ID, 'local', legacy_lifecycle='excluded')
    assert view['next_action'] == {'action': 'none', 'why': 'legacy_excluded'}


# ---- manager tool, receipts, migration --------------------------------------------------------
def test_the_manager_reads_facts_through_a_tool_whose_result_is_not_a_source_observation(tmp_path):
    config = AgencyConfig.load()
    store = AgencyStore(tmp_path / 'agency.sqlite', config, supervisor_agent=None); store.initialize()
    registry = default_registry(config, store)
    store.submit(agent='manager', objective='Report the listing state', inputs={}, listing_ref=ID, request_key='m', now=1)
    provider = Provider([decision('tool', 'facts.status', args={'listing_id': ID}), decision('complete')])
    engine = AgentEngine(store, registry, {'codex': provider})
    assert engine.step(now=2) and engine.step(now=3)
    seen = provider.calls[1]['context']['history'][-1]['data']['output']
    assert seen['listing_id'] == ID and seen['decision'] is None and seen['next_action']['action'] == 'sahibinden.read_listing'
    assert [o['kind'] for o in store.overview()['records']['observations']] == ['calculation']
    assert not table(store, 'SELECT id FROM claims')


def test_browser_receipts_carry_attributes_through_the_real_bridge(tmp_path):
    config = AgencyConfig.load()
    store = AgencyStore(tmp_path / 'agency.sqlite', config, supervisor_agent=None); store.initialize()
    registry = default_registry(config, store)
    store.submit(agent='sahibinden', objective='Read', inputs={'listing_id': ID}, listing_ref=ID, request_key='r',
                 executor_tool='sahibinden.read_listing', now=1)
    engine = AgentEngine(store, registry, providers={}); assert engine.step(now=2)
    bridge = BrowserBridge(store); bridge.attach('w', now=3)
    job = bridge.claim('w', now=4)
    bridge.finish(job['id'], 'w', job['token'], result=listing_payload(attributes={'tapu durumu': 'Hisseli Tapu'}), now=5)
    view = status(store)
    assert view['decision']['state'] == 'excluded' and view['facts']['shared_title']['quote'] == 'Hisseli Tapu'


def test_listing_attributes_are_bounded():
    base = {k: v for k, v in listing_payload().items() if k != 'attributes'}
    for bad in ({'x' * 81: 'v'}, {'k': 'v' * 301}, {str(i): 'v' for i in range(41)}, {' ': 'v'}):
        with pytest.raises(ValueError):
            Listing.model_validate({**base, 'attributes': bad})
    assert Listing.model_validate(base).attributes is None


def test_v7_databases_upgrade_in_place_without_losing_work(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        con.execute('DROP TABLE claims'); con.execute('DROP TABLE assessments'); con.execute('UPDATE agency_meta SET version=7')
    before = table(store, 'SELECT id FROM inbox')
    store.initialize(); store.initialize()
    assert table(store, 'SELECT version FROM agency_meta') == [{'version': 9}]
    assert table(store, 'SELECT id FROM inbox') == before and table(store, 'SELECT id FROM claims') == []
    from verda.agency.health import inspect_health
    assert inspect_health(store)['storage_accessible'] is True


def test_backfill_turns_older_observations_into_facts_and_is_idempotent(tmp_path, monkeypatch):
    store, registry = python_listing_store(tmp_path, [listing_payload(), listing_payload(listing_id='9000000002', attributes={'tapu durumu': 'Hisseli Tapu'})])
    with monkeypatch.context() as m:
        m.setattr(type(store.facts), 'on_observation', lambda *a, **k: None)      # as if recorded before v8
        read(store, registry, 'one'); read(store, registry, 'two', listing='9000000002', start=10)
    assert not table(store, 'SELECT id FROM claims')
    with store.transaction() as con:
        first = store.facts.backfill(con, 'local', 50)
        second = store.facts.backfill(con, 'local', 60)
    assert first['observations'] == 2 and first['accepted'] == 8 and first['assessed'] == 2
    assert second['accepted'] == 0 and second['duplicate'] == 8 and second['assessed'] == 0
    assert status(store, '9000000002')['decision']['state'] == 'excluded' and status(store)['decision']['state'] == 'research_required'


# ---- "the listing does not say hisseli, so it is not hisseli" ---------------------------------------
def clean(**over):
    base = dict(attributes={'ada no': '123', 'parsel no': '45'}, description='Ada 123 parsel 45. Manzaralı zeytinlik.')
    return listing_payload(**{**base, **over})


def test_the_assumption_is_code_only_clearing_only_and_needs_a_full_listing():
    base = dict(field='shared_title', value=False, basis='absence', source_ref='absence:no_shared_title_words')
    ok = verify(propose(proposer='code:no_disclosure@1', **base), obs())
    assert (ok.status, ok.grade, ok.reason) == ('accepted', 'rapor', 'no_disclosure_assumed')
    assert verify(propose(proposer='llm:manager', **base), obs()).reason == 'absence_not_allowed'
    assert verify(propose(proposer='code:x@1', **{**base, 'value': True}), obs()).reason == 'absence_not_allowed'
    assert verify(propose(proposer='code:x@1', **{**base, 'field': 'natural_sit', 'value': 'none'}), obs()).reason == 'absence_not_allowed'
    assert verify(propose(proposer='code:x@1', quote='anything long enough', **base), obs()).reason == 'absence_not_allowed'
    assert verify(propose(proposer='code:x@1', **base), thread(seller('x'))).reason == 'absence_needs_full_listing'


def test_a_listing_that_says_nothing_about_hisse_is_treated_as_not_hisseli_and_labelled_as_such(tmp_path):
    store, registry = python_listing_store(tmp_path, [clean()])
    read(store, registry, 'one')
    view = status(store)
    assert view['facts']['shared_title']['value'] is False
    assert view['facts']['shared_title']['grade'] == 'rapor' and view['facts']['shared_title']['source_ref'].startswith('absence:')
    assert 'ownership_not_officially_verified' not in view['decision']['conditions']
    assert 'shared_title' not in view['to_verify_officially']      # accepted rule; no official source shows shares


@pytest.mark.parametrize('words', ['Hisseli değildir, müstakil.', 'Paylı mülkiyet', 'müşterek tapu', 'ortak tapulu'])
def test_any_hisse_wording_stops_the_assumption_and_leaves_it_to_the_reader(tmp_path, words):
    store, registry = python_listing_store(tmp_path, [clean(description=f'Ada 123 parsel 45. {words}')])
    read(store, registry, 'one')
    view = status(store)
    assert 'shared_title' not in view['facts']
    assert 'ownership_not_officially_verified' in view['decision']['conditions']


def test_an_explicit_statement_outranks_the_assumption(tmp_path):
    store, registry = python_listing_store(tmp_path, [clean(), {'listing_id': ID, 'messages': [seller('Tapu hisselidir, satılık hisse.')]}],
                                           tools=('sahibinden.read_listing', 'sahibinden.read_thread'))
    read(store, registry, 'listing'); read(store, registry, 'thread', tool='sahibinden.read_thread', start=10)
    assert status(store)['decision']['state'] == 'research_required'
    thread_obs = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_thread'")[0]['id']
    with store.transaction() as con:
        result = store.facts.submit(con, Proposal(listing_ref=ID, field='shared_title', value=True, proposer='llm:manager', observation_id=thread_obs,
                                                  source_ref='message:m2', quote='Tapu hisselidir'), now=20)
        store.facts.assess(con, ID, 'local', 21)
    assert result['status'] == 'accepted'
    view = status(store)
    assert view['decision']['state'] == 'excluded' and view['facts']['shared_title']['grade'] == 'beyan'
    assert not view['contradictions']                              # an assumption is not a statement to contradict


def test_two_parts_of_one_listing_that_disagree_are_a_conflict_not_a_new_version(tmp_path):
    payload = listing_payload(description='Parsel müstakil görünse de satılık hisse vardır.')    # row says Müstakil Parsel
    store, registry = python_listing_store(tmp_path, [payload])
    read(store, registry, 'one')
    obs_id = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_listing'")[0]['id']
    with store.transaction() as con:
        held = store.facts.submit(con, Proposal(listing_ref=ID, field='shared_title', value=True, proposer='llm:manager', observation_id=obs_id,
                                                source_ref='description', quote='satılık hisse vardır'), now=20)
        store.facts.assess(con, ID, 'local', 21)
    assert held['status'] == 'accepted'
    view = status(store)
    assert view['conflicts'] == ['shared_title'] and 'shared_title' not in view['facts']
    assert view['next_action']['action'] == 'human_review'


# ---- whole-page text and the reading agent's proposals -----------------------------------------
def test_a_quote_found_only_in_the_visible_page_text_is_accepted_and_the_receipt_always_fits():
    page = listing_payload(page_text='Ana sayfa > Arsa. İlan açıklaması dışında: yola 20 metre mesafede.')
    verdict = verify(propose('access', 'direct', quote='yola 20 metre mesafede', source_ref='page_text'), obs(payload=page))
    assert verdict.status == 'accepted'
    big = Listing.model_validate({**listing_payload(description='d' * 13000), 'page_text': 'p' * 8000})
    assert len(big.description) + len(big.page_text or '') <= 14000


def test_the_agents_proposal_tool_labels_the_proposer_and_hits_the_same_verifier(tmp_path):
    config = AgencyConfig.load()
    store = AgencyStore(tmp_path / 'agency.sqlite', config, supervisor_agent=None); store.initialize()
    registry = default_registry(config, store)
    store, py_registry = python_listing_store(tmp_path / 'b', [listing_payload(page_text='Sit yoktur, yola cepheli.')])
    read(store, py_registry, 'one')
    obs_id = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_listing'")[0]['id']
    task_id = table(store, 'SELECT id FROM inbox')[0]['id']
    registry = default_registry(store.config, store)
    args = {'listing_id': ID, 'observation_id': obs_id, 'field': 'access', 'value': 'direct', 'source_ref': 'page_text', 'quote': 'yola cepheli'}
    ok = registry.call('facts.propose', args, context={'mode': 'local', 'task_id': task_id})
    assert (ok['status'], ok['grade']) == ('accepted', 'beyan')
    assert table(store, "SELECT proposer FROM claims WHERE field='access'") == [{'proposer': 'llm:sahibinden'}]
    bad = registry.call('facts.propose', {**args, 'field': 'natural_sit', 'value': 'none', 'quote': 'tamamen uydurma bir cümle'}, context={'mode': 'local', 'task_id': task_id})
    assert (bad['status'], bad['reason']) == ('rejected', 'quote_not_in_source')
    assert status(store)['facts']['access']['proposer'] == 'llm:sahibinden' and 'natural_sit' not in status(store)['facts']


# ---- official parcel record (TKGM) ------------------------------------------------------------------
import urllib.error

from verda.agency.registry import ResourceBlocked, TransientToolError
from verda.agency.tkgm import TkgmClient, clean_neighborhood, validate_feature


def admin(rows):
    return {'type': 'FeatureCollection', 'features': [{'properties': {'text': t, 'id': i}} for t, i in rows]}


def parcel_feature(area='1510.40', ada='123', parsel='45', name='Kızlan', ring=None):
    ring = ring or [[27.5, 36.7], [27.6, 36.7], [27.6, 36.8], [27.5, 36.8], [27.5, 36.7]]
    return {'type': 'Feature', 'properties': {'mahalleAd': name, 'mahalleId': 100, 'adaNo': ada, 'parselNo': parsel, 'alan': area,
                                              'nitelik': 'Tarla', 'durum': 'Aktif', 'ilceAd': 'Datça'},
            'geometry': {'type': 'Polygon', 'coordinates': [ring]}}


def fake_net(feature=None, calls=None, raises=None):
    def fetch(url):
        (calls if calls is not None else []).append(url)
        if raises:
            raise raises
        if url.endswith('/idariYapi/ilListe'):
            return admin([('Muğla', 48), ('Mersin', 33)])
        if url.endswith('/ilceListe/48'):
            return admin([('Datça', 7)])
        if url.endswith('/mahalleListe/7'):
            return admin([('Kızlan', 100), ('Kızlan Köyü', 101)])
        return feature if feature is not None else parcel_feature()
    return fetch


def client(tmp_path, **kw):
    tmp_path.mkdir(exist_ok=True)
    return TkgmClient(tmp_path / 'admin.json', sleep=lambda s: None, **kw)


def test_the_official_record_is_resolved_with_exact_names_and_the_neighborhood_is_cached(tmp_path):
    calls = []
    c = client(tmp_path, fetch=fake_net(calls=calls))
    got = c.lookup({'parcel_key': '123/45', 'neighborhood': 'Kızlan Mh.'})
    assert got['found'] and got['official_area_m2'] == 1510.4 and got['property_type'] == 'Tarla' and got['neighborhood_id'] == 100
    assert got['parcel_url'].endswith('#ara/idari/100/123/45') and got['geometry']['ring_points'] == 5
    assert len(calls) == 4                                   # 3 administrative lookups + the parcel
    c.lookup({'parcel_key': '123/45', 'neighborhood': 'kizlan'})
    assert len(calls) == 5                                   # the second lookup only asks for the parcel


def test_a_wrong_or_malformed_answer_is_never_accepted():
    with pytest.raises(ValueError, match='identity'):
        validate_feature(parcel_feature(parsel='46'), 'Kızlan', 100, '123', '45')
    with pytest.raises(ValueError, match='closed Polygon'):
        validate_feature(parcel_feature(ring=[[0, 0], [1, 0], [1, 1], [0, 1]]), 'Kızlan', 100, '123', '45')
    with pytest.raises(ValueError, match='not positive|not a number'):
        validate_feature(parcel_feature(area='0'), 'Kızlan', 100, '123', '45')
    assert clean_neighborhood('Köyler\nYaka Mh.') == 'Yaka' and clean_neighborhood('Yaka Mahallesi') == 'Yaka'


def test_rate_limits_stop_the_source_missing_parcels_are_reported_and_network_errors_are_transient(tmp_path):
    def http(code):
        return urllib.error.HTTPError('u', code, 'x', {}, None)
    with pytest.raises(ResourceBlocked):
        client(tmp_path, fetch=fake_net(raises=http(429))).lookup({'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    with pytest.raises(TransientToolError):
        client(tmp_path / 'b', fetch=fake_net(raises=urllib.error.URLError('down'))).lookup({'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    c = client(tmp_path / 'c', fetch=lambda url: (_ for _ in ()).throw(http(404)) if '/parsel/' in url else fake_net()(url))
    assert c.lookup({'parcel_key': '123/45', 'neighborhood': 'Kızlan'})['found'] is False
    with pytest.raises(ValueError, match='exact'):
        client(tmp_path, fetch=fake_net()).lookup({'parcel_key': '123/45', 'neighborhood': 'Kızl'})


def test_a_matching_official_record_settles_area_and_scope_and_shows_type_and_links(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'x', fetch=fake_net()).lookup})
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    view = status(store)
    assert view['facts']['sale_area_m2']['grade'] == 'resmi' and view['facts']['parcel_key']['grade'] == 'resmi'
    assert view['facts']['sale_scope_resolved']['value'] is True
    assert 'sale_area' not in view['decision']['reasons']
    assert view['official_parcel'] == {'found': True, 'type': 'Tarla', 'area_m2': 1510.4, 'neighborhood': 'Kızlan', 'status': 'Aktif'}
    assert view['links'] == {'sahibinden': f'https://www.sahibinden.com/{ID}', 'tkgm': 'https://parselsorgu.tkgm.gov.tr/#ara/idari/100/123/45'}
    assert 'sale_area_m2' not in view['to_verify_officially'] and not view['contradictions']   # 1.510 vs 1510,40: same parcel
    assert view['next_action']['action'] == 'tkgm.corridor' and 'sahibinden.read_thread' in view['next_action']['also']


def test_an_official_area_that_differs_from_the_listing_wins_and_the_listing_is_marked_wrong(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'y', fetch=fake_net(parcel_feature(area='900.00'))).lookup})
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    view = status(store)
    assert view['facts']['sale_area_m2']['value'] == 900.0
    assert [(e['field'], e['other']['value'], e['chosen']['value']) for e in view['listing_errors']] == [('sale_area_m2', 1510.0, 900.0)]
    assert not view['contradictions']
    # 40 % apart is not the same land (two title deeds, a wrong parcel number, a share): a person decides, nothing is excluded on it
    assert 'sale_scope_resolved' not in view['facts'] and view['conflicts'] == ['sale_scope']
    assert view['decision']['state'] == 'research_required' and view['next_action']['action'] == 'human_review'


@pytest.mark.parametrize('raw,expected', [('1.053,41', 1053.41), ('1,053.41', 1053.41), ('12.588,73', 12588.73), ('900', 900.0), ('12.345', 12345.0), ('1,053', 1053.0), ('1510.40', 1510.4), ('5,5', 5.5)])
def test_official_areas_are_read_in_turkish_number_format(raw, expected):
    from verda.agency.tkgm import parse_area
    assert parse_area(raw) == expected
    with pytest.raises(ValueError):
        parse_area('1.0,5,5')


def test_the_advertised_property_type_is_a_statement_that_the_official_record_confirms_or_contradicts(tmp_path):
    attrs = {'tapu durumu': 'Müstakil Tapulu', 'imar durumu': 'Tarla'}
    store, registry = python_listing_store(tmp_path, [listing_payload(attributes=attrs)], extra={
        'tkgm.lookup': client(tmp_path / 'z', fetch=fake_net(parcel_feature(area='1510.40'))).lookup})
    read(store, registry, 'listing')
    assert status(store)['facts']['property_type'] == {**status(store)['facts']['property_type'], 'value': 'Tarla', 'grade': 'beyan'}
    assert 'property_type' in status(store)['to_verify_officially']
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    view = status(store)                                   # the fake record says "Tarla": confirmed
    assert view['facts']['property_type']['grade'] == 'resmi' and 'property_type' not in view['to_verify_officially'] and not view['contradictions']


def test_a_different_official_property_type_is_a_visible_contradiction(tmp_path):
    attrs = {'tapu durumu': 'Müstakil Tapulu', 'imar durumu': 'Zeytinlik'}
    store, registry = python_listing_store(tmp_path, [listing_payload(attributes=attrs)], extra={
        'tkgm.lookup': client(tmp_path / 'z', fetch=fake_net()).lookup})        # official: Tarla
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    view = status(store)
    assert view['facts']['property_type']['value'] == 'Tarla'
    assert [(e['field'], e['other']['value']) for e in view['listing_errors']] == [('property_type', 'Zeytinlik')] and not view['contradictions']


# ---- routing ------------------------------------------------------------------------------------------
from verda.agency import geo

ORIGIN = {'lat': 36.69971, 'lon': 27.505889}
DEST = {'lat': 36.676853, 'lon': 27.512604, 'name': 'Mertur'}


def osrm_ok(minutes=14.9, snap=88):
    return {'code': 'Ok', 'routes': [{'duration': minutes * 60, 'distance': 5540}], 'waypoints': [{'distance': snap}, {'distance': 5}]}


def valhalla_ok(minutes=19.4):
    return {'trip': {'summary': {'time': minutes * 60, 'length': 5.2}}}


def routes_fetch(calls, valhalla_fails_at_origin=False):
    def fetch(url):
        calls.append(url)
        if '/nearest/' in url:
            return {'code': 'Ok', 'waypoints': [{'location': [27.5062, 36.6992], 'distance': 140.4}]}
        if 'valhalla' in url:
            if valhalla_fails_at_origin and '27.505889' in urllib.parse.unquote(url):
                raise urllib.error.HTTPError(url, 400, 'no suitable edges near location', {}, None)
            return valhalla_ok()
        return osrm_ok()
    return fetch


import urllib.parse


def test_two_engines_are_both_recorded_and_the_start_is_the_parcel_centre():
    got = geo.route(ORIGIN, DEST, routes_fetch([]), sleep=lambda s: None)
    assert [(e['engine'], e['minutes'], e['ok']) for e in got['engines']] == [('osrm', 14.9, True), ('valhalla', 19.4, True)]
    assert got['engines'][0]['origin_snap_m'] == 88 and 2.5 < got['straight_line_km'] < 2.7 and 'Trafik içermez' in got['note']


def test_when_no_road_is_near_the_start_the_route_is_drawn_from_the_nearest_road_and_says_so():
    calls = []
    got = geo.route(ORIGIN, DEST, routes_fetch(calls, valhalla_fails_at_origin=True), sleep=lambda s: None)
    valhalla = next(e for e in got['engines'] if e['engine'] == 'valhalla')
    assert valhalla['ok'] and valhalla['fallback'] == 'nearest_road' and valhalla['origin_snap_m'] == 140
    assert sum('/nearest/' in u for u in calls) == 1                      # asked once, shared by both engines
    assert 'fallback' not in next(e for e in got['engines'] if e['engine'] == 'osrm')


def test_if_there_is_no_road_anywhere_near_the_result_is_reported_not_invented():
    def fetch(url):
        if '/nearest/' in url:
            raise urllib.error.URLError('none')
        raise urllib.error.HTTPError(url, 400, 'NoSegment', {}, None)
    got = geo.route(ORIGIN, DEST, fetch, sleep=lambda s: None)
    assert all(e['error'] == 'no_road_near_origin' for e in got['engines']) and 'elle inceleme' in got['note']


def test_a_rate_limit_stops_the_shared_source_and_one_failed_engine_does_not_hide_the_other():
    def limited(url):
        raise urllib.error.HTTPError(url, 429, 'slow down', {}, None)
    with pytest.raises(ResourceBlocked):
        geo.route(ORIGIN, DEST, limited, sleep=lambda s: None)
    def half(url):
        if 'valhalla' in url:
            raise urllib.error.URLError('down')
        return osrm_ok()
    got = geo.route(ORIGIN, DEST, half, sleep=lambda s: None)
    assert [e['ok'] for e in got['engines']] == [True, False]


def test_the_route_comes_from_the_official_parcel_not_from_where_a_seller_placed_the_pin(tmp_path):
    seen = []
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={
        'tkgm.lookup': client(tmp_path / 'g', fetch=fake_net()).lookup,
        'geo.route': lambda args: geo.listing_route(store, args, mode='local', fetch=lambda u: (seen.append(u), routes_fetch([])(u))[1], sleep=lambda s: None)})
    read(store, registry, 'listing')
    first = geo.listing_route(store, {'listing_id': ID}, mode='local', fetch=routes_fetch([]), sleep=lambda s: None)
    assert first['reason'] == 'official_parcel_required'                  # no official parcel yet: nothing is guessed
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    read(store, registry, 'route', tool='geo.route', agent='research', start=20)
    view = status(store)
    assert {r['value'] for r in view['facts']['route_minutes']} == {14.9, 19.4}
    assert view['facts']['destination_verified']['proposer'] == 'policy' and 'route' not in view['decision']['reasons']
    assert seen and all('27.54,36.74' in u for u in seen if 'router.project-osrm.org/route' in u)   # the start is the official centroid


# ---- photos: the private evidence store ------------------------------------------------------------------
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


def test_images_are_identified_by_their_bytes_and_stored_once(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    obs_id = table(store, "SELECT id FROM observations")[0]['id']
    with store.transaction() as con:
        a = store.assets.save(con, listing_ref=ID, mode='local', observation_id=obs_id, source_url='https://i0.shbdn.com/a.jpg', position=0, data=JPEG)
        b = store.assets.save(con, listing_ref=ID, mode='local', observation_id=obs_id, source_url='https://i0.shbdn.com/a-again.jpg', position=1, data=JPEG)
        store.assets.save(con, listing_ref=ID, mode='local', observation_id=obs_id, source_url='https://i0.shbdn.com/b.png', position=2, data=PNG)
        for bad in (b'<html>not an image</html>', b'', b'\xff\xd8\xff' + b'0' * 7_000_000):
            with pytest.raises(ValueError):
                store.assets.save(con, listing_ref=ID, mode='local', observation_id=obs_id, source_url='x', position=3, data=bad)
    assert a['id'] == b['id'] and a['new'] and not b['new'] and a['mime'] == 'image/jpeg'
    with store.transaction() as con:
        assert [x['mime'] for x in store.assets.listing(con, ID, 'local')] == ['image/jpeg', 'image/png']
        mime, path = store.assets.read(con, a['id'])
        assert mime == 'image/jpeg' and path.read_bytes() == JPEG and oct(path.stat().st_mode & 0o777) == '0o600'
        assert store.assets.read(con, 'f' * 64) is None
        with pytest.raises(sqlite3.IntegrityError, match='append_only'):
            con.execute('DELETE FROM evidence_assets')


def test_v8_databases_gain_the_asset_table_without_losing_work(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        con.execute('DROP TABLE evidence_assets'); con.execute('UPDATE agency_meta SET version=8')
    before = table(store, 'SELECT id FROM claims')
    store.initialize()
    assert table(store, 'SELECT version FROM agency_meta') == [{'version': 9}] and table(store, 'SELECT id FROM claims') == before
    assert table(store, 'SELECT id FROM evidence_assets') == []


# ---- the listing file in the local panel ------------------------------------------------------------------
from fastapi.testclient import TestClient
from verda.api import create_app


def test_the_listing_file_and_its_photos_need_the_local_viewer_and_serve_only_real_images(engine, tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload(photos=['https://i0.shbdn.com/photos/1.jpg'])])
    read(store, registry, 'one')
    obs_id = table(store, "SELECT id FROM observations")[0]['id']
    with store.transaction() as con:
        asset = store.assets.save(con, listing_ref=ID, mode='local', observation_id=obs_id, source_url='https://i0.shbdn.com/photos/1.jpg', position=0, data=JPEG)
    with TestClient(create_app(engine, 'token-of-at-least-thirty-two-characters', viewer_nonce='open', agency_store=store)) as client:
        assert client.get(f'/control/private/listing/{ID}').status_code == 401
        assert client.get(f"/control/private/assets/{asset['id']}").status_code == 401
        assert client.get(f'/control/listing/{ID}').status_code == 200 and client.get('/control/listing/not-a-number').status_code == 404
        client.get('/control/unlock/open')
        data = client.get(f'/control/private/listing/{ID}').json()
        assert data['page']['description'].startswith('Ada 123 parsel 45') and data['page']['photos_listed'] == 1
        assert data['assets'] == [{'id': asset['id'], 'mime': 'image/jpeg', 'bytes': len(JPEG)}]
        assert data['status']['decision']['state'] == 'research_required' and data['status']['links']['sahibinden'].endswith(ID)
        image = client.get(f"/control/private/assets/{asset['id']}")
        assert image.status_code == 200 and image.headers['content-type'] == 'image/jpeg' and image.content == JPEG
        assert client.get('/control/private/assets/' + 'f' * 64).status_code == 404
        assert client.get('/control/private/assets/../../etc/passwd').status_code in {404, 422}
        assert client.get('/control/private/listing/abc').status_code == 404


def test_photo_addresses_must_be_on_the_sites_own_cdn_and_only_new_ones_are_downloaded(tmp_path):
    from verda.agency.photos import allowed, save_listing_photos
    assert allowed('https://i0.shbdn.com/photos/1.jpg') and allowed('https://www.sahibinden.com/x.jpg')
    for bad in ('http://i0.shbdn.com/x.jpg', 'https://evil.example/x.jpg', 'https://user@i0.shbdn.com/x.jpg', 'https://shbdn.com.evil.example/x.jpg', 'https://i0.shbdn.com:8443/x.jpg'):
        assert not allowed(bad)
    with pytest.raises(ValueError):
        Listing.model_validate({**{k: v for k, v in listing_payload().items() if k != 'attributes'}, 'photos': ['https://evil.example/x.jpg']})
    urls = ['https://i0.shbdn.com/photos/1.jpg', 'https://i0.shbdn.com/photos/2.png', 'https://i0.shbdn.com/photos/3.jpg']
    store, registry = python_listing_store(tmp_path, [listing_payload(photos=urls)])
    read(store, registry, 'one')
    fetched = []
    def fetch(url):
        fetched.append(url)
        if url.endswith('2.png'):
            return b'<html>login wall</html>'
        return JPEG if url.endswith('1.jpg') else JPEG + b'1'
    first = save_listing_photos(store, {'listing_id': ID}, mode='local', fetch=fetch, sleep=lambda s: None)
    assert first['saved'] == 2 and first['failed'] == [{'position': 1, 'error': 'not_an_image'}]
    again = save_listing_photos(store, {'listing_id': ID}, mode='local', fetch=fetch, sleep=lambda s: None)
    assert again['already'] == 2 and len(fetched) == 3 + 1                  # only the failed one is retried
    assert save_listing_photos(store, {'listing_id': '9999999999'}, mode='local', fetch=fetch)['reason'] == 'listing_not_read'
    def limited(url):
        raise urllib.error.HTTPError(url, 429, 'slow', {}, None)
    with pytest.raises(ResourceBlocked):
        save_listing_photos(store, {'listing_id': ID}, mode='local', fetch=limited, sleep=lambda s: None)


# ---- cadastral road frontage from official geometry ---------------------------------------------------
from verda.agency import corridor as cor

LON0, LAT0 = 27.5, 36.7
PLANE = cor.Plane(LON0, LAT0)


def poly(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def inside(pt, ring):
    x, y = pt; hit = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            hit = not hit
    return hit


def world(parcels, calls=None, fail_with=None):
    """parcels: {'ada/parsel': ring in metres around (LON0, LAT0)}; anything else is an empty gap (404)."""
    import re
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if fail_with:
            raise urllib.error.HTTPError(url, fail_with, 'x', {}, None)
        lat, lon = map(float, re.search(r'/parsel/([-\d.]+)/([-\d.]+)/', url).groups())
        pt = PLANE.to_xy(lon, lat)
        for key, ring in parcels.items():
            if inside(pt, ring):
                ada, parsel = key.split('/')
                return {'type': 'Feature', 'properties': {'adaNo': ada, 'parselNo': parsel, 'mahalleId': 100, 'mahalleAd': 'Kızlan'},
                        'geometry': {'type': 'Polygon', 'coordinates': [[list(PLANE.to_lonlat(*p)) for p in ring]]}}
        raise urllib.error.HTTPError(url, 404, 'no parcel', {}, None)
    return fetch


SUBJECT_RING = poly(0, 0, 30, 30)
SUBJECT = {'parcel_key': '1/1', 'neighborhood_id': 100, 'ring': [list(PLANE.to_lonlat(*p)) for p in SUBJECT_RING]}
NEIGHBORS = {'1/2': poly(-40, -2.5 - 40, -0.0, 70), 'x': None}
NEIGHBORS = {'1/2': poly(-40, -5, 0, 70), '1/3': poly(30, -5, 70, 70), '1/4': poly(0, 30, 30, 70)}


def test_a_gap_on_one_side_with_a_numbered_parcel_across_it_is_a_corridor_candidate():
    parcels = {'1/1': SUBJECT_RING, **NEIGHBORS, '1/9': poly(-40, -60, 70, -12)}      # road strip y in (-12, 0) between 1/1 and 1/9
    got = cor.analyse(SUBJECT, world(parcels), sleep=lambda s: None)
    assert got['verdict'] == 'corridor_candidate' and got['corridor']['other_bank'] == ['1/9'] and got['corridor']['edges'] == [0]
    south = next(e for e in got['edges'] if e['edge'] == 0)
    assert south['results'] == ['gap', 'gap', 'gap'] and south['other_bank']['within_m'] == 15
    neighbors = {r['parcel'] for r in got['probes'] if r['result'] == 'neighbor'}
    assert {'1/2', '1/3', '1/4'} <= neighbors and all(r['shared_m'] >= 1 for r in got['probes'] if r['result'] == 'neighbor')
    assert 'yolun manuel değerlendirilmesi tavsiye edilir' in got['note'].lower()


def test_every_side_a_numbered_neighbor_is_no_frontage_and_never_a_road_or_a_missing_road():
    parcels = {'1/1': SUBJECT_RING, '1/2': poly(-40, 0, 0, 30), '1/3': poly(30, 0, 70, 30), '1/4': poly(0, 30, 30, 70), '1/5': poly(0, -40, 30, 0)}
    got = cor.analyse(SUBJECT, world(parcels), sleep=lambda s: None)
    assert got['verdict'] == 'all_edges_numbered_neighbors' and got['corridor'] is None


def test_a_gap_with_nothing_on_the_far_bank_or_a_failed_answer_stays_inconclusive_not_a_road():
    parcels = {'1/1': SUBJECT_RING, **NEIGHBORS}                                      # open gap to the south, nothing beyond
    got = cor.analyse(SUBJECT, world(parcels), sleep=lambda s: None)
    assert got['verdict'] == 'inconclusive' and got['corridor'] is None and 'yol yok' in got['note']
    broken = cor.analyse(SUBJECT, lambda url: (_ for _ in ()).throw(urllib.error.URLError('down')), sleep=lambda s: None)
    assert broken['verdict'] == 'inconclusive' and broken['failed_probes'] == len(broken['probes'])


def test_probes_are_sparse_capped_and_a_rate_limit_stops_the_source():
    calls = []
    cor.analyse(SUBJECT, world({'1/1': SUBJECT_RING}, calls), sleep=lambda s: None)
    assert len(calls) <= cor.MAX_REQUESTS
    capped = cor.analyse(SUBJECT, world({'1/1': SUBJECT_RING}), sleep=lambda s: None, max_requests=3)
    assert capped['requests'] == 3 and capped['verdict'] == 'inconclusive'
    with pytest.raises(ResourceBlocked):
        cor.analyse(SUBJECT, world({}, fail_with=429), sleep=lambda s: None)
    assert cor.shared_length((0, 0), (30, 0), poly(-5, -20, 40, 0)) == pytest.approx(30, abs=1.5)
    assert cor.shared_length((0, 0), (30, 0), poly(-5, -20, 40, -3)) == 0                  # 3 m away is not touching


def test_probes_of_one_call_are_spaced_and_a_struggling_source_is_slowed_then_left_alone():
    waits, calls = [], []
    got = cor.analyse(SUBJECT, world({'1/1': SUBJECT_RING}, calls), sleep=waits.append)
    assert cor.PROBE_DELAY >= 2.5 and len(waits) == len(calls) - 1 and set(waits) == {cor.PROBE_DELAY}      # a steady, slow pace; the tool wait only spaces whole calls
    assert 'stopped' not in got
    waits, calls = [], []
    down = cor.analyse(SUBJECT, world({}, calls, fail_with=503), sleep=waits.append)
    assert len(calls) == cor.MAX_FAILURES_IN_A_ROW and waits == [cor.PROBE_DELAY * 2, cor.PROBE_DELAY * 4]   # each failure in a row doubles the wait, then no more requests
    assert down['stopped'] == 'repeated_failures' and down['verdict'] == 'inconclusive' and down['requests'] == 3 and down['corridor'] is None
    assert {p['reason'] for p in down['probes'] if p['result'] == 'failed'} == {'http_503', 'stopped_after_failures'}


def test_one_good_answer_after_a_failure_brings_the_pace_back():
    waits, answers = [], iter([503, 404, 404, 404, 404])
    def flaky(url):
        code = next(answers, 404)
        raise urllib.error.HTTPError(url, code, 'x', {}, None)
    got = cor.analyse(SUBJECT, flaky, sleep=waits.append)
    assert waits[:3] == [cor.PROBE_DELAY * 2, cor.PROBE_DELAY, cor.PROBE_DELAY] and 'stopped' not in got


def test_the_back_off_has_a_ceiling_and_never_goes_below_the_base_pace():
    for delay, expected in ((20, [30.0, 30.0]), (60, [60.0, 60.0])):
        waits = []
        cor.analyse(SUBJECT, world({}, fail_with=503), sleep=waits.append, delay=delay)
        assert waits == expected


def test_the_corridor_tool_waits_between_calls_so_a_batch_is_not_one_long_burst():
    tools = {t.key: t for t in AgencyConfig.load(None).tools}
    assert tools['tkgm.corridor'].min_interval_seconds >= 30 and tools['tkgm.corridor'].resource == tools['tkgm.lookup'].resource == 'tkgm'


def test_a_corridor_candidate_becomes_a_conditional_access_fact_and_the_agent_asks_nobody_first(tmp_path):
    parcels = {'1/1': SUBJECT_RING, **NEIGHBORS, '1/9': poly(-40, -60, 70, -12)}
    store, registry = python_listing_store(tmp_path, [listing_payload(parcel_key='1/1', attributes={'ada no': '1', 'parsel no': '1'})], extra={
        'tkgm.lookup': client(tmp_path / 'c1', fetch=fake_net(parcel_feature(ada='1', parsel='1', ring=[list(PLANE.to_lonlat(*p)) for p in SUBJECT_RING]))).lookup,
        'tkgm.corridor': lambda args: cor.listing_corridor(store, args, mode='local', fetch=world(parcels), sleep=lambda s: None)})
    read(store, registry, 'listing')
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '1/1', 'neighborhood': 'Kızlan'})
    assert status(store)['next_action']['action'] == 'tkgm.corridor'
    read(store, registry, 'corridor', tool='tkgm.corridor', agent='parcel', start=20)
    view = status(store)
    assert view['facts']['access']['value'] == 'corridor_conditional' and view['facts']['access']['grade'] == 'bagimsiz'
    assert 'access' not in view['decision']['reasons'] and 'access' in view['to_verify_officially']
    assert 'ask:access' not in json.dumps(view['next_action'])
    # a parcel without stored geometry is never guessed at
    assert cor.listing_corridor(store, {'listing_id': '9999999999'}, mode='local')['reason'] == 'official_parcel_with_geometry_required'


def test_a_legacy_excluded_listing_has_an_excluded_decision_whatever_the_new_records_say(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    with store.transaction() as con:
        fresh = store.facts.status(con, ID, 'local')
        view = store.facts.status(con, ID, 'local', legacy_lifecycle='excluded')
    assert fresh['decision']['state'] != 'excluded'                                  # the new records alone would keep it a candidate
    assert view['decision']['state'] == 'excluded' and view['decision']['reasons'][0] == 'legacy_excluded'
    assert view['facts'] and view['next_action'] == {'action': 'none', 'why': 'legacy_excluded'}


def test_a_plain_no_natural_sit_means_none_and_a_wrong_enum_value_names_the_allowed_ones():
    from verda.agency.research import normalise_value, vocabulary_hint
    assert normalise_value('natural_sit', False) == 'none' and normalise_value('natural_sit', True) is True
    assert normalise_value('archaeological_sit', False) is False
    assert 'none' in vocabulary_hint('natural_sit') and vocabulary_hint('archaeological_sit') is None


def test_a_quote_about_the_other_sit_type_goes_to_review_and_an_ordinary_doğal_is_not_a_sit_claim():
    held = verify(propose('archaeological_sit', False, quote='doğal sit yoktur', source_ref='message:m2'), thread(seller('Doğal sit yoktur.')))
    assert (held.status, held.reason) == ('needs_review', 'sit_type_unclear')
    held = verify(propose('natural_sit', 'none', quote='arkeolojik sit yoktur', source_ref='message:m2'), thread(seller('Arkeolojik sit yoktur.')))
    assert (held.status, held.reason) == ('needs_review', 'sit_type_unclear')
    text = 'Arazi doğal haliyle zeytinlik vasıflı bir tarladır. Herhangi bir sit yoktur.'
    ok = verify(propose('archaeological_sit', False, quote='Herhangi bir sit yoktur', source_ref='message:m2'), thread(seller(text)))
    assert ok.status == 'accepted'                                   # "doğal konumunda" is an adjective, not a natural-sit claim


def test_an_official_parcel_far_smaller_or_larger_than_the_advertised_land_does_not_settle_what_is_on_sale():
    from verda.policy import evaluate
    from verda.facts.service import FactService
    def case(listing_area, official_area):
        return assemble([claim('price_tl', 12e6, 'beyan', seq=1), claim('sale_area_m2', listing_area, 'beyan', seq=2),
                         claim('sale_area_m2', official_area, 'resmi', seq=3, kind='official'), claim('sale_scope_resolved', True, 'resmi', seq=4, kind='official')])
    # 1252932673: "2 adet tapu: 2582 m2 + 678 m2"; only the 678 m2 parcel was looked up. It was excluded as below the minimum area.
    two_deeds = case(3260.0, 678.02)
    assert 'sale_scope_resolved' not in two_deeds.values and 'sale_scope' in two_deeds.conflicts
    decision = evaluate(FactService.screening(two_deeds))
    assert decision.state == 'research_required' and 'area_below_minimum' not in decision.reasons
    # a survey update of a few per cent is the same parcel: the official area stands and the rules apply to it
    same = case(1902.0, 1704.72)
    assert same.values['sale_scope_resolved'] is True and not same.conflicts
    small = case(990.0, 950.0)
    assert evaluate(FactService.screening(small)).reasons == ['area_below_minimum']


def test_a_parcel_outside_any_block_is_ada_0_and_can_be_looked_up_officially(tmp_path):
    # 1310268248, seller: "Ada:0 Parsel:1238"; TKGM answers /parsel/<Karaköy>/0/1238 with an empty ada and 1.463,18 m2 (checked 14 Oct 2026)
    from verda.agency.tkgm import split_parcel
    from verda.agency.corridor import identity
    from verda.facts.extract import parcel_from_listing
    from verda.facts.model import PARCEL
    assert split_parcel('0/1238') == ('0', '1238') and PARCEL.match('0/1238') and not PARCEL.match('0/0') and not PARCEL.match('01/5')
    assert parcel_from_listing({'attributes': {'ada no': '0', 'parsel no': '1238'}}) == '0/1238'
    result = client(tmp_path / 'ada0', fetch=fake_net(parcel_feature(area='1.463,18', ada='', parsel='1238'))).lookup({'parcel_key': '0/1238', 'neighborhood': 'Kızlan'})
    assert result['found'] and result['official_area_m2'] == 1463.18 and result['parcel_url'].endswith('/0/1238')
    assert identity(parcel_feature(ada='', parsel='1238')) == ('0', '1238', '100')      # the corridor knows the parcel itself, not a neighbour


def test_the_agent_sees_the_observation_id_next_to_a_tool_output_so_it_can_record_what_it_read(tmp_path):
    # Eight inbox runs (Oct 2026) read seller answers and ended "observation_id gerekli": the id was never in what the model saw.
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'one')
    task = table(store, "SELECT * FROM inbox WHERE request_key='one'")[0]
    finished = [e['data'] for e in store.context(task)['history'] if e['kind'] == 'tool_finished']
    recorded = table(store, "SELECT id FROM observations WHERE tool='sahibinden.read_listing'")[0]['id']
    assert finished and finished[-1]['observation_id'] == recorded


def bound(field, value, grade, *, seq, parcel=None, kind='official', key=None):
    return {**claim(field, value, grade, seq=seq, kind=kind, key=key), 'parcel': parcel}


def test_a_two_deed_sale_waits_for_its_second_parcel_and_then_counts_both_areas():
    # 1252932673: "2 adet tapu: 2582 + 678 m2"; the user found the second deed, 402/7, next to 402/5 on the TKGM map
    from verda.facts.action import next_action
    from verda.facts.service import FactService
    from verda.policy import evaluate
    base = [claim('price_tl', 12e6, 'beyan', seq=1), claim('sale_area_m2', 3260.0, 'beyan', seq=2), claim('parcel_key', '402/5', 'beyan', seq=3),
            bound('parcel_key', '402/5', 'resmi', seq=4, parcel='402/5'), bound('sale_area_m2', 678.02, 'resmi', seq=5, parcel='402/5'),
            bound('parcel_area_m2', 678.02, 'resmi', seq=6, parcel='402/5', key='402/5'), bound('sale_scope_resolved', True, 'resmi', seq=7, parcel='402/5'),
            claim('sale_parcels', ['402/5', '402/7'], 'kullanici', seq=8, kind='user')]
    waiting = assemble(base)
    assert waiting.parcels == ['402/5', '402/7'] and waiting.unchecked_parcels == ['402/7'] and not waiting.conflicts
    assert 'sale_area_m2' not in waiting.values                                  # one deed's area is not the sale's
    decision = evaluate(FactService.screening(waiting))
    assert decision.state == 'research_required' and 'area_below_minimum' not in decision.reasons
    step = next_action(decision, waiting, {'listing_read': True, 'tkgm_looked_up': True}, 0, None)
    assert step['action'] == 'tkgm.lookup' and step['parcel'] == '402/7' and step['part'] is True
    done = assemble(base + [bound('parcel_area_m2', 2582.0, 'resmi', seq=9, parcel='402/7', key='402/7')])
    assert done.values['sale_area_m2'] == 3260.02 and done.values['sale_scope_resolved'] is True and not done.conflicts and not done.unchecked_parcels


def test_the_users_parcel_sets_aside_every_record_about_the_wrong_parcel():
    # 1336424878: the listing named 894/3, a 222 m2 road parcel; the user read the real one, 881/14, off a photo of the deed
    from verda.facts.action import next_action
    from verda.facts.service import FactService
    from verda.policy import evaluate
    got = assemble([claim('price_tl', 15e6, 'beyan', seq=1), claim('sale_area_m2', 4540.0, 'beyan', seq=2), claim('parcel_key', '894/3', 'beyan', seq=3),
                    bound('parcel_key', '894/3', 'resmi', seq=4, parcel='894/3'), bound('sale_area_m2', 221.84, 'resmi', seq=5, parcel='894/3'),
                    bound('property_type', 'Yol', 'resmi', seq=6, parcel='894/3'), bound('sale_scope_resolved', True, 'resmi', seq=7, parcel='894/3'),
                    bound('natural_sit', 'first_degree', 'resmi', seq=8, parcel='894/3'),
                    claim('parcel_key', '881/14', 'kullanici', seq=9, kind='user')])
    assert got.values['parcel_key'] == '881/14' and got.values['sale_area_m2'] == 4540.0 and 'natural_sit' not in got.values
    assert got.unchecked_parcels == ['881/14'] and not got.conflicts
    decision = evaluate(FactService.screening(got))
    assert decision.state == 'research_required'                                 # neither excluded by the road parcel's sit nor its area
    step = next_action(decision, got, {'listing_read': True, 'tkgm_looked_up': True}, 0, None)
    assert step == {'action': 'tkgm.lookup', 'why': 'sale_parcel_unchecked', 'parcel': '881/14', 'part': False}


def test_the_users_no_eliminates_and_the_users_ask_sends_the_scope_question():
    from verda.agency import message as M
    from verda.facts.action import next_action
    from verda.facts.service import FactService
    from verda.policy import evaluate
    out = assemble([claim('price_tl', 5e6, 'beyan', seq=1), claim('user_excluded', True, 'kullanici', seq=2, kind='user')])
    assert evaluate(FactService.screening(out)).reasons == ['user_excluded']
    scope = assemble([claim('price_tl', 2.25e6, 'beyan', seq=1), claim('sale_area_m2', 1318.0, 'beyan', seq=2), claim('parcel_key', '115/57', 'beyan', seq=3),
                      bound('sale_area_m2', 2606.22, 'resmi', seq=4, parcel='115/57'), bound('sale_scope_resolved', True, 'resmi', seq=5, parcel='115/57'),
                      claim('scope_question', True, 'kullanici', seq=6, kind='user')])
    decision = evaluate(FactService.screening(scope))
    assert next_action(decision, scope, {'listing_read': True, 'thread_read': True, 'tkgm_looked_up': True}, 0, None)['action'] == 'ask:sale_scope'
    assert M.render('1216510607', ['sale_scope'], scope=(1318.0, 2606.22)) == \
        'Merhaba, 1216510607 numaralı taşınmaz için ilanda 1.318 m² yazıyor, tapu kaydında parsel 2.606 m² görünüyor; satılan parselin tamamı mı, bir kısmı mı, yoksa hisse mi? Teşekkürler.'


def test_a_further_deeds_lookup_records_only_its_own_area():
    from verda.facts.extract import extract
    payload = {'found': True, 'parcel_key': '402/7', 'official_area_m2': 2582.0, 'property_type': 'Tarla', 'part': True}
    assert [(p.field, p.key, p.value) for p in extract('tkgm.lookup', 'o1', 'local', payload, '1252932673')] == [('parcel_area_m2', '402/7', 2582.0)]
    main = extract('tkgm.lookup', 'o2', 'local', {**payload, 'parcel_key': '402/5', 'part': None}, '1252932673')
    assert {p.field for p in main} == {'parcel_area_m2', 'sale_area_m2', 'property_type', 'parcel_key', 'sale_scope_resolved'}


def test_ilgilenmiyorum_on_the_dashboard_is_the_users_elimination():
    from verda.publish.feedback import decisions
    assert decisions({'decision': 'excluded', 'updatedAt': 't1'}) == [('user_excluded', True, 'decision=excluded@t1', 'dashboard')]
    assert decisions({'decision': 'shortlist', 'updatedAt': 't2'}) == [('user_excluded', False, 'decision=shortlist@t2', 'dashboard')]
    assert decisions({'note': 'sadece not', 'updatedAt': 't3'}) == []


def test_a_stated_list_of_parcels_is_verified_like_any_statement_and_an_open_review_does_not_hold_an_official_lookup():
    from verda.facts.action import next_action
    text = 'Satılık arsa. *121 Ada 9 Parsel ve 121 Ada 6 Parsel* olmak üzere toplam iki parsel bulunmaktadır.'
    got = verify(propose('sale_parcels', ['121/9', '121/6'], quote='121 Ada 9 Parsel ve 121 Ada 6 Parsel* olmak üzere toplam iki parsel bulunmaktadır',
                         source_ref='description'), obs(payload=listing_payload(description=text)))
    assert got.status == 'accepted' and got.grade == 'beyan'
    out = assemble([claim('parcel_key', '881/14', 'kullanici', seq=1, kind='user'), bound('sale_area_m2', 221.84, 'resmi', seq=2, parcel='894/3')])
    assert next_action(None, out, {'listing_read': True, 'tkgm_looked_up': True}, 1, None)['action'] == 'tkgm.lookup'


@pytest.mark.parametrize('quote,field,status', [
    ('herhangi bir doğal ve alkolojik sit yoktur', 'archaeological_sit', 'accepted'),     # a seller's typo: the model read it right
    ('Herhangi bir arkeolojik ya da sitte değildir', 'natural_sit', 'accepted'),          # "neither archaeological nor any sit"
    ('Arkolojik sit alanı değil', 'archaeological_sit', 'accepted'),
    ('Doğal sit alanı değildir', 'archaeological_sit', 'needs_review'),                   # only the other kind is named
    ('Arkeolojik sit yoktur', 'natural_sit', 'needs_review'),
    ('Ekolojik açıdan sit alanı yoktur', 'archaeological_sit', 'accepted'),              # "ekolojik" is no kind of sit: a general sit
])
def test_the_model_reads_typos_and_general_answers_and_code_only_stops_an_answer_about_the_other_kind(quote, field, status):
    got = verify(propose(field, 'none' if field == 'natural_sit' else False, quote=quote, source_ref='message:m2'), thread(seller(quote + '.')))
    assert got.status == status


def test_a_later_accepted_value_closes_the_review_item_and_a_model_proposal_is_checked_again_under_new_rules(tmp_path):
    store, registry = python_listing_store(tmp_path, [listing_payload()])
    read(store, registry, 'listing')
    with store.transaction() as con:
        con.execute("INSERT INTO claims(id,listing_ref,mode,field,value,scope,source_kind,subject,proposer,status,grade,reason,verifier,created_at,dedupe_key) "
                    "VALUES('old','" + ID + "','local','archaeological_sit','false','listing','thread','this_parcel','llm:manager','needs_review',NULL,'sit_type_unclear','v1',1,'d-old')")
        assert [c['id'] for c in store.facts.review_items(con, ID, 'local')] == ['old']
        store.facts.decide(con, ID, 'local', 'archaeological_sit', False, 'satıcı yazım hatası', 2.0)
        assert store.facts.review_items(con, ID, 'local') == []


def test_a_parcel_tkgm_does_not_know_is_asked_once_and_then_goes_to_the_user(tmp_path):
    # 15 Oct 2026: three listings whose parcel TKGM answers "not found" were looked up 35 times each, every sweep
    import urllib.error
    def missing(url):
        if '/parsel/' in url:
            raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
        return fake_net(parcel_feature())(url)
    store, registry = python_listing_store(tmp_path, [listing_payload()], extra={'tkgm.lookup': client(tmp_path / 'nf', fetch=missing).lookup})
    read(store, registry, 'listing')
    assert status(store)['next_action']['action'] == 'tkgm.lookup'
    read(store, registry, 'tkgm', tool='tkgm.lookup', agent='parcel', start=10, inputs={'parcel_key': '123/45', 'neighborhood': 'Kızlan'})
    assert status(store)['next_action'] == {'action': 'human_review', 'why': 'official_parcel_not_found_or_unusable', 'also': status(store)['next_action'].get('also')}
