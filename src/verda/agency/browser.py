"""Durable read-only jobs for a scoped Codex/Chrome browser driver.

This module never launches Chrome, copies cookies or bypasses a site challenge.
The driver uses the already-authorized computer-use connection and returns a receipt.
"""
import json
from typing import Literal
import time
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator
from verda.legacy import canonical, digest

READ_TOOLS = {'sahibinden.search', 'sahibinden.read_listing', 'sahibinden.read_thread', 'sahibinden.list_inbox', 'sahibinden.login', 'sahibinden.login_click'}
LOGIN_TOOLS = {'sahibinden.login', 'sahibinden.login_click'}
SEND_TOOL = 'sahibinden.send_message'          # the one browser tool that writes: gated and recorded by `message`, run by `send`
BLOCKERS = {'captcha', 'otp', 'rate_limited', 'access_denied', 'session_lost', 'unusual_access'}
DRIVERS = {'codex': 'Codex · Chrome', 'jev': 'Browser Use · Jev Ultrafast'}


def source_url(value):
    url = urlparse(value)
    if url.scheme != 'https' or url.hostname not in {'www.sahibinden.com', 'sahibinden.com'} or url.username or url.password or url.port:
        raise ValueError('Only Sahibinden HTTPS source URLs are allowed')
    return value


class Listing(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    listing_id: str = Field(pattern=r'^\d{8,14}$')
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=1500)
    price_tl: FiniteFloat | None = Field(default=None, gt=0)
    area_m2: FiniteFloat | None = Field(default=None, gt=0)
    neighborhood: str | None = Field(default=None, max_length=200)
    listed_at_text: str | None = Field(default=None, max_length=100)
    parcel_key: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=16000)
    # Visible text of the whole page for the reading agent. Bounded so a receipt always fits.
    page_text: str | None = Field(default=None, max_length=8000)
    # Photo addresses on the site's own CDN; the images are saved by a separate tool.
    photos: list[str] | None = Field(default=None, max_length=30)
    # Structured label→value rows of the detail page (e.g. "tapu durumu"). Raw and
    # bounded; the fact layer decides what, if anything, they establish.
    attributes: dict[str, str] | None = Field(default=None, max_length=40)

    @model_validator(mode='after')
    def fit_receipt(self):
        room = 14000 - len(self.description or '')
        if self.page_text and len(self.page_text) > max(room, 0):
            self.page_text = self.page_text[:max(room, 0)] or None
        return self

    @field_validator('photos')
    @classmethod
    def site_photos_only(cls, value):
        from verda.agency.photos import allowed
        for url in value or []:
            if len(url) > 500 or not allowed(url):
                raise ValueError('Photo address outside the site CDN')
        return value

    @field_validator('attributes')
    @classmethod
    def bounded_attributes(cls, value):
        for label, text in (value or {}).items():
            if not label.strip() or len(label) > 80 or len(text) > 300:
                raise ValueError('Listing attribute out of bounds')
        return value

    @field_validator('url')
    @classmethod
    def listing_url(cls, value):
        source_url(value)
        if not urlparse(value).path.startswith('/ilan/'):
            raise ValueError('Listing URL required')
        return value

    @model_validator(mode='after')
    def same_listing(self):
        if not urlparse(self.url).path.endswith('-' + self.listing_id + '/detay'):
            raise ValueError('Listing URL and identifier differ')
        return self


THREAD_MESSAGES = 60
INBOX_PATH = '/mesajlarim'


def thread_url(value, listing_id=None):
    """The account's own message pages only: banaozel.sahibinden.com/mesajlarim[/<listing>/<id>/<id>]."""
    url = urlparse(value)
    if url.scheme != 'https' or url.hostname != 'banaozel.sahibinden.com' or url.username or url.password or url.port:
        raise ValueError('Only the Sahibinden message pages are allowed')
    parts = [p for p in url.path.split('/') if p]
    if not parts or parts[0] != INBOX_PATH.strip('/'):
        raise ValueError('Not a message page')
    if listing_id is not None and (len(parts) < 2 or parts[1] != listing_id):
        raise ValueError('Conversation belongs to another listing')
    return value


class ThreadMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[0-9a-f]{12}(-\d+)?$')
    role: Literal['seller', 'user']
    text: str = Field(min_length=1, max_length=1500)
    truncated: bool = False
    sent_at_text: str | None = Field(default=None, max_length=60)


class ThreadReceipt(BaseModel):
    """`found` False means the inbox was shown and no conversation for the listing exists: no information, not an answer."""
    model_config = ConfigDict(extra='forbid', strict=True)
    listing_id: str = Field(pattern=r'^\d{8,14}$')
    url: str = Field(max_length=1500)
    found: bool
    older_omitted: bool = False
    messages: list[ThreadMessage] = Field(max_length=THREAD_MESSAGES)

    @model_validator(mode='after')
    def consistent(self):
        thread_url(self.url, self.listing_id if self.found else None)
        if not self.found and self.messages:
            raise ValueError('Messages without a conversation')
        return self


class ListingGone(BaseModel):
    """The listing page says the ad is not published any more (sold, withdrawn, expired): a fact about the ad, not a read error."""
    model_config = ConfigDict(extra='forbid', strict=True)
    listing_id: str = Field(pattern=r'^\d{8,14}$')
    unavailable: Literal[True]
    notice: str = Field(min_length=1, max_length=300)
    final_url: str = Field(max_length=1500)

    @field_validator('final_url')
    @classmethod
    def sahibinden(cls, value):
        return source_url(value)


INBOX_ROWS = 400


class InboxRow(BaseModel):
    """What the inbox list shows about one conversation. No message text: that belongs to the thread reader."""
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    counterpart: str | None = Field(default=None, max_length=80)
    last_at_text: str | None = Field(default=None, max_length=40)
    unread: int = Field(default=0, ge=0)


class InboxReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    url: str = Field(max_length=300)
    complete: bool                                  # the list was scrolled to its end
    rows: list[InboxRow] = Field(max_length=INBOX_ROWS)

    @field_validator('url')
    @classmethod
    def inbox_url(cls, value):
        thread_url(value)
        if urlparse(value).path.rstrip('/') != INBOX_PATH:
            raise ValueError('Inbox URL required')
        return value


class LoginReceipt(BaseModel):
    """What a sign-in step achieved and what the page in front shows now (for the agent that decides the next allowed click)."""
    model_config = ConfigDict(extra='forbid', strict=True)
    ok: bool
    reason: str = Field(max_length=40)
    detail: str = Field(default='', max_length=300)
    tabs_left_open: int | None = Field(default=None, ge=0)
    screen: dict | None = None

    @field_validator('screen')
    @classmethod
    def small_screen(cls, value):
        if value is not None and len(json.dumps(value)) > 2500:
            raise ValueError('Screen description too large')
        return value


class SendReceipt(BaseModel):
    """What the sender reports. `not_sent` means nothing was clicked; `delivery_unknown` means the click may have happened."""
    model_config = ConfigDict(extra='forbid', strict=True)
    outbox_id: str = Field(min_length=1, max_length=64)
    listing_id: str = Field(pattern=r'^\d{8,14}$')
    status: Literal['dry_run', 'confirmed', 'delivery_unknown', 'not_sent', 'cancelled']
    reason: str = Field(max_length=80)
    thread_url: str | None = Field(default=None, max_length=1500)
    evidence: dict = Field(default_factory=dict)

    @field_validator('thread_url')
    @classmethod
    def conversation(cls, value):
        return None if value is None else thread_url(value)

    @field_validator('evidence')
    @classmethod
    def small(cls, value):
        if len(json.dumps(value)) > 4000:
            raise ValueError('Evidence too large')
        return value


class SearchReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_url: str = Field(max_length=2000)
    total_reported: int = Field(ge=0)
    page: int = Field(ge=1)
    has_more: bool
    filters: dict
    listings: list[Listing] = Field(max_length=50)

    @field_validator('source_url')
    @classmethod
    def search_url(cls, value):
        source_url(value)
        if not urlparse(value).path.startswith('/satilik-arsa/mugla-datca'):
            raise ValueError('Datça search URL required')
        return value


class BrowserBridge:
    def __init__(self, store):
        self.store = store

    def select(self, driver):
        if driver not in DRIVERS:
            raise ValueError('Unknown browser driver')
        with self.store.transaction() as con:
            if con.execute("SELECT 1 FROM browser_jobs WHERE state IN ('queued','running')").fetchone():
                raise ValueError('Bekleyen tarayıcı işleri varken motor değiştirilemez.')
            con.execute('UPDATE browser_config SET driver=? WHERE id=1', (driver,))

    def attach(self, worker, label='Codex · Mac Chrome', now=None, *, driver='codex', ttl=600):
        now = time.time() if now is None else now
        if driver not in DRIVERS or not worker or len(worker) > 100 or len(label) > 200 or not 1 <= ttl <= 600:
            raise ValueError('Invalid browser driver')
        with self.store.transaction() as con:
            con.execute('INSERT INTO browser_sessions(id,label,seen_at,expires_at,driver) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,seen_at=excluded.seen_at,expires_at=excluded.expires_at,driver=excluded.driver',
                        (worker, label, now, now + ttl, driver))

    def detach(self, worker):
        with self.store.transaction() as con:
            con.execute('UPDATE browser_sessions SET expires_at=0 WHERE id=?', (worker,))

    def overview(self, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            selected = con.execute('SELECT driver FROM browser_config WHERE id=1').fetchone()[0]
            sessions = [dict(r) for r in con.execute('SELECT * FROM browser_sessions ORDER BY seen_at DESC LIMIT 10')]
            jobs = [{k: r[k] for k in ('id', 'task_id', 'tool', 'driver', 'state', 'created_at', 'finished_at')}
                    for r in con.execute('SELECT * FROM browser_jobs ORDER BY created_at DESC LIMIT 50')]
        from verda.agency.access import ACCOUNT, login_state
        with self.store.transaction() as con:
            last = con.execute("SELECT payload,captured_at FROM observations WHERE tool='sahibinden.read_listing' AND mode='local' ORDER BY captured_at DESC LIMIT 1").fetchone()
        session = {'expected_account': ACCOUNT['name'] or 'ayarlanmadı (.local/account.json)', 'state': 'unknown', 'seen_at': None,
                   'basis': 'Henüz bir ilan okunmadı; oturum son ilan sayfasının üst menüsünden anlaşılır.'}
        if last:
            session.update(state=login_state((json.loads(last['payload']) or {}).get('page_text')), seen_at=last['captured_at'],
                           basis='Son ilan okumasının sayfa üst menüsü.')
        from verda.agency.jev import installation_status
        return {'kind': 'browser_job_bridge', 'sahibinden_session': session, 'selected_driver': selected, 'label': DRIVERS[selected],
                'drivers': [{'key': k, 'label': v} for k, v in DRIVERS.items()], 'jev': installation_status(self.store),
                'active': any(s['expires_at'] > now and s['driver'] == selected for s in sessions),
                'sessions': sessions, 'jobs': jobs, 'read_tools': sorted(READ_TOOLS),
                'note': ('Jev yerel yürütücüsü Browser Harness ile Chrome’a bağlanır. Yürütücü veya bağlantı yokken işler kuyrukta bekler. ' if selected == 'jev' else
                         'Mevcut Codex tarayıcı oturumu üzerinden çalışır. Oturum kapalıyken işler kuyrukta bekler. ') + 'Mesaj gönderme: ' + {'off': 'kapalı', 'dry_run': 'deneme (göndermez)', 'live': 'canlı'}[__import__('verda.agency.message', fromlist=['x']).mode_of(self.store)] + '.'}

    def enqueue(self, task, tool, call_id, now=None):
        now = time.time() if now is None else now
        if task['mode'] != 'local' or not ((tool.key in READ_TOOLS and tool.effect == 'read') or (tool.key == SEND_TOOL and tool.effect == 'write')):
            raise ValueError('Browser bridge only accepts supported live reads and the registered sender')
        with self.store.transaction() as con:
            self.store._owned(con, task, now)
            call = con.execute("SELECT * FROM tool_calls WHERE id=? AND task_id=? AND state='started'", (call_id, task['id'])).fetchone()
            if not call:
                raise ValueError('Tool receipt missing')
            driver = con.execute('SELECT driver FROM browser_config WHERE id=1').fetchone()[0]
            con.execute("INSERT INTO browser_jobs(id,task_id,tool,arguments,state,created_at,driver) VALUES(?,?,?,?,'queued',?,?)",
                        (call_id, task['id'], tool.key, call['arguments'], now, driver))
            con.execute("UPDATE inbox SET state='waiting_browser',reason='browser_driver_wait',token=NULL,lease_until=NULL WHERE id=?", (task['id'],))
            self.store._event(con, task, 'browser_queued', {'call_id': call_id, 'tool': tool.key, 'driver': driver}, now)

    def claim(self, worker, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            session = con.execute('SELECT * FROM browser_sessions WHERE id=? AND expires_at>?', (worker, now)).fetchone()
            if not session:
                raise ValueError('Browser session must be attached')
            # Expired claims are not replayed: the browser may already have navigated.
            if con.execute("SELECT 1 FROM browser_jobs WHERE state='running'").fetchone():
                return None
            job = con.execute("SELECT * FROM browser_jobs WHERE state='queued' AND driver=? ORDER BY created_at,id LIMIT 1", (session['driver'],)).fetchone()
            if not job:
                return None
            task = con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone()
            resource = con.execute("SELECT halted FROM resources WHERE key='sahibinden'").fetchone()
            if task['state'] != 'waiting_browser' or (resource and resource['halted']):
                return None
            token = uuid4().hex
            con.execute("UPDATE browser_jobs SET state='running',worker=?,token=?,lease_until=? WHERE id=?", (worker, token, now + 600, job['id']))
            self.store._event(con, task, 'browser_claimed', {'call_id': job['id'], 'worker': worker}, now)
            return {'id': job['id'], 'task_id': task['id'], 'tool': job['tool'], 'arguments': json.loads(job['arguments']),
                    'token': token, 'driver': job['driver'], 'objective': task['objective'], 'listing_ref': task['listing_ref']}

    def progress(self, job, worker, phase, detail=None, *, now=None):
        if phase not in {'connected', 'observed', 'SCROLL_DOWN', 'SCROLL_UP', 'WAIT', 'DONE', 'BLOCKED', 'verified', 'model_failed', 'read_failed', 'verify_failed', 'filled', 'clicked'}:
            raise ValueError('Invalid browser progress')
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            owned = con.execute("SELECT * FROM browser_jobs WHERE id=? AND worker=? AND token=? AND state='running' AND lease_until>?", (job['id'], worker, job['token'], now)).fetchone()
            if not owned:
                raise ValueError('Browser job is not owned')
            task = con.execute('SELECT * FROM inbox WHERE id=?', (owned['task_id'],)).fetchone()
            self.store._event(con, task, 'browser_progress', {'call_id': job['id'], 'driver': owned['driver'], 'phase': phase, **({'detail': str(detail)[:300]} if detail else {})}, now)

    def checkpoint_send(self, job, worker, text_hash, url, *, now=None):
        """The click intent, written before the click. True only while this job owns the outbox row, the text is the approved one
        and the whole conversation history (read again here, not taken from the gate) has not covered these fields; else the refusal code."""
        from verda.agency import message
        now = time.time() if now is None else now
        args = job['arguments']
        refusal = message.history_refusal(self.store, args['listing_id'], 'local', args.get('fields') or [])     # its own read: no nested write transaction
        with self.store.transaction() as con:
            owned = con.execute("SELECT * FROM browser_jobs WHERE id=? AND worker=? AND token=? AND state='running' AND lease_until>?", (job['id'], worker, job['token'], now)).fetchone()
            if not owned or owned['tool'] != SEND_TOOL:
                raise ValueError('Browser job is not owned')
            args = json.loads(owned['arguments'])
            granted = refusal is None and message.checkpoint(con, args['outbox_id'], job['id'], text_hash, now)
            task = con.execute('SELECT * FROM inbox WHERE id=?', (owned['task_id'],)).fetchone()
            self.store._event(con, task, 'send_click_intent' if granted else 'send_click_refused', {'call_id': job['id'], 'outbox_id': args['outbox_id'], 'url': str(url)[:300],
                                                                                                  **({'reason': refusal['reason'], 'fields': refusal.get('fields')} if refusal else {})}, now)
            return True if granted else (refusal['reason'] if refusal else False)

    def finish(self, job_id, worker, token, *, result=None, error=None, now=None):
        now = time.time() if now is None else now
        if error is not None and error not in BLOCKERS | {'read_failed'}:
            raise ValueError('Invalid browser error category')
        if (result is None) == (error is None):
            raise ValueError('Supply exactly one result or error')
        fingerprint = digest({'result': result, 'error': error})
        with self.store.transaction() as con:
            job = con.execute('SELECT * FROM browser_jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['worker'] != worker or job['token'] != token:
                raise ValueError('Browser job is not owned')
            if job['state'] in {'complete', 'failed'}:
                if job['result_hash'] == fingerprint:
                    return
                raise ValueError('Browser receipt conflicts with saved result')
            if job['state'] != 'running' or job['lease_until'] <= now:
                raise ValueError('Browser job expired; inspect before retrying')
            tool = next(t for t in self.store.config.tools if t.key == job['tool'])
            nothing_happened = False
            if tool.key == SEND_TOOL:
                from verda.agency import message
                args = json.loads(job['arguments'])
                if result is not None:
                    receipt = SendReceipt.model_validate(result).model_dump()
                    if receipt['outbox_id'] != args['outbox_id'] or receipt['listing_id'] != args['listing_id']:
                        raise ValueError('Wrong send receipt')
                    final = message.finish(con, args['outbox_id'], job_id, receipt['status'], now, reason=receipt['reason'],
                                           thread_url=receipt['thread_url'], evidence=receipt['evidence'])
                    result = {**receipt, 'outbox_state': final, 'dry_run': args['dry_run'], 'browser_driver': 'jev_ultrafast', 'captured_at': now, 'synthetic': False}
                else:
                    # A security control or a failure: if the click may have happened the row becomes delivery_unknown and the call stays uncertain.
                    nothing_happened = not message.clicked_possibly(con, args['outbox_id'])
                    message.finish(con, args['outbox_id'], job_id, 'not_sent', now, reason=error)
            elif result is not None:
                args = json.loads(job['arguments'])
                if tool.key == 'sahibinden.search':
                    result = SearchReceipt.model_validate(result).model_dump()
                    expected = {'max_price_tl': args.get('max_price_tl', 15000000), 'min_area_m2': args.get('min_area_m2', 1000)}
                    if result['filters'] != expected:
                        raise ValueError('Search filters do not match the assigned task')
                    if result['page'] != args.get('page', 1):
                        raise ValueError('Search page does not match the assigned task')
                    params = parse_qs(urlparse(result['source_url']).query)
                    for url_key, value in (('price_max', expected['max_price_tl']), ('a507_min', expected['min_area_m2'])):
                        values = params.get(url_key, [])
                        if len(values) != 1 or float(values[0]) != value:
                            raise ValueError('Search URL does not preserve the observed filters')
                elif tool.key in LOGIN_TOOLS:
                    result = LoginReceipt.model_validate(result).model_dump()
                elif tool.key == 'sahibinden.list_inbox':
                    result = InboxReceipt.model_validate(result).model_dump()
                elif tool.key == 'sahibinden.read_thread':
                    result = ThreadReceipt.model_validate(result).model_dump()
                    if args.get('listing_id') and result['listing_id'] != args['listing_id']:
                        raise ValueError('Wrong conversation receipt')
                    if not args.get('listing_id') and not result['found']:
                        raise ValueError('A row read must open its conversation')
                elif isinstance(result, dict) and result.get('unavailable'):
                    result = ListingGone.model_validate(result).model_dump()
                    if result['listing_id'] != args['listing_id']:
                        raise ValueError('Wrong listing receipt')
                else:
                    result = Listing.model_validate(result).model_dump()
                    if result['listing_id'] != args['listing_id']:
                        raise ValueError('Wrong listing receipt')
                result = {**result, 'browser_driver': 'jev_ultrafast' if job['driver'] == 'jev' else 'codex_chrome', 'captured_at': now, 'synthetic': False}
                if len(canonical(result).encode()) > 32768:
                    raise ValueError('Browser result is too large')
            task = dict(con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone())
            if task['state'] != 'waiting_browser':
                raise ValueError('Task is not waiting for this browser receipt')
            task['token'] = uuid4().hex
            con.execute("UPDATE inbox SET state='running',token=?,lease_until=? WHERE id=?", (task['token'], now + 60, task['id']))
            self.store._finish_tool(con, task, tool, job_id, result=result, error=error, halt=error in BLOCKERS, now=now, nothing_happened=nothing_happened)
            con.execute('UPDATE browser_jobs SET state=?,finished_at=?,result_hash=? WHERE id=?',
                        ('failed' if error else 'complete', now, fingerprint, job_id))

    def has_queued(self):
        with self.store.transaction() as con:
            return con.execute("SELECT 1 FROM browser_jobs WHERE state='queued' AND driver='jev' LIMIT 1").fetchone() is not None

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            for job in con.execute("SELECT * FROM browser_jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall():
                task = dict(con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone())
                click_may_have_happened = False
                if job['tool'] == SEND_TOOL:
                    from verda.agency import message
                    args = json.loads(job['arguments'])
                    click_may_have_happened = message.clicked_possibly(con, args['outbox_id'])
                    message.finish(con, args['outbox_id'], job['id'], 'not_sent', now, reason='browser_connection_lost')
                if not click_may_have_happened and self.store._lost_read_retry(con, task, now):
                    # Nothing was changed (every other browser tool reads, and a sender that never reached its click wrote nothing): the job is tried again.
                    con.execute("UPDATE browser_jobs SET state='failed',finished_at=? WHERE id=?", (now, job['id']))
                    con.execute("UPDATE tool_calls SET state='failed',result=?,finished_at=? WHERE id=?", (canonical({'output': None, 'error': 'browser_connection_lost'}), now, job['id']))
                    con.execute('UPDATE resources SET owner=NULL WHERE owner=?', (job['id'],))
                    con.execute("UPDATE inbox SET state='queued',reason='worker_lost_retry',pending=NULL,token=NULL,lease_until=NULL,available_at=? WHERE id=?", (now + 5, task['id']))
                    self.store._event(con, task, 'worker_lost', {'automatic_retry': True}, now)
                    continue
                con.execute("UPDATE browser_jobs SET state='uncertain',finished_at=? WHERE id=?", (now, job['id']))
                con.execute("UPDATE tool_calls SET state='uncertain',finished_at=? WHERE id=?", (now, job['id']))
                con.execute("UPDATE resources SET owner=NULL,halted='browser_connection_lost' WHERE owner=?", (job['id'],))
                con.execute("UPDATE inbox SET state='uncertain',reason='browser_connection_lost' WHERE id=?", (task['id'],))
                incident = self.store._source_incident(con, task, 'sahibinden', 'browser_connection_lost', now)
                self.store._record_outcome(con, task, 'uncertain', None, 'browser_connection_lost', now, incident)
