"""Searches outside Datça (the first: Latmos olive groves), kept apart from the Datça flow by construction.

The Datça pipeline reads `sahibinden.search` / `sahibinden.read_listing` observations and listing-linked records. A region search uses its own tools
(`region.search`, `region.read_listing`), its read arguments name the listing `ilan` (never `listing_id`), so its records are never linked to a
listing: no Datça rank, comparable, signal, brief, research task or seller message ever sees them. The browser, its single tab, its pace and its
stop rules are shared: a security check stops both, as it should for one account.

Read-only. What is derived from the reads (official area, elevation, slope) is kept in a store of its own (`.local/regions.sqlite`).
"""
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

PAGE_SIZE = 20
MAX_PAGES = 30

# One entry per search. `towns`: the site's own result paths, the only ones a region search may open; the order is the scan order.
REGIONS = {
    'latmos': {
        'label': 'Latmos zeytinlikleri',
        'towns': {
            'milas': '/satilik-arsa/mugla-milas',
            'karpuzlu': '/satilik-arsa/aydin-karpuzlu',
            'cine': '/satilik-arsa/aydin-cine',
            'kocarli': '/satilik-arsa/aydin-kocarli',
            'soke': '/satilik-arsa/aydin-soke',
        },
        'max_price_tl': 9_000_000,
        'min_area_m2': 20_000,
        'elevation_m': (200, 450),          # memecik quality band; 150-550 still counts in part
        'elevation_soft_m': (150, 550),
    },
}


def region(key):
    if key not in REGIONS:
        raise ValueError('Unknown region')
    return REGIONS[key]


def allowed_paths():
    return {path for spec in REGIONS.values() for path in spec['towns'].values()}


def search_url(args):
    """The result page a region.search job opens: only a configured town path with the region's own filters."""
    spec = region(args['region'])
    if args['town'] not in spec['towns']:
        raise ValueError('Unknown town')
    filters = {'price_max': spec['max_price_tl'], 'a507_min': spec['min_area_m2']}
    if args.get('sort') == 'newest':
        filters['sorting'] = 'date_desc'
    page = args.get('page', 1)
    if page > 1:
        filters['pagingOffset'] = (page - 1) * PAGE_SIZE
    return 'https://www.sahibinden.com' + spec['towns'][args['town']] + '?' + urlencode(filters)


def filters_of(args):
    spec = region(args['region'])
    return {'max_price_tl': spec['max_price_tl'], 'min_area_m2': spec['min_area_m2']}


def search_path_ok(url, args):
    spec = region(args['region'])
    return urlparse(url).path.rstrip('/') == spec['towns'].get(args['town'], '//')


def known_url(store, listing_id):
    """The detail address a region search card showed for this listing; None when no region search saw it (never a composed address)."""
    from verda.agency.browser import Listing
    if not re.fullmatch(r'\d{8,14}', str(listing_id or '')):
        return None
    with store.transaction() as con:
        for row in con.execute("SELECT payload FROM observations WHERE mode='local' AND tool='region.search' AND payload LIKE ? ORDER BY captured_at DESC, id DESC",
                               ('%' + listing_id + '%',)):
            for item in json.loads(row['payload']).get('listings', []):
                if item.get('listing_id') == listing_id and item.get('url'):
                    return Listing.model_validate({k: item[k] for k in ('listing_id', 'title', 'url')}).url
    return None


# ---- the region's own store ------------------------------------------------------------------------------------------------------------

def region_db(path=None):
    path = Path(path or '.local/regions.sqlite')
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS listings(region TEXT NOT NULL, listing_id TEXT NOT NULL, town TEXT, card TEXT, detail TEXT,
                   first_seen REAL, last_seen REAL, detail_at REAL, gone INTEGER NOT NULL DEFAULT 0, land TEXT, land_at REAL,
                   PRIMARY KEY(region, listing_id))''')
    con.execute('''CREATE TABLE IF NOT EXISTS prices(region TEXT NOT NULL, listing_id TEXT NOT NULL, at REAL NOT NULL, price_tl REAL NOT NULL,
                   PRIMARY KEY(region, listing_id, at))''')
    con.execute('''CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, region TEXT NOT NULL, started_at REAL, finished_at REAL, summary TEXT)''')
    return con


def remember_cards(db, region_key, town, cards, now):
    """-> ids whose detail should be read: new cards, and cards whose price or area changed since the last read."""
    wanted = []
    for card in cards:
        old = db.execute('SELECT card, detail_at FROM listings WHERE region=? AND listing_id=?', (region_key, card['listing_id'])).fetchone()
        before = json.loads(old['card']) if old and old['card'] else None
        if old is None:
            db.execute('INSERT INTO listings(region, listing_id, town, card, first_seen, last_seen) VALUES(?,?,?,?,?,?)',
                       (region_key, card['listing_id'], town, json.dumps(card, ensure_ascii=False), now, now))
        else:
            db.execute('UPDATE listings SET card=?, last_seen=?, town=? WHERE region=? AND listing_id=?',
                       (json.dumps(card, ensure_ascii=False), now, town, region_key, card['listing_id']))
        if card.get('price_tl'):
            last = db.execute('SELECT price_tl FROM prices WHERE region=? AND listing_id=? ORDER BY at DESC LIMIT 1', (region_key, card['listing_id'])).fetchone()
            if not last or last['price_tl'] != card['price_tl']:
                db.execute('INSERT OR IGNORE INTO prices VALUES(?,?,?,?)', (region_key, card['listing_id'], now, card['price_tl']))
        changed = before is not None and (before.get('price_tl'), before.get('area_m2')) != (card.get('price_tl'), card.get('area_m2'))
        if old is None or not old['detail_at'] or changed:
            wanted.append(card['listing_id'])
    db.commit()
    return wanted


# ---- the scan: submits read jobs; the running services do them (one tab, shared pace and stop rules) -------------------------------------

def _state(store, key):
    with store.transaction() as con:
        row = con.execute('SELECT id, state, reason FROM inbox WHERE request_key=?', (key,)).fetchone()
        return dict(row) if row else None


def _wait(store, key, step, sleep, limit):
    for _ in range(limit):
        step()
        state = _state(store, key)
        if state and state['state'] in {'complete', 'failed', 'blocked', 'uncertain', 'cancelled'}:
            return state
        sleep(1.0)
    return _state(store, key)


def _receipt(store, task_id, tool):
    with store.transaction() as con:
        row = con.execute('SELECT payload FROM observations WHERE task_id=? AND tool=? ORDER BY captured_at DESC LIMIT 1', (task_id, tool)).fetchone()
        return json.loads(row['payload']) if row else None


def scan(store, region_key, *, step=lambda: None, sleep=time.sleep, db=None, scan_id=None, towns=None, read_details=True,
         wait_limit=900, max_details=200, now=time.time):
    """Read every result page of the region's towns, then the details of new and changed cards. Stops at the first job that does not
    complete (a security check, a block, a failure): nothing is tried around it. -> summary dict."""
    spec = region(region_key)
    db = db or region_db()
    scan_id = scan_id or time.strftime('%Y%m%dT%H%M%S')
    started = now()
    db.execute('INSERT OR REPLACE INTO scans(id, region, started_at) VALUES(?,?,?)', (scan_id, region_key, started))
    db.commit()
    summary = {'region': region_key, 'scan_id': scan_id, 'towns': {}, 'details_read': 0, 'gone': 0, 'stopped': None}
    wanted = []
    for town in towns or list(spec['towns']):
        pages = cards = 0
        for page in range(1, MAX_PAGES + 1):
            key = f'region:{region_key}:{scan_id}:{town}:p{page}'
            store.submit(agent='region', objective=f'{spec["label"]}: {town} sayfa {page}', request_key=key, executor_tool='region.search',
                         inputs={'region': region_key, 'town': town, 'page': page, 'sort': 'newest'}, priority=8)
            state = _wait(store, key, step, sleep, wait_limit)
            receipt = _receipt(store, state['id'], 'region.search') if state and state['state'] == 'complete' else None
            if receipt is None:
                summary['stopped'] = f'{town} p{page}: ' + ((state or {}).get('reason') or (state or {}).get('state') or 'no_result')
                break
            pages += 1
            cards += len(receipt['listings'])
            wanted += [i for i in remember_cards(db, region_key, town, receipt['listings'], now()) if i not in wanted]
            if not receipt['has_more']:
                break
        summary['towns'][town] = {'pages': pages, 'cards': cards}
        if summary['stopped']:
            break
    if read_details and not summary['stopped']:
        for listing_id in wanted[:max_details]:
            key = f'region:{region_key}:{scan_id}:detail:{listing_id}'
            store.submit(agent='region', objective=f'{spec["label"]}: ilan detayı', request_key=key, executor_tool='region.read_listing',
                         inputs={'region': region_key, 'ilan': listing_id}, priority=8)
            state = _wait(store, key, step, sleep, wait_limit)
            receipt = _receipt(store, state['id'], 'region.read_listing') if state and state['state'] == 'complete' else None
            if receipt is None:
                summary['stopped'] = f'detail {listing_id}: ' + ((state or {}).get('reason') or (state or {}).get('state') or 'no_result')
                break
            if receipt.get('unavailable'):
                db.execute('UPDATE listings SET gone=1, detail_at=? WHERE region=? AND listing_id=?', (now(), region_key, listing_id))
                summary['gone'] += 1
            else:
                db.execute('UPDATE listings SET detail=?, detail_at=?, gone=0 WHERE region=? AND listing_id=?',
                           (json.dumps(receipt, ensure_ascii=False), now(), region_key, listing_id))
                summary['details_read'] += 1
            db.commit()
    summary['details_wanted'] = len(wanted)
    db.execute('UPDATE scans SET finished_at=?, summary=? WHERE id=?', (now(), json.dumps(summary, ensure_ascii=False), scan_id))
    db.commit()
    return summary
