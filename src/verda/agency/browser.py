"""Durable read-only jobs for a scoped Codex/Chrome browser driver.

This module never launches Chrome, copies cookies or bypasses a site challenge.
The driver uses the already-authorized computer-use connection and returns a receipt.
"""
import json
import time
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator
from verda.legacy import canonical, digest

READ_TOOLS = {'sahibinden.search', 'sahibinden.read_listing'}
BLOCKERS = {'captcha', 'rate_limited', 'access_denied', 'session_lost'}


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

    def attach(self, worker, label='Codex · Mac Chrome', now=None):
        now = time.time() if now is None else now
        if not worker or len(worker) > 100 or len(label) > 200:
            raise ValueError('Invalid browser driver')
        with self.store.transaction() as con:
            con.execute('INSERT INTO browser_sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,seen_at=excluded.seen_at,expires_at=excluded.expires_at',
                        (worker, label, now, now + 600))

    def detach(self, worker):
        with self.store.transaction() as con:
            con.execute('UPDATE browser_sessions SET expires_at=0 WHERE id=?', (worker,))

    def overview(self, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            sessions = [dict(r) for r in con.execute('SELECT * FROM browser_sessions ORDER BY seen_at DESC LIMIT 10')]
            jobs = [{k: r[k] for k in ('id', 'task_id', 'tool', 'state', 'created_at', 'finished_at')}
                    for r in con.execute('SELECT * FROM browser_jobs ORDER BY created_at DESC LIMIT 50')]
        return {'kind': 'codex_browser_bridge', 'active': any(s['expires_at'] > now for s in sessions),
                'sessions': sessions, 'jobs': jobs, 'read_tools': sorted(READ_TOOLS),
                'note': 'Mevcut Codex tarayıcı oturumu üzerinden çalışır. Oturum kapalıyken işler kuyrukta bekler. Mesaj gönderme bağlı değil.'}

    def enqueue(self, task, tool, call_id, now=None):
        now = time.time() if now is None else now
        if task['mode'] != 'local' or tool.key not in READ_TOOLS or tool.effect != 'read':
            raise ValueError('Browser bridge only accepts supported live reads')
        with self.store.transaction() as con:
            self.store._owned(con, task, now)
            call = con.execute("SELECT * FROM tool_calls WHERE id=? AND task_id=? AND state='started'", (call_id, task['id'])).fetchone()
            if not call:
                raise ValueError('Tool receipt missing')
            con.execute("INSERT INTO browser_jobs(id,task_id,tool,arguments,state,created_at) VALUES(?,?,?,?,'queued',?)",
                        (call_id, task['id'], tool.key, call['arguments'], now))
            con.execute("UPDATE inbox SET state='waiting_browser',reason='browser_driver_wait',token=NULL,lease_until=NULL WHERE id=?", (task['id'],))
            self.store._event(con, task, 'browser_queued', {'call_id': call_id, 'tool': tool.key}, now)

    def claim(self, worker, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            session = con.execute('SELECT * FROM browser_sessions WHERE id=? AND expires_at>?', (worker, now)).fetchone()
            if not session:
                raise ValueError('Browser session must be attached')
            # Expired claims are not replayed: the browser may already have navigated.
            if con.execute("SELECT 1 FROM browser_jobs WHERE state='running'").fetchone():
                return None
            job = con.execute("SELECT * FROM browser_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not job:
                return None
            task = con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone()
            token = uuid4().hex
            con.execute("UPDATE browser_jobs SET state='running',worker=?,token=?,lease_until=? WHERE id=?", (worker, token, now + 600, job['id']))
            self.store._event(con, task, 'browser_claimed', {'call_id': job['id'], 'worker': worker}, now)
            return {'id': job['id'], 'task_id': task['id'], 'tool': job['tool'], 'arguments': json.loads(job['arguments']),
                    'token': token, 'objective': task['objective'], 'listing_ref': task['listing_ref']}

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
            if result is not None:
                args = json.loads(job['arguments'])
                if tool.key == 'sahibinden.search':
                    result = SearchReceipt.model_validate(result).model_dump()
                    expected = {'max_price_tl': args.get('max_price_tl', 15000000), 'min_area_m2': args.get('min_area_m2', 1000)}
                    if result['filters'] != expected:
                        raise ValueError('Search filters do not match the assigned task')
                    params = parse_qs(urlparse(result['source_url']).query)
                    for url_key, value in (('price_max', expected['max_price_tl']), ('a507_min', expected['min_area_m2'])):
                        values = params.get(url_key, [])
                        if len(values) != 1 or float(values[0]) != value:
                            raise ValueError('Search URL does not preserve the observed filters')
                else:
                    result = Listing.model_validate(result).model_dump()
                    if result['listing_id'] != args['listing_id']:
                        raise ValueError('Wrong listing receipt')
                result = {**result, 'browser_driver': 'codex_chrome', 'captured_at': now, 'synthetic': False}
                if len(canonical(result).encode()) > 32768:
                    raise ValueError('Browser result is too large')
            task = dict(con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone())
            if task['state'] != 'waiting_browser':
                raise ValueError('Task is not waiting for this browser receipt')
            task['token'] = uuid4().hex
            con.execute("UPDATE inbox SET state='running',token=?,lease_until=? WHERE id=?", (task['token'], now + 60, task['id']))
            self.store._finish_tool(con, task, tool, job_id, result=result, error=error, halt=error in BLOCKERS, now=now)
            con.execute('UPDATE browser_jobs SET state=?,finished_at=?,result_hash=? WHERE id=?',
                        ('failed' if error else 'complete', now, fingerprint, job_id))

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.store.transaction() as con:
            for job in con.execute("SELECT * FROM browser_jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall():
                task = dict(con.execute('SELECT * FROM inbox WHERE id=?', (job['task_id'],)).fetchone())
                con.execute("UPDATE browser_jobs SET state='uncertain',finished_at=? WHERE id=?", (now, job['id']))
                con.execute("UPDATE tool_calls SET state='uncertain',finished_at=? WHERE id=?", (now, job['id']))
                con.execute("UPDATE resources SET owner=NULL,halted='browser_connection_lost' WHERE owner=?", (job['id'],))
                con.execute("UPDATE inbox SET state='uncertain',reason='browser_connection_lost' WHERE id=?", (task['id'],))
                incident = self.store._source_incident(con, task, 'sahibinden', 'browser_connection_lost', now)
                self.store._record_outcome(con, task, 'uncertain', None, 'browser_connection_lost', now, incident)
