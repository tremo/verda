import json
from verda.agency.compact import batch_progress, compact_history, compact_output, fit_context


def test_a_search_page_shrinks_to_number_price_and_area_and_long_texts_are_cut():
    page = {'page': 2, 'total_reported': 295, 'has_more': True, 'source_url': 'x' * 400,
            'listings': [{'listing_id': str(1000000000 + i), 'title': 'T' * 200, 'url': 'u' * 300, 'price_tl': 1.0, 'area_m2': 2.0} for i in range(20)]}
    small = compact_output(page)
    assert set(small['listings'][0]) == {'listing_id', 'price_tl', 'area_m2'} and small['page'] == 2
    assert len(json.dumps(small)) < len(json.dumps(page)) / 4
    detail = compact_output({'listing_id': '1', 'neighborhood': 'Yaka Mh.', 'price_tl': 1.0, 'description': 'a' * 5000, 'photos': ['p'] * 40,
                             'page_text': 'b' * 8000, 'attributes': {'ada no': '1', 'parsel no': '2'}})
    assert detail['listing_id'] == '1' and detail['neighborhood'] == 'Yaka Mh.' and detail['price_tl'] == 1.0     # what the agent needs stays
    assert detail['photos'] == '[40 öğe]' and detail['attributes'].startswith('[nesne:') and len(detail['description']) < 200 and 'kısaltılmıştır' in detail['_özet']
    assert len(json.dumps(detail, ensure_ascii=False)) < 900
    small = {'found': True, 'area_m2': 12.5}
    assert compact_output(small) == small                                                                      # small outputs are untouched
    corridor = compact_output({'verdict': 'corridor_candidate', 'listing_id': '2', 'corridor': {'edges': list(range(500))}, 'geometry': ['x' * 50] * 300})
    assert corridor['verdict'] == 'corridor_candidate' and corridor['geometry'] == '[300 öğe]'                  # never "hidden": the verdict is readable


def test_history_outputs_are_compacted_and_the_oldest_entries_go_first_when_the_context_is_too_big():
    history = [{'kind': 'tool_finished', 'data': {'tool': 't', 'output': {'listings': [{'listing_id': 'a'}] * 40, 'total_reported': 40}}}]
    assert len(compact_history(history)[0]['data']['output']['listings']) == 30
    context = {'objective': 'x', 'history': [{'kind': 'decision', 'data': {'n': i, 'pad': 'z' * 3000}} for i in range(30)]}
    fitted = fit_context(context, budget=20_000)
    assert 0 < len(fitted['history']) < 30 and fitted['history'][-1]['data']['n'] == 29
    assert len(json.dumps(fitted)) <= 20_000


def test_what_an_agent_decides_from_is_never_summarized():
    queue = {'groups': [{'action': 'tkgm.lookup', 'operator': 'parcel', 'batch': [{'listing_id': str(1000000000 + i), 'parcel_key': '1/%d' % (i + 1), 'neighborhood': 'Yaka'} for i in range(10)]}] * 2}
    assert len(json.dumps(queue)) > 1500
    kept = compact_history([{'kind': 'tool_finished', 'data': {'tool': 'facts.queue', 'state': 'complete', 'output': queue}}])[0]['data']['output']
    assert kept == queue                                                          # the manager still sees every item it must dispatch
    summarized = compact_history([{'kind': 'tool_finished', 'data': {'tool': 'sahibinden.read_listing', 'state': 'complete', 'output': {'listing_id': '1', 'page_text': 'x' * 5000}}}])[0]['data']['output']
    assert summarized['listing_id'] == '1' and len(summarized['page_text']) < 200  # raw page reads are summarized


def _turn(listing, pad=7400):
    """One recorded turn of a batch reader, with the bookkeeping the engine stores beside the decision."""
    decision = {'kind': 'tool', 'target': 'sahibinden.read_listing', 'listing_ref': listing, 'summary': 'Sıradaki ilanı okuyorum.',
                'arguments_json': json.dumps({'listing_id': listing})}
    return [{'kind': 'decision', 'data': {'decision': decision, 'config_revision': 'c' * 64, 'prompt_version': '9',
                                          'generation': {'instructions': 'Sen bir operatörsün. ' * (pad // 21), 'usage': {'total_tokens': 12000}, 'invocation_id': 'i' * 32}}},
            {'kind': 'tool_finished', 'data': {'tool': 'sahibinden.read_listing', 'state': 'complete', 'call_id': 'c' + listing,
                                               'output': {'listing_id': listing, 'page_text': 'x' * 7000}}}]


def test_a_recorded_turn_keeps_what_the_agent_decided_but_not_a_copy_of_its_own_instructions():
    kept = compact_history(_turn('1000000001'))[0]['data']
    assert kept == {'decision': {'kind': 'tool', 'target': 'sahibinden.read_listing', 'listing_ref': '1000000001', 'summary': 'Sıradaki ilanı okuyorum.',
                                 'arguments_json': json.dumps({'listing_id': '1000000001'})}}


def test_a_ten_item_batch_stays_fully_in_view_so_no_listing_is_read_twice():
    # Before: ~8 KB per decision filled the 56 KB budget after five reads and the oldest ones were dropped, so the agent read them again.
    ids = [str(1000000000 + i) for i in range(10)]
    history = [entry for listing in ids for entry in _turn(listing)]
    context = {'objective': 'toplu iş', 'inputs': {'routine': 'batch', 'items': [{'listing_id': i} for i in ids]}, 'tools': [{'key': 't', 'pad': 'z' * 9000}],
               'history': compact_history(history)}
    fitted = fit_context(context)
    assert len(fitted['history']) == 20
    seen = [json.loads(e['data']['decision']['arguments_json'])['listing_id'] for e in fitted['history'] if e['kind'] == 'decision']
    assert seen == ids                                                            # the very first read is still there


def test_batch_progress_comes_from_every_event_of_the_task_not_from_the_history_window():
    ids = ['1000000001', '1000000002', '1000000003']
    inputs = {'routine': 'batch', 'action': 'sahibinden.read_listing', 'items': [{'listing_id': i, 'why': 'x'} for i in ids]}
    def started(listing, call): return ('tool_started', {'call_id': call, 'tool': 'sahibinden.read_listing', 'arguments': {'listing_id': listing}})
    def finished(call, state='complete'): return ('tool_finished', {'call_id': call, 'tool': 'sahibinden.read_listing', 'state': state})
    assert batch_progress(inputs, []) == {'done': [], 'remaining': ids, 'next': ids[0]}
    events = [started(ids[0], 'a'), finished('a'), started(ids[1], 'b'), finished('b', 'failed')]
    assert batch_progress(inputs, events) == {'done': [ids[0]], 'remaining': ids[1:], 'next': ids[1]}            # a failed read is not done
    events += [started(ids[1], 'c'), finished('c'), started(ids[2], 'd'), finished('d')]
    assert batch_progress(inputs, events) == {'done': ids, 'remaining': [], 'next': None}
    assert batch_progress({'routine': 'research_sweep'}, events) is None and batch_progress({'listing_id': 'x'}, events) is None
    other = [('tool_started', {'call_id': 'z', 'tool': 'tkgm.lookup', 'arguments': {'listing_id': ids[0]}}), finished('z')]
    assert batch_progress(inputs, other)['done'] == []                                                           # another tool's call does not count
