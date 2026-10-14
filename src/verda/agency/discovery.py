"""Finding new and changed listings without reading everything again.

A listing is "known" when the old archive or a detail read has its number; a card seen on a result page is not enough. A known listing is read again only
when its card shows another price or area than the last values on record. New numbers and changed cards go to the
detail reader; the rest costs nothing. The scan walks the newest-first result pages and stops when pages bring nothing.
"""
import json
import time
from uuid import uuid4

PAGE_STOP_QUIET, DAILY_MAX_PAGES, FULL_MAX_PAGES = 2, 3, 15      # defaults; the panel's settings override them


def baseline(store):
    """listing_id -> {price_tl, area_m2, source}: the old archive first, then what this system observed (later wins)."""
    known = {}
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if archive:
        try:
            snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
            for row in archive.execute("SELECT subject_id,payload FROM legacy_rows WHERE table_name='items' AND snapshot_id=?",
                                       (snapshot['id'],)) if snapshot else []:
                data = json.loads(json.loads(row['payload']).get('legacy_json') or '{}')
                known[row['subject_id']] = {'price_tl': data.get('price_tl'), 'area_m2': data.get('listing_area_m2'), 'source': 'legacy', 'detail': True}
        finally:
            archive.close()
    with store.transaction() as con:
        for row in con.execute("SELECT tool,payload FROM observations WHERE mode='local' AND tool IN ('sahibinden.search','sahibinden.read_listing') ORDER BY captured_at,id"):
            data = json.loads(row['payload'])
            for item in data.get('listings', [data]):
                listing_id = item.get('listing_id')
                if not listing_id:
                    continue
                if row['tool'] == 'sahibinden.read_listing':        # a detail read sets the reference values
                    known[listing_id] = {'price_tl': item.get('price_tl'), 'area_m2': item.get('area_m2'), 'source': row['tool'], 'detail': True}
                else:                                               # a card only proves the number exists
                    known.setdefault(listing_id, {'price_tl': None, 'area_m2': None, 'source': row['tool'], 'detail': False})
    return known


def _differs(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) > 0.5


def classify(known, rows):
    """[(row, 'new' | 'changed' | 'known')] for the cards of one result page."""
    out = []
    for row in rows:
        prior = known.get(row['listing_id'])
        if prior is None or not prior.get('detail', True):
            out.append((row, 'new'))                       # never read in detail: new to us, whatever a card said before
        elif _differs(row.get('price_tl'), prior['price_tl']) or _differs(row.get('area_m2'), prior['area_m2']):
            out.append((row, 'changed'))
        else:
            out.append((row, 'known'))
    return out


def should_stop(history, mode, has_more, limits=None):
    """history: per page, the number of new or changed cards. Daily scans stop after two quiet pages or three pages."""
    quiet_pages, daily_max, full_max = limits or (PAGE_STOP_QUIET, DAILY_MAX_PAGES, FULL_MAX_PAGES)
    if not has_more:
        return True
    if mode == 'full':
        return len(history) >= full_max
    quiet = history[-quiet_pages:]
    return len(history) >= daily_max or (len(quiet) == quiet_pages and not any(quiet))


def _task_state(store, request_key):
    with store.transaction() as con:
        row = con.execute('SELECT id,state,reason FROM inbox WHERE request_key=?', (request_key,)).fetchone()
        return dict(row) if row else None


def _run_until_done(store, request_key, step, *, limit=400, sleep=time.sleep):
    for _ in range(limit):
        step()
        state = _task_state(store, request_key)
        if state and state['state'] in {'complete', 'failed', 'blocked', 'uncertain'}:
            return state
        sleep(0.5)
    return _task_state(store, request_key)


def _observation(store, task_id, tool):
    with store.transaction() as con:
        row = con.execute("SELECT payload FROM observations WHERE task_id=? AND tool=? ORDER BY captured_at DESC LIMIT 1", (task_id, tool)).fetchone()
        return json.loads(row['payload']) if row else None


def run_scan(store, *, mode='daily', step, sleep=time.sleep, scan_id=None, read_details=True):
    """Walk the result pages, then read the details of new and changed cards. `step()` advances the workers once."""
    scan_id = scan_id or time.strftime('%Y%m%dT%H%M%S')
    known = baseline(store)
    pages, history, wanted, stopped = [], [], {}, None
    page = 1
    while True:
        key = f'scan:{scan_id}:{mode}:p{page}'
        store.submit(agent='sahibinden', objective=f'Tarama sayfa {page}', request_key=key, executor_tool='sahibinden.search',
                     inputs={'query': 'datca arsa', 'page': page, 'sort': 'newest'})
        state = _run_until_done(store, key, step, sleep=sleep)
        receipt = _observation(store, state['id'], 'sahibinden.search') if state and state['state'] == 'complete' else None
        if receipt is None:
            stopped = (state or {}).get('reason') or (state or {}).get('state') or 'no_result'
            break
        marks = classify(known, receipt['listings'])
        interesting = [(r, why) for r, why in marks if why != 'known']
        for r, why in interesting:
            wanted.setdefault(r['listing_id'], why)
        history.append(len(interesting))
        pages.append({'page': page, 'cards': len(marks), 'new': sum(w == 'new' for _, w in marks), 'changed': sum(w == 'changed' for _, w in marks)})
        if should_stop(history, mode, receipt['has_more']):
            break
        page += 1
    details = {'requested': 0, 'complete': 0, 'failed': 0}
    if read_details and stopped is None:
        for listing_id in wanted:
            key = f'scan:{scan_id}:detail:{listing_id}'
            store.submit(agent='sahibinden', objective='İlan detayı', request_key=key, executor_tool='sahibinden.read_listing',
                         inputs={'listing_id': listing_id}, listing_ref=listing_id)
            details['requested'] += 1
            state = _run_until_done(store, key, step, sleep=sleep)
            details['complete' if state and state['state'] == 'complete' else 'failed'] += 1
    return {'scan_id': scan_id, 'mode': mode, 'pages': pages, 'wanted': len(wanted),
            'new': sum(w == 'new' for w in wanted.values()), 'changed': sum(w == 'changed' for w in wanted.values()),
            'details': details, 'stopped_early': stopped}


def scan_delta(store, args, *, mode, task_id):
    """The agent's scan helper: what the result pages read so far in *this task* brought, and whether to go on.

    Pure bookkeeping, no site access. The agent decides; this says which cards are new or changed against the old
    archive and earlier detail reads, how many pages in a row were quiet, and which details are still unread.
    """
    scan_mode = args.get('mode', 'daily')
    known = baseline(store)
    with store.transaction() as con:
        pages = [(json.loads(r['payload'])) for r in con.execute(
            "SELECT payload FROM observations WHERE task_id=? AND tool='sahibinden.search' AND mode=? ORDER BY captured_at,id", (task_id, mode))]
        details = {r['listing_ref'] for r in con.execute(
            "SELECT listing_ref FROM observations WHERE task_id=? AND tool='sahibinden.read_listing' AND mode=?", (task_id, mode))}
    if not pages:
        return {'pages_read': 0, 'advice': 'read_next_page', 'next_page': 1, 'unread_details': []}
    per_page, wanted = [], {}
    for page in sorted({p['page']: p for p in pages}.values(), key=lambda p: p['page']):
        marks = classify(known, page['listings'])
        interesting = [(r['listing_id'], why) for r, why in marks if why != 'known']
        wanted.update(dict(interesting))
        per_page.append({'page': page['page'], 'cards': len(marks), 'new': sum(w == 'new' for _, w in interesting),
                         'changed': sum(w == 'changed' for _, w in interesting)})
    last = sorted({p['page']: p for p in pages}.values(), key=lambda p: p['page'])[-1]
    from verda.agency.tuning import number
    limits = (number(store, 'scan.quiet_pages'), number(store, 'scan.daily_max_pages'), number(store, 'scan.full_max_pages'))
    stop = should_stop([p['new'] + p['changed'] for p in per_page], scan_mode, last['has_more'], limits)
    unread = [i for i in wanted if i not in details]
    advice = 'read_next_page' if not stop else ('read_details' if unread else 'finish')
    return {'pages_read': len(per_page), 'per_page': per_page, 'mode': scan_mode, 'advice': advice,
            'next_page': None if stop else last['page'] + 1, 'new': sum(w == 'new' for w in wanted.values()),
            'changed': sum(w == 'changed' for w in wanted.values()), 'unread_details': unread[:40],
            'unread_details_total': len(unread)}
