from verda.agency.discovery import classify, should_stop


def row(i, price=1_000_000.0, area=2000.0):
    return {'listing_id': i, 'price_tl': price, 'area_m2': area}


def test_cards_are_new_changed_or_known_by_number_then_price_and_area():
    known = {'111': {'price_tl': 1_000_000.0, 'area_m2': 2000.0}, '222': {'price_tl': 2_000_000.0, 'area_m2': None},
             '444': {'price_tl': None, 'area_m2': None, 'detail': False}}
    marks = dict((r['listing_id'], why) for r, why in classify(known, [
        row('111'), row('222', price=1_900_000.0), row('333'), row('111', area=2100.0)]))
    assert marks['222'] == 'changed' and marks['333'] == 'new'
    assert classify(known, [row('444')])[0][1] == 'new'          # seen on a card, never read in detail
    assert classify(known, [row('111')])[0][1] == 'known'
    assert classify(known, [row('111', area=2100.0)])[0][1] == 'changed'
    # a card without a value cannot prove a change; an unknown stored value never marks one
    assert classify(known, [{'listing_id': '111', 'price_tl': None, 'area_m2': None}])[0][1] == 'known'
    assert classify(known, [row('222', price=2_000_000.0, area=5000.0)])[0][1] == 'known'


def test_a_daily_scan_stops_after_two_quiet_pages_or_three_pages_and_a_full_scan_reads_to_the_end():
    assert not should_stop([5], 'daily', True)
    assert not should_stop([5, 0], 'daily', True)
    assert should_stop([5, 0, 0], 'daily', True)
    assert should_stop([0, 0], 'daily', True)
    assert should_stop([5, 4, 3], 'daily', True)
    assert should_stop([5], 'daily', False)
    assert not should_stop([0, 0, 0, 0], 'full', True) and should_stop([0] * 15, 'full', True) and should_stop([2], 'full', False)


def test_the_baseline_uses_the_old_archive_then_observations(tmp_path):
    import json, sqlite3
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    from verda.agency.discovery import baseline
    archive = tmp_path / 'legacy.sqlite'
    db = sqlite3.connect(archive)
    db.executescript("CREATE TABLE legacy_snapshots(id TEXT, imported_at TEXT); CREATE TABLE legacy_rows(snapshot_id TEXT, table_name TEXT, row_key TEXT, subject_id TEXT, payload TEXT)")
    db.execute("INSERT INTO legacy_snapshots VALUES('s','2026-10-01')")
    db.execute("INSERT INTO legacy_rows VALUES('s','items','1','999',?)", (json.dumps({'legacy_json': json.dumps({'price_tl': 5_000_000.0, 'listing_area_m2': 1500.0})}),))
    db.commit(); db.close()
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize(); store.archive_path = archive
    assert baseline(store)['999'] == {'price_tl': 5_000_000.0, 'area_m2': 1500.0, 'source': 'legacy', 'detail': True}


def _store(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _observe(store, task, tool, payload, arguments=None, at=1.0):
    """Insert an observation directly (foreign keys off): these tests exercise the helpers, not the recording path."""
    import json, sqlite3, uuid
    con = sqlite3.connect(store.path)
    con.execute('PRAGMA foreign_keys=OFF')
    with con:
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, uuid.uuid4().hex, task, 'sahibinden', 't', payload.get('listing_id'), 'local', tool, 'sahibinden',
                     'tool_observation', at, json.dumps(arguments or {}), json.dumps(payload), uuid.uuid4().hex, '{}'))
    con.close()


def test_scan_delta_tells_the_agent_what_is_new_what_to_do_next_and_when_to_stop(tmp_path):
    from verda.agency.discovery import scan_delta
    store = _store(tmp_path)
    card = lambda i, price=1_000_000.0: {'listing_id': i, 'title': 'x', 'url': 'https://www.sahibinden.com/ilan/x-%s/detay' % i, 'price_tl': price, 'area_m2': 2000.0}
    assert scan_delta(store, {}, mode='local', task_id='T')['advice'] == 'read_next_page'
    _observe(store, 'T', 'sahibinden.search', {'page': 1, 'has_more': True, 'listings': [card('1111111111'), card('2222222222')]}, at=1)
    out = scan_delta(store, {'mode': 'daily'}, mode='local', task_id='T')
    assert out['advice'] == 'read_next_page' and out['next_page'] == 2 and out['new'] == 2
    _observe(store, 'T', 'sahibinden.read_listing', {'listing_id': '1111111111', 'price_tl': 1_000_000.0, 'area_m2': 2000.0}, at=2)
    _observe(store, 'T', 'sahibinden.read_listing', {'listing_id': '2222222222', 'price_tl': 1_000_000.0, 'area_m2': 2000.0}, at=3)
    # two quiet pages in a row now end the scan: nothing is new, nothing changed
    for page, at in ((2, 4), (3, 5)):
        _observe(store, 'T', 'sahibinden.search', {'page': page, 'has_more': True, 'listings': [card('1111111111')]}, at=at)
    out = scan_delta(store, {'mode': 'daily'}, mode='local', task_id='T')
    assert out['advice'] == 'finish' and out['unread_details'] == []
    # a price drop on a listing read in an earlier task brings its detail back
    _observe(store, 'OLD', 'sahibinden.read_listing', {'listing_id': '3333333333', 'price_tl': 1_000_000.0, 'area_m2': 2000.0}, at=0.5)
    _observe(store, 'T', 'sahibinden.search', {'page': 4, 'has_more': False, 'listings': [card('3333333333', 900_000.0)]}, at=6)
    out = scan_delta(store, {'mode': 'full'}, mode='local', task_id='T')
    assert out['changed'] == 1 and out['unread_details'] == ['3333333333'] and out['advice'] == 'read_details'


def test_inbox_plan_lists_only_conversations_that_changed(tmp_path):
    from verda.agency.inbox import inbox_plan
    store = _store(tmp_path)
    assert inbox_plan(store, {}, mode='local', task_id='T')['advice'] == 'list_inbox_first'
    rows = [{'title': 'A ilan', 'counterpart': 'Ali', 'last_at_text': '01.10.2026 10:00', 'unread': 1},
            {'title': 'B ilan', 'counterpart': 'Veli', 'last_at_text': '02.10.2026 11:00', 'unread': 0}]
    _observe(store, 'T', 'sahibinden.list_inbox', {'url': 'https://banaozel.sahibinden.com/mesajlarim', 'complete': True, 'rows': rows})
    # B was read earlier with the same last message time: it is marked read
    _observe(store, 'OLD', 'sahibinden.read_thread', {'found': True, 'listing_id': '1111111111', 'messages': []},
             arguments={'row': {'title': 'B ilan', 'counterpart': 'Veli', 'last_at_text': '02.10.2026 11:00'}})
    out = inbox_plan(store, {}, mode='local', task_id='T')
    assert [r['title'] for r in out['to_read']] == ['A ilan'] and out['advice'] == 'read_threads' and out['counts']['skip:already_read'] == 1
