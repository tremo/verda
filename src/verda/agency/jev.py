"""Pinned Jev policy + Browser Harness, serving the same durable browser queue.

Reads: fixed source navigation followed by Jev-selected scrolling/waiting; static DOM receipts
independently verify a DONE decision. The one write, a seller message, is `send.MessageSender`:
fixed code-owned selectors and the code-written text, never a model-selected click or text.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

from verda.agency.access import classify_url
from verda.agency.login import online

JEV_COMMIT = '1231850a0bf1a0c0341fe408ef1668dbbfdfac46'
ENV_KEYS = {'TYPESAFE_API_KEY', 'TYPESAFE_MODEL', 'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL', 'TEXT_MODEL_REASONING'}
PAGE_SIZE = 20
READ_STATE = Path(__file__).with_name('sahibinden-read.js').read_text()


def installation_status(store=None):
    try:
        package = importlib.metadata.distribution('jev-ultrafast')
        origin = json.loads(package.read_text('direct_url.json') or '{}')
        installed = origin.get('vcs_info', {}).get('commit_id') == JEV_COMMIT
        version = package.version
    except (importlib.metadata.PackageNotFoundError, ValueError):
        installed, version = False, None
    status = None
    if store:
        try:
            status = json.loads(store.path.with_suffix('.jev-status.json').read_text())
        except (OSError, ValueError):
            pass
    return {'installed': installed, 'version': version, 'commit': JEV_COMMIT, 'last_check': status,
            'requires': ['TYPESAFE_API_KEY', 'Browser Harness · Chrome bağlantısı'],
            'scope': 'Arama ve ilan okuma. Model sayfada kaydırma/bekleme seçer; sonucu statik okuyucu doğrular. Mesaj yalnız sahibinden.send_message ile gider (Ayarlar: kapalı / deneme / canlı); metni ve tıklamayı kod yapar.'}


def load_env(path):
    """Explicit file only. Never source a shell or expose secret values."""
    if path is None:
        return
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.removeprefix('export ').partition('=')
        key = key.strip()
        if not sep or key not in ENV_KEYS:
            continue
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        if value:
            os.environ[key] = value


def describe_failure(error, raw) -> str:
    """Bounded, non-private reason for a failed verification."""
    fields = [str(e['loc'][0]) if e['loc'] else e['type'] for e in error.errors()] if hasattr(error, 'errors') else []
    raw = raw if isinstance(raw, dict) else {}
    return (f"{type(error).__name__}: {','.join(fields) or str(error)[:120]}"
            f" | detail={'yes' if raw.get('detail') else 'no'} listings={len(raw.get('listings') or [])}"
            f" path={urlparse(str(raw.get('url', ''))).path[:80]}")


class JevStopped(RuntimeError):
    def __init__(self, code='read_failed'):
        super().__init__(code)
        self.code = code


def target_url(store, job):
    if job['tool'] == 'sahibinden.search':
        args = job['arguments']
        filters = {'price_max': args.get('max_price_tl', 15000000), 'a507_min': args.get('min_area_m2', 1000)}
        if args.get('sort') == 'newest':
            filters['sorting'] = 'date_desc'
        if args.get('page', 1) > 1:
            filters['pagingOffset'] = (args['page'] - 1) * PAGE_SIZE
        return 'https://www.sahibinden.com/satilik-arsa/mugla-datca?' + urlencode(filters)
    if job['tool'] != 'sahibinden.read_listing':
        raise JevStopped()
    url = known_listing_url(store, job['arguments']['listing_id'])
    if not url:
        raise JevStopped()               # the queue does not hand such a listing out; a job that still arrives stops here, never on a guessed address
    return url


def known_listing_url(store, key):
    """The detail address of a listing as some search, listing read or the old system observed it; None when none did.
    Only previously observed detail URLs. No model URL or guessed slug."""
    from verda.agency.browser import Listing
    if not re.fullmatch(r'\d{8,14}', str(key or '')):
        return None
    with store.transaction() as con:
        for row in con.execute("SELECT payload FROM observations WHERE mode='local' AND tool IN ('sahibinden.search','sahibinden.read_listing') AND payload LIKE ? ORDER BY captured_at DESC,id DESC",
                               ('%' + key + '%',)):
            data = json.loads(row['payload'])
            for item in data.get('listings', [data]):
                if item.get('listing_id') == key and item.get('url'):
                    return Listing.model_validate({k: item[k] for k in ('listing_id','title','url')}).url
    return legacy_listing_url(store, key)


def legacy_listing_url(store, listing_id):
    """The detail address the old system recorded for a listing (observed there, never composed here)."""
    from verda.agency.research import archive_connection
    from verda.agency.browser import source_url
    archive = archive_connection(store)
    if not archive:
        return None
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        row = snapshot and archive.execute("SELECT payload FROM legacy_rows WHERE table_name='items' AND subject_id=? AND snapshot_id=?",
                                           (listing_id, snapshot['id'])).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload'])
        data = json.loads(payload.get('legacy_json') or '{}')
        for url in (data.get('source_listing_url_original'), payload.get('listing_url'), data.get('listing_url')):
            if isinstance(url, str) and listing_id in url:
                try:
                    return source_url(url)
                except ValueError:
                    continue
    finally:
        archive.close()
    return None


def verify_receipt(job, raw):
    from verda.agency.browser import source_url, Listing, SearchReceipt, BLOCKERS
    if raw.get('blocker') in BLOCKERS:
        raise JevStopped(raw['blocker'])
    url = raw.get('url', '')
    if classify_url(url):
        raise JevStopped(classify_url(url))
    source_url(url)
    if job['tool'] == 'sahibinden.read_listing' and raw.get('removed') and not raw.get('detail'):
        from verda.agency.browser import ListingGone
        return ListingGone.model_validate({'listing_id': job['arguments']['listing_id'], 'unavailable': True,
                                           'notice': 'Görüntülemek istediğiniz ilan yayında değildir.', 'final_url': url}).model_dump()
    if job['tool'] == 'sahibinden.read_listing':
        result = Listing.model_validate(raw.get('detail')).model_dump()
        if result['listing_id'] != job['arguments']['listing_id'] or result['url'] != url:
            raise ValueError('Wrong listing')
        return result
    params = parse_qs(urlparse(url).query)
    filters = {'max_price_tl': job['arguments'].get('max_price_tl',15000000), 'min_area_m2': job['arguments'].get('min_area_m2',1000)}
    for key, value in [('price_max', filters['max_price_tl']), ('a507_min', filters['min_area_m2'])]:
        if len(params.get(key, [])) != 1 or float(params[key][0]) != value:
            raise ValueError('Filters not applied')
    page = job['arguments'].get('page', 1)
    if [int(v) for v in params.get('pagingOffset', ['0'])] != [(page - 1) * PAGE_SIZE]:
        raise ValueError('Wrong result page')
    if (params.get('sorting') == ['date_desc']) != (job['arguments'].get('sort') == 'newest'):
        raise ValueError('Sort order not applied')
    rows = raw['listings']
    total = raw['total_reported']
    if type(total) is not int or total < len(rows) or (total > 0 and not rows):
        raise ValueError('Unverified results')
    if len({row['listing_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate source rows')
    result = SearchReceipt.model_validate({'source_url':url,'filters':filters,'total_reported':total,
        'page':page,'has_more':total>(page-1)*PAGE_SIZE+len(rows),'listings':rows}).model_dump()
    for row in result['listings']:
        if row['price_tl'] is not None and row['price_tl'] > filters['max_price_tl']:
            raise ValueError('Price filter mismatch')
        if row['area_m2'] is not None and row['area_m2'] < filters['min_area_m2']:
            raise ValueError('Area filter mismatch')
    return result


def scoped_page(page):
    """The model sees only read actions; enforce again before execution."""
    return {**page, 'actions': [a for a in page['actions'] if a['kind'] in {'scroll','wait'}]}


def same_target(actual, expected):
    """Site redirects can reorder or normalize numeric filter parameters."""
    from verda.agency.browser import source_url
    source_url(actual)
    left, right = urlparse(actual), urlparse(expected)
    ids = [re.search(r'^/ilan/.+-(\d{8,14})/detay/?$', u.path) for u in (left, right)]
    if all(ids):
        # A detail page is the listing with that number: the seller may retitle it and the address follows the title.
        return ids[0].group(1) == ids[1].group(1) and left.hostname == right.hostname
    if left.path.rstrip('/') != right.path.rstrip('/'):
        return False
    a, b = parse_qs(left.query), parse_qs(right.query)
    if left.path.startswith('/satilik-arsa/'):
        offsets = [[int(v) for v in params.pop('pagingOffset', ['0'])] for params in (a, b)]
        if offsets[0] != offsets[1]:
            return False
        for params in (a,b):
            for key in ('price_max','a507_min'):
                if len(params.get(key,[])) != 1:
                    return False
                params[key] = [float(params[key][0])]
    return a == b


def connect_chrome(wait=90, attach=None, approve=None, sleep=time.sleep):
    """Open the user's Chrome if it is closed and attach to it (Browser Harness does the launching).

    Chrome asks "Allow remote debugging?". While *this* attach is waiting, the harness's own macOS
    helper presses that one button (Accessibility API, exact sheet title, Chrome only; no model, no
    free mouse). If macOS has not granted Accessibility to the launching app, or Chrome's debugging
    switch is off, the worker says so instead of guessing. Local Chrome only, bounded by `wait`.
    """
    import threading
    os.environ['BU_NAME'] = 'verda'
    try:
        from browser_harness.admin import ensure_daemon, _is_local_chrome_mode
        from browser_harness.macos import approve_remote_debugging
    except Exception:
        return 'chrome_connection_missing'
    attach, approve = attach or ensure_daemon, approve or approve_remote_debugging
    if not _is_local_chrome_mode():
        return 'browser_not_local'
    outcome = {}

    def run():
        try:
            attach(wait=wait, name='verda')
            outcome['ok'] = True
        except Exception:
            outcome['ok'] = False
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + wait
    while worker.is_alive() and time.monotonic() < deadline:
        status, _ = approve()
        if status == 'accessibility-required':
            return 'accessibility_required'
        if status == 'setup-required':
            return 'chrome_remote_debugging_switch_off'
        worker.join(2)
    return None if outcome.get('ok') else 'chrome_connection_missing'


def visible_browser(url):
    """A plain foreground tab in the user's own Chrome profile and session.

    No hidden tab, no fake focus, no fake window size: what happens is what the user sees.
    """
    from jev_ultrafast.browser import Browser
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp

    class Visible(Browser):
        def __init__(self, url):
            ensure_daemon()
            self.target = cdp('Target.createTarget', url='about:blank')['targetId']
            register_tab(self.target)
            self.session = cdp('Target.attachToTarget', targetId=self.target, flatten=True)['sessionId']
            self.call('Page.navigate', url=url)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.evaluate('document.readyState') == 'complete':
                    break
                time.sleep(0.05)
    return Visible(url)


# Jev lists only HTML/ARIA controls. Sahibinden's account menu is plain spans whose click handler lives in script,
# so before each observation those get an attribute-only role="link". Nothing is read, typed, or restyled.
CLICKABLES = """(() => { let n=0; for (const e of document.querySelectorAll('body *')) {
  if (e.getAttribute('role') || e.closest('a,button,input,select,textarea,[role]')) continue;
  const t=(e.innerText||'').trim();
  if (!t || t.length>300 || e.children.length>8) continue;
  if (getComputedStyle(e).cursor!=='pointer') continue;
  if (e.parentElement && getComputedStyle(e.parentElement).cursor==='pointer') continue;   // outermost only: one row, one target
  e.setAttribute('role','link'); n++; } return n; })()"""


def human_pause():
    """Between two browser actions: 0.5 to 2 seconds, never the same twice."""
    return random.uniform(0.5, 2.0)


class JevNavigator:
    """Jev picks where to click; this class decides what it may pick from and when it has arrived.

    `allow(label)` limits the choices shown to the model, so a wrong or manipulated page cannot lead it to
    anything else. `reached(url)` is the only success test: the page address, never the model's DONE.
    """

    def __init__(self, browser, chooser=None, sleep=time.sleep, settle=None):
        self.browser, self.chooser, self.sleep, self.settle = browser, chooser, sleep, settle
        self.last_label = None

    def go(self, goal, *, reached, allow, steps=4):
        from jev_ultrafast.browser import StalePage
        from jev_ultrafast.model import choose
        choose, history = self.chooser or choose, []
        for _ in range(steps):
            self.browser.evaluate(CLICKABLES)
            page = self.browser.observe(screenshot=False)
            signal = classify_url(page['url'])
            if signal:
                raise JevStopped(signal)
            if reached(page['url']):
                return page['url']
            shown = [a for a in page['actions'] if a['kind'] == 'click' and allow(a['label'])]
            for _ in range(3):
                if shown:
                    break
                # The target may still be scrolling into view or rendering: look again before giving up.
                self.sleep(human_pause())
                self.browser.evaluate(CLICKABLES)
                page = self.browser.observe(screenshot=False)
                shown = [a for a in page['actions'] if a['kind'] == 'click' and allow(a['label'])]
            if not shown:
                raise JevStopped('navigation_unavailable')
            if len(shown) == 1:
                action = shown[0]       # one allowed target: nothing to decide, and the arrival test still decides success
            else:
                decision = choose({**page, 'actions': shown}, goal, history)
                action = next((a for a in shown if a['id'] == decision['choice']), None)
            if not action:
                raise JevStopped('navigation_unavailable')
            try:
                self.browser.act(action, page)
            except StalePage:
                continue
            self.last_label = action['label']
            history.append({'action': action['label'], 'kind': 'click', 'text': None, 'page_changed': None})
            self.sleep(self.settle if self.settle is not None else human_pause())
        page = self.browser.observe(screenshot=False)
        if reached(page['url']):
            return page['url']
        raise JevStopped('navigation_unavailable')



# Sahibinden prints a card's last-message time as a full date, as a clock time for today or as a weekday name for the past week.
DATE_LINE = re.compile(r'^(?:\d{2}\.\d{2}\.\d{4}(?: \d{2}:\d{2})?|\d{1,2}:\d{2}|Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar|Dün|Bugün)$')


def parse_row(text):
    """One inbox card: title, optional unread count, counterpart, preview lines, date. No preview text is kept."""
    lines = [l.strip() for l in str(text).split('\n') if l.strip()]
    if not lines:
        return None
    title, rest, unread = lines[0], lines[1:], 0
    if rest and rest[0].isdigit():
        unread, rest = int(rest[0]), rest[1:]
    counterpart = rest[0] if rest else None
    last = rest[-1] if rest and DATE_LINE.match(rest[-1]) else None
    return {'title': title[:120], 'counterpart': counterpart[:80] if counterpart else None,
            'last_at_text': last, 'unread': unread}


def row_matches(text, row):
    """True when a card is exactly the conversation a list_inbox row described."""
    from verda.facts.text import fold
    other = parse_row(text)
    return bool(other) and fold(other['title']) == fold(row['title']) and \
        fold(other['counterpart'] or '') == fold(row.get('counterpart') or '') and other['last_at_text'] == row.get('last_at_text')

THREAD_READ = Path(__file__).with_name('sahibinden-thread.js').read_text()
INBOX_URL = 'https://banaozel.sahibinden.com/mesajlarim'
# The inbox list is a nested scroller Jev cannot drive. Code reads the row titles, matches them in Python, and only
# scrolls: either the list (more rows load) or the wanted row into view.
ROW_TEXTS = """(() => [...document.querySelectorAll('.message-card-by-person')].map(r => r.innerText))()"""
SCROLL_TO_ROW = """(i => { const r=document.querySelectorAll('.message-card-by-person')[i]; r.scrollIntoView({block:'center', behavior:'instant'}); return true; })"""
SCROLL_LIST = """(() => {
  const list = document.querySelector('.message-list-panel');
  const scroller = [list, ...(list ? list.querySelectorAll('*') : [])].find(e => e && e.scrollHeight > e.clientHeight + 20 && /auto|scroll/.test(getComputedStyle(e).overflowY)) || document.scrollingElement;
  const before = scroller.scrollTop;
  scroller.scrollBy(0, scroller.clientHeight * 0.8);
  return scroller.scrollTop !== before;
})()"""


def thread_receipt(listing_id, raw):
    """Verified, bounded receipt from the static thread reader. The conversation must belong to this listing."""
    from verda.agency.browser import ThreadReceipt, THREAD_MESSAGES, thread_url
    import hashlib
    url = raw.get('url', '')
    thread_url(url, listing_id)
    seen, messages = {}, []
    for item in raw.get('messages', []):
        text = (item.get('text') or '').strip()
        if not text:
            continue                                    # attachments and system rows carry no statement
        if item.get('role') not in {'seller', 'user'}:
            raise ValueError('Message sender could not be determined')
        base = hashlib.sha1('|'.join((item['role'], item.get('sent_at_text') or '', text)).encode()).hexdigest()[:12]
        seen[base] = seen.get(base, 0) + 1
        messages.append({'id': base if seen[base] == 1 else f'{base}-{seen[base]}', 'role': item['role'],
                         'text': text[:1500], 'truncated': len(text) > 1500, 'sent_at_text': item.get('sent_at_text')})
    omitted = len(messages) > THREAD_MESSAGES or bool(raw.get('older_omitted'))
    kept = messages[-THREAD_MESSAGES:]
    # A record is at most 32 KB (browser.finish): a long conversation (60 messages of 1,500 characters) failed every read and was retried.
    # The newest messages are what decides anything, so the oldest are dropped until the receipt fits and the omission is marked.
    while len(kept) > 1 and sum(len(m['text'].encode()) + 200 for m in kept) > 24000:
        kept.pop(0)
        omitted = True
    return ThreadReceipt.model_validate({'listing_id': listing_id, 'url': url, 'found': True,
                                         'older_omitted': omitted, 'messages': kept}).model_dump()


def listing_title(store, listing_id):
    with store.transaction() as con:
        for row in con.execute("SELECT payload FROM observations WHERE mode='local' AND tool IN ('sahibinden.search','sahibinden.read_listing') ORDER BY captured_at DESC,id DESC"):
            data = json.loads(row['payload'])
            for item in data.get('listings', [data]):
                if item.get('listing_id') == listing_id and item.get('title'):
                    return item['title']
    return None


# Older messages load when the conversation is scrolled up. A real wheel event over the panel, as a person would do it.
PANEL_BOX = """(() => { const e=document.querySelector('.message-detail-panel .messages-container') || document.querySelector('.message-detail-panel'); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+Math.min(r.height/2,300), top:e.scrollTop}; })()"""
BUBBLES = "document.querySelectorAll('.message-detail-panel .message-bubble-wrapper').length"
SCROLLBACK_ROUNDS = 40


# The page's own filter buttons ("Tümü (99)", "Okunmamış (5)"). Unread conversations are asked for by that filter, so none is missed
# however far down the list it sits; the view is put back to "Tümü" afterwards.
FILTER_BUTTON = """(label => { const b=[...document.querySelectorAll('button.filter-btn')].find(e => (e.innerText||'').trim().startsWith(label)); if(!b) return null;
  const r=b.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2, text:b.innerText.trim()}; })"""


class InboxReader:
    """Lists the conversations of the account's inbox. Reads titles and metadata only; no conversation is opened."""

    def __init__(self, browser_factory=None, sleep=time.sleep, keep_open=False):
        self.browser_factory, self.sleep, self.keep_open = browser_factory, sleep, keep_open

    def _press(self, browser, label):
        spot = browser.evaluate(FILTER_BUTTON + '(' + json.dumps(label) + ')')
        if not spot:
            return None
        for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            browser.call('Input.dispatchMouseEvent', type=kind, x=spot['x'], y=spot['y'], button='left', clickCount=1)
            self.sleep(0.15)
        return spot['text']

    def _unread_texts(self, browser):
        """The card texts the unread filter lists (all of them, scrolled until the list stops growing). Best effort: any failure leaves the plain list."""
        try:
            label = self._press(browser, 'Okunmamış')
            if not label or label.rstrip().endswith('(0)'):
                return []
            self.sleep(1.0)
            idle, texts = 0, []
            for _ in range(60):
                texts = browser.evaluate(ROW_TEXTS)
                moved = browser.evaluate(SCROLL_LIST)
                self.sleep(0.4 if moved else 1.0)
                grown = len(browser.evaluate(ROW_TEXTS)) != len(texts)
                idle = 0 if (moved or grown) else idle + 1
                if idle >= 3:
                    break
            texts = browser.evaluate(ROW_TEXTS)
            self._press(browser, 'Tümü')
            return texts
        except Exception:
            return []

    def read(self, store, job, progress):
        from jev_ultrafast.browser import Browser
        from verda.agency.browser import InboxReceipt, INBOX_ROWS
        browser = (self.browser_factory or Browser)(INBOX_URL)
        finished = False
        try:
            progress('connected')
            deadline = time.monotonic() + 20
            raw = browser.evaluate(THREAD_READ)
            while raw['inbox_rows'] == 0 and time.monotonic() < deadline:
                self.sleep(0.5)
                raw = browser.evaluate(THREAD_READ)
            if classify_url(raw['url']):
                raise JevStopped(classify_url(raw['url']))
            blocker = browser.evaluate(READ_STATE).get('blocker')
            if blocker:
                raise JevStopped(blocker)
            if raw['inbox_rows'] == 0:
                raise JevStopped()
            progress('observed')
            from verda.agency.inbox import enough_rows, settled_check
            recent = (job['arguments'].get('depth') or 'recent') == 'recent'
            settled = settled_check(store) if recent else None
            idle, complete, texts = 0, False, []
            for _ in range(400):
                texts = browser.evaluate(ROW_TEXTS)
                if recent and enough_rows([r for r in (parse_row(t) for t in texts) if r], settled):
                    break                                   # newest first: what is below needs nothing, no reason to scroll on
                moved = browser.evaluate(SCROLL_LIST)
                self.sleep(0.4 if moved else 1.0)
                grown = len(browser.evaluate(ROW_TEXTS)) != len(texts)
                idle = 0 if (moved or grown) else idle + 1
                if idle >= 6:
                    complete = True
                    break
            texts = browser.evaluate(ROW_TEXTS)
            complete = complete and not recent              # a recent read stops on purpose; a stall there must not pass for the whole list
            rows, seen = [], set()
            for text in texts + self._unread_texts(browser):
                row = parse_row(text)
                identity = (row['title'], row['counterpart'], row['last_at_text']) if row else None
                if row and identity not in seen:
                    seen.add(identity)
                    rows.append(row)
            result = InboxReceipt.model_validate({'url': INBOX_URL, 'complete': complete and len(rows) <= INBOX_ROWS,
                                                  'rows': rows[:INBOX_ROWS]}).model_dump()
            progress('verified')
            finished = True
            return result
        finally:
            if finished or not self.keep_open:
                try:
                    browser.close()
                except Exception:
                    pass


class ThreadReader:
    """Opens the inbox in the user's Chrome, lets Jev pick the conversation for one listing, reads it statically."""

    def __init__(self, browser_factory=None, chooser=None, sleep=time.sleep, keep_open=False):
        self.browser_factory, self.chooser, self.sleep, self.keep_open = browser_factory, chooser, sleep, keep_open

    def _wait(self, browser, test, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = browser.evaluate(THREAD_READ)
            if test(value):
                return value
            self.sleep(0.5)
        return browser.evaluate(THREAD_READ)

    def _reveal(self, browser, matches):
        """Bring the first card accepted by `matches(text)` into view. Returns the number of matching cards.

        "Not found" needs the whole list: the list ends only after it stopped moving and stayed the same size
        for several seconds, because rows load after the scroll.
        """
        idle = 0
        for _ in range(120):
            texts = browser.evaluate(ROW_TEXTS)
            hits = [i for i, t in enumerate(texts) if matches(t)]
            if hits:
                browser.evaluate(SCROLL_TO_ROW + '(' + str(hits[0]) + ')')
                return len(hits)
            moved = browser.evaluate(SCROLL_LIST)
            self.sleep(0.4 if moved else 1.0)
            grown = len(browser.evaluate(ROW_TEXTS)) != len(texts)
            idle = 0 if (moved or grown) else idle + 1
            if idle >= 6:
                return 0
        return 0

    def _scroll_back(self, browser):
        """Wheel upward over the conversation until no older messages load. Returns True when the top was reached."""
        stable = 0
        for _ in range(SCROLLBACK_ROUNDS):
            box, before = browser.evaluate(PANEL_BOX), browser.evaluate(BUBBLES)
            browser.call('Input.dispatchMouseEvent', type='mouseWheel', x=box['x'], y=box['y'], deltaX=0, deltaY=-700)
            self.sleep(0.6)
            moved = browser.evaluate(PANEL_BOX)['top'] != box['top'] or browser.evaluate(BUBBLES) != before
            stable = 0 if moved else stable + 1
            if stable >= 2:
                return True
        return False

    def read(self, store, job, progress):
        from jev_ultrafast.browser import Browser
        from verda.agency.browser import ThreadReceipt, source_url
        from verda.facts.text import fold
        row = job['arguments'].get('row')
        listing_id = job['arguments'].get('listing_id')
        seen_before = False
        if row:
            title = row['title']
            matches = lambda text: row_matches(text, row)
            allow_key = lambda label: all(fold(part)[:30] in fold(label) for part in (row['title'], row.get('counterpart') or '', row.get('last_at_text') or ''))
        else:
            title = listing_title(store, listing_id)
            if not title or not re.fullmatch(r'\d{8,14}', listing_id):
                raise JevStopped()                      # a conversation is found by a title we have actually observed
            key = fold(title)[:30]
            from verda.agency.inbox import found_before, known_titles
            seen_before = found_before(store, listing_id)        # then "not found" can only be a reading failure, never an answer
            titles = None if seen_before else known_titles(store)
            if titles is not None and not any(key in t for t in titles):
                # The inbox list we already hold says there is no conversation for this listing: no need to open the inbox and scroll it again.
                progress('verified')
                return ThreadReceipt.model_validate({'listing_id': listing_id, 'url': INBOX_URL, 'found': False, 'messages': []}).model_dump()
            matches = lambda text: key in fold(text.split('\n')[0])
            allow_key = lambda label: key in fold(label)
        browser = (self.browser_factory or Browser)(INBOX_URL)
        finished = False
        try:
            progress('connected')
            raw = self._wait(browser, lambda v: v['inbox_rows'] > 0)
            if classify_url(raw['url']):
                raise JevStopped(classify_url(raw['url']))
            blocker = browser.evaluate(READ_STATE).get('blocker')
            if blocker:
                raise JevStopped(blocker)
            if raw['inbox_rows'] == 0:
                progress('read_failed', 'inbox_not_shown')
                raise JevStopped()                      # the inbox did not show: not evidence that no conversation exists
            progress('observed')
            tried = set()
            for _ in range(3):
                if not self._reveal(browser, matches):
                    if row:
                        progress('read_failed', 'row_not_found')
                        raise JevStopped()         # the row listed a moment ago is gone: nothing to read, no claim to make
                    if not row and seen_before:
                        progress('read_failed', 'known_conversation_not_found')
                        raise JevStopped()         # an earlier read opened it: reporting "no conversation" would hide what the seller wrote
                    progress('verified')
                    finished = True
                    return ThreadReceipt.model_validate({'listing_id': listing_id, 'url': INBOX_URL, 'found': False, 'messages': []}).model_dump()
                nav = JevNavigator(browser, chooser=self.chooser, sleep=self.sleep)
                opened = lambda u: u.split('?')[0].rstrip('/').count('/') > 3 and '/mesajlarim/' in u
                try:
                    url = nav.go('Gelen kutusunda, başlığı "%s" olan ilanın konuşmasını aç.' % title[:120], reached=opened,
                                 allow=lambda label: allow_key(label) and label not in tried)
                except JevStopped as error:
                    if error.code != 'navigation_unavailable':
                        raise
                    if tried and not row and not seen_before:
                        # Every row with this title belonged to another listing (a re-posted ad): this listing has none.
                        progress('verified')
                        finished = True
                        return ThreadReceipt.model_validate({'listing_id': listing_id, 'url': INBOX_URL, 'found': False, 'messages': []}).model_dump()
                    progress('read_failed', 'conversation_not_opened')
                    raise JevStopped() from None
                if row:
                    listing_id = urlparse(url).path.split('/')[2]    # the address names the listing; the receipt checks it
                    break
                if '/mesajlarim/%s/' % listing_id in url:
                    break
                tried.add(nav.last_label)               # same title, another listing: back to the inbox and try the next row
                browser.call('Page.navigate', url=INBOX_URL)
                self._wait(browser, lambda v: v['inbox_rows'] > 0)
            else:
                raise JevStopped()
            self._wait(browser, lambda v: v['panel'] and v['messages'])
            complete = self._scroll_back(browser)
            raw = browser.evaluate(THREAD_READ)
            raw['older_omitted'] = not complete
            source_url(INBOX_URL.replace('banaozel', 'www'))
            result = thread_receipt(listing_id, raw)
            progress('verified')
            finished = True
            return result
        finally:
            # A finished read leaves nothing open; a stopped one stays for the user when asked (security check).
            if finished or not self.keep_open:
                try:
                    browser.close()
                except Exception:
                    pass



class JevReader:
    """`patience`: seconds to wait, without touching anything, when a security check page
    appears (the user may pass it by hand). `keep_open`: leave that tab for the user."""

    def __init__(self, browser_factory=None, chooser=None, sleep=time.sleep, patience=0, keep_open=False, connect=None):
        self.browser_factory, self.chooser, self.sleep, self.connect = browser_factory, chooser, sleep, connect
        self.patience, self.keep_open = patience, keep_open

    def ready(self):
        if not installation_status()['installed']:
            return 'package_missing'
        if self.connect:
            reason = self.connect()
            if reason:
                return reason
        try:
            # Probe only. Starting a worker must not silently enable debugging,
            # launch a different profile, or create a cloud browser.
            os.environ['BU_NAME'] = 'verda'
            from browser_harness.admin import daemon_browser_kind
            if daemon_browser_kind('verda') != 'local':
                return 'chrome_connection_missing'
            from browser_harness.helpers import cdp
            cdp('Browser.getVersion')
        except Exception:
            return 'chrome_connection_missing'
        return None

    def read(self, url, job, progress):
        from jev_ultrafast.browser import Browser, StalePage
        from jev_ultrafast.model import choose
        browser = (self.browser_factory or Browser)(url)
        history, started, leave_open, explained, finished = [], time.monotonic(), False, False, False
        try:
            progress('connected')
            for _ in range(12):
                if time.monotonic() - started > 240:
                    raise JevStopped()
                page = browser.observe(screenshot=False)
                raw = browser.evaluate(READ_STATE)
                # Check challenge/login/off-site before sharing content with a model.
                from verda.agency.browser import source_url
                if raw.get('blocker'):
                    raise JevStopped(raw['blocker'])
                # A redirect to a login or a security-check page is named, never reported as a mere read error.
                signal = classify_url(page['url']) or classify_url(raw.get('url'))
                if signal == 'unusual_access' and self.patience:
                    # Observe only: poll the address. Whoever passes the check does it by hand.
                    progress('WAIT')
                    waited = 0
                    while waited < self.patience and signal:
                        self.sleep(3); waited += 3
                        signal = classify_url(browser.evaluate('location.href'))
                    if not signal:
                        continue
                    leave_open = self.keep_open
                if signal:
                    raise JevStopped(signal)
                source_url(page['url'])
                if raw.get('removed') and job['tool'] == 'sahibinden.read_listing':
                    progress('observed')
                    result = verify_receipt(job, raw)         # the page itself says the ad is gone: that is the receipt
                    progress('verified')
                    finished = True
                    return result
                if not same_target(page['url'],url) or not same_target(raw['url'],url):
                    raise JevStopped()
                progress('observed')
                try:
                    result = verify_receipt(job, raw)
                except JevStopped:
                    raise
                except Exception as error:
                    result = None          # not loaded yet: that is when navigation decisions are needed
                    if not explained:
                        explained = True
                        progress('verify_failed', describe_failure(error, raw))
                if result is not None:
                    progress('verified')
                    finished = True
                    return result
                goal = ('Inspect only this public Sahibinden page. Do not navigate, click, type, log in, or send messages. '
                        'Choose DONE when the requested search results or listing details have loaded. '
                        'Use scrolling or WAIT only if needed to see loaded content. Stop BLOCKED at any access challenge. '
                        'An independent DOM reader will extract and verify the result. Task: ' +
                        ('Datça land search, first page only.' if job['tool']=='sahibinden.search' else 'Read listing ' + job['arguments']['listing_id']))
                try:
                    decision = (self.chooser or choose)(scoped_page(page), goal, history)
                except Exception as error:
                    progress('model_failed', type(error).__name__)     # class only: messages may carry private data
                    raise JevStopped() from None
                choice = decision['choice']
                if choice in {'DONE','BLOCKED'}:
                    progress(choice)
                    if choice == 'BLOCKED':
                        raise JevStopped()
                    try:
                        if not browser.fresh(page):
                            continue
                    except StalePage:
                        continue
                    raw_final = browser.evaluate(READ_STATE)
                    try:
                        result = verify_receipt(job, raw_final)
                    except JevStopped:
                        raise
                    except Exception as error:
                        # Say why, in bounded non-private terms; leave the tab for the user in visible mode.
                        progress('verify_failed', describe_failure(error, raw_final))
                        leave_open = self.keep_open
                        raise JevStopped() from None
                    progress('verified')
                    finished = True
                    return result
                action = next((a for a in scoped_page(page)['actions'] if a['id']==choice),None)
                if not action:
                    raise JevStopped()
                self.sleep(human_pause())  # No Jev-speed click loop on Sahibinden.
                try:
                    browser.act(action,page)
                except StalePage:
                    continue
                progress('WAIT' if action['kind']=='wait' else 'SCROLL_DOWN' if action['delta']>0 else 'SCROLL_UP')
                history.append({'action':action['label'],'kind':action['kind'],'text':None,'page_changed':None})
            raise JevStopped()
        finally:
            if not (leave_open or (self.keep_open and not finished)):
                try:
                    browser.close()
                except Exception:
                    pass  # Preserve the source result/incident if tab cleanup fails.


TAB_REGISTRY = Path(os.environ.get('VERDA_TABS', '.local/automation-tabs.json'))
STALE_TAB_SECONDS = 1800


def register_tab(target, path=None):
    """Remember a tab the automation opened, so that tabs a failed run left behind can be closed later (never the user's own tabs)."""
    path = path or TAB_REGISTRY
    try:
        entries = json.loads(path.read_text()) if path.exists() else []
        entries.append({'id': target, 'at': time.time()})
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(entries[-300:], out)
    except OSError:
        pass


def close_stale_tabs(path=None, *, closer=None, now=None, max_age=STALE_TAB_SECONDS):
    """Close automation tabs older than `max_age` seconds. Returns how many were closed."""
    path = path or TAB_REGISTRY
    now = time.time() if now is None else now
    if not path.exists():
        return 0
    if closer is None:
        from browser_harness.helpers import cdp
        closer = lambda target: cdp('Target.closeTarget', targetId=target)
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    keep, closed = [], 0
    for entry in entries:
        if now - entry['at'] < max_age:
            keep.append(entry)
            continue
        try:
            closer(entry['id'])
            closed += 1
        except Exception:
            pass                                          # already gone
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(keep, out)
    return closed


def login_job(store, job, report):
    """The sign-in tools. The scripted flow or one allowed click; a success reopens the source whoever got there."""
    from verda.agency import login
    hands = login.harness_hands()
    report('connected')
    result = login.relogin(hands) if job['tool'] == 'sahibinden.login' else login.click_choice(hands, job['arguments']['label'])
    login.record(store, result, via='agent')
    if result['ok']:
        login.resolve_open(store, result)
    report('verified')
    return {k: result.get(k) for k in ('ok', 'reason', 'detail', 'tabs_left_open', 'screen') if result.get(k) is not None or k in ('ok', 'reason')}


def send_job(bridge, job, worker, report, browser_factory=None, open_page=None, clock=None, sleep=None):
    """One seller message in the user's visible Chrome. A security control stops the job like any read; everything else is in the receipt."""
    from verda.agency import send
    open_page = open_page or (lambda url: send.CdpPage((browser_factory or visible_browser)(url), READ_STATE, THREAD_READ))
    sender = send.MessageSender(open_page, **({'now': clock} if clock else {}), **({'sleep': sleep} if sleep else {}))
    try:
        return sender.run(job, report, lambda text_hash, url: bridge.checkpoint_send(job, worker, text_hash, url))
    except send.Stopped as stop:
        raise JevStopped(stop.code) from None


def run_worker(store, *, env_file=None, continuous=False, reader=None, visible=False, launch=True):
    from verda.agency.browser import BrowserBridge
    load_env(env_file)
    bridge = BrowserBridge(store)
    link = connect_chrome if launch else None
    reader = reader or (JevReader(browser_factory=visible_browser, patience=180, keep_open=True, connect=link) if visible else JevReader(connect=link))
    worker = 'jev-' + uuid4().hex
    completed = 0
    def status(reason):
        path = store.path.with_suffix('.jev-status.json')
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'checked_at':time.time(), 'reason':reason}, out)
    last_tidy, last_beat = [0.0], [0.0]

    def housekeeping():
        """Between jobs: close tabs a failed run left behind, and sign in again if the session was lost."""
        now = time.time()
        if now - last_tidy[0] > 300:
            last_tidy[0] = now
            try:
                close_stale_tabs()
            except Exception:
                pass
        from verda.agency import login
        try:
            if login.open_incident(store) and login.may_try(store) and reader.ready() is None:
                login.heal(store, login.harness_hands())
        except Exception:
            pass                                           # a failed attempt never stops the worker

    if not continuous:
        reason = reader.ready()
        status(reason)
        if reason:
            return {'driver':'jev','state':'waiting_setup','reason':reason,'completed':0}
    try:
        while True:
            bridge.expire()
            if continuous:
                housekeeping()
                # A service: stay idle (Chrome untouched) until a job waits, then connect, opening Chrome if it is closed.
                if not bridge.has_queued():
                    if time.time() - last_beat[0] > 20:
                        # Idle is not absent: without this the panel showed "Bağlantı bekliyor" while the worker was running.
                        bridge.attach(worker, 'Browser Use · Jev Ultrafast', driver='jev', ttl=30)
                        last_beat[0] = time.time()
                    time.sleep(5)
                    continue
                if not online():
                    status('offline')                      # no internet: jobs wait; they are not attempted and not counted as failures
                    time.sleep(30)
                    continue
                reason = reader.ready()
                status(reason)
                if reason:
                    time.sleep(30)
                    continue
            else:
                reason = reader.ready()
                status(reason)
                if reason:
                    return {'driver':'jev','state':'waiting_setup','reason':reason,'completed':completed}
            bridge.attach(worker,'Browser Use · Jev Ultrafast',driver='jev',ttl=30)
            job = bridge.claim(worker)
            if job:
                # A bounded job is allowed up to ten minutes; idle health lasts 30s.
                bridge.attach(worker,'Browser Use · Jev Ultrafast',driver='jev',ttl=300)
                report = lambda phase, detail=None: bridge.progress(job,worker,phase,detail)
                try:
                    if job['tool'] in ('sahibinden.login', 'sahibinden.login_click'):
                        result = login_job(store, job, report)
                    elif job['tool'] == 'sahibinden.send_message':
                        result = send_job(bridge, job, worker, report, reader.browser_factory, getattr(reader, 'open_page', None), getattr(reader, 'clock', None), getattr(reader, 'sleep', None))
                    elif job['tool'] == 'sahibinden.list_inbox':
                        result = InboxReader(browser_factory=reader.browser_factory, keep_open=reader.keep_open).read(store, job, report)
                    elif job['tool'] == 'sahibinden.read_thread':
                        result = ThreadReader(browser_factory=reader.browser_factory, chooser=reader.chooser, keep_open=reader.keep_open).read(store, job, report)
                    else:
                        result = reader.read(target_url(store,job),job,report)
                except JevStopped as error:
                    # Every stop says where it happened: a stop without a reason cannot be told apart from the next one (41 silent row failures, Oct 2026).
                    tb = error.__traceback__
                    while tb and tb.tb_next:
                        tb = tb.tb_next
                    try:
                        report('read_failed' if error.code == 'read_failed' else 'BLOCKED', '%s@%s:%s' % (error.code, Path(tb.tb_frame.f_code.co_filename).name, tb.tb_lineno) if tb else error.code)
                    except Exception:
                        pass                               # reporting never decides the outcome
                    bridge.finish(job['id'],worker,job['token'],error=error.code)
                except Exception as error:
                    tb = error.__traceback__
                    while tb and tb.tb_next:
                        tb = tb.tb_next
                    # Class and source line only: exception messages may carry page content.
                    report('read_failed', '%s@%s:%s' % (type(error).__name__, Path(tb.tb_frame.f_code.co_filename).name, tb.tb_lineno) if tb else type(error).__name__)
                    bridge.finish(job['id'],worker,job['token'],error='read_failed')
                else:
                    bridge.finish(job['id'],worker,job['token'],result=result)
                completed += 1
            if not continuous:
                return {'driver':'jev','state':'idle','completed':completed}
            time.sleep(5)
    finally:
        bridge.detach(worker)
