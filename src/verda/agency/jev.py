"""Pinned Jev policy + Browser Harness, serving the same durable browser queue.

The first integration exposes only read tools: fixed source navigation followed
by Jev-selected scrolling/waiting. No model-selected clicks or text entry can
send messages. Static DOM receipts independently verify a DONE decision.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

JEV_COMMIT = '1231850a0bf1a0c0341fe408ef1668dbbfdfac46'
ENV_KEYS = {'TYPESAFE_API_KEY', 'TYPESAFE_MODEL', 'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL', 'TEXT_MODEL_REASONING'}
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
            'scope': 'Arama ve ilan okuma. Model sayfada kaydırma/bekleme seçer; sonucu statik okuyucu doğrular. Metin yazma ve mesaj gönderme kapalı.'}


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


class JevStopped(RuntimeError):
    def __init__(self, code='read_failed'):
        super().__init__(code)
        self.code = code


def target_url(store, job):
    from verda.agency.browser import Listing
    if job['tool'] == 'sahibinden.search':
        args = job['arguments']
        filters = {'price_max': args.get('max_price_tl', 15000000), 'a507_min': args.get('min_area_m2', 1000)}
        return 'https://www.sahibinden.com/satilik-arsa/mugla-datca?' + urlencode(filters)
    if job['tool'] != 'sahibinden.read_listing':
        raise JevStopped()
    key = job['arguments']['listing_id']
    if not re.fullmatch(r'\d{8,14}', key):
        raise JevStopped()
    # Only previously observed detail URLs. No model URL or guessed slug.
    with store.transaction() as con:
        for row in con.execute("SELECT payload FROM observations WHERE mode='local' AND tool IN ('sahibinden.search','sahibinden.read_listing') ORDER BY captured_at DESC,id DESC"):
            data = json.loads(row['payload'])
            for item in data.get('listings', [data]):
                if item.get('listing_id') == key and item.get('url'):
                    return Listing.model_validate({k: item[k] for k in ('listing_id','title','url')}).url
    raise JevStopped()


def verify_receipt(job, raw):
    from verda.agency.browser import source_url, Listing, SearchReceipt, BLOCKERS
    if raw.get('blocker') in BLOCKERS:
        raise JevStopped(raw['blocker'])
    url = raw.get('url', '')
    if '/giris' in url or urlparse(url).hostname == 'secure.sahibinden.com':
        raise JevStopped('session_lost')
    source_url(url)
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
    if params.get('pagingOffset', ['0']) != ['0']:
        raise ValueError('Only the first page is supported')
    rows = raw['listings']
    total = raw['total_reported']
    if type(total) is not int or total < len(rows) or (total > 0 and not rows):
        raise ValueError('Unverified results')
    if len({row['listing_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate source rows')
    result = SearchReceipt.model_validate({'source_url':url,'filters':filters,'total_reported':total,
        'page':1,'has_more':total>len(rows),'listings':rows}).model_dump()
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
    if left.path.rstrip('/') != right.path.rstrip('/'):
        return False
    a, b = parse_qs(left.query), parse_qs(right.query)
    if left.path.startswith('/satilik-arsa/'):
        for params in (a,b):
            if params.pop('pagingOffset',['0']) != ['0']:
                return False
            for key in ('price_max','a507_min'):
                if len(params.get(key,[])) != 1:
                    return False
                params[key] = [float(params[key][0])]
    return a == b


class JevReader:
    def __init__(self, browser_factory=None, chooser=None, sleep=time.sleep):
        self.browser_factory, self.chooser, self.sleep = browser_factory, chooser, sleep

    def ready(self):
        if not installation_status()['installed']:
            return 'package_missing'
        if not os.environ.get('TYPESAFE_API_KEY'):
            return 'typesafe_key_missing'
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
        history, started = [], time.monotonic()
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
                if '/giris' in page['url'] or urlparse(page['url']).hostname == 'secure.sahibinden.com':
                    raise JevStopped('session_lost')
                source_url(page['url'])
                if not same_target(page['url'],url) or not same_target(raw['url'],url):
                    raise JevStopped()
                progress('observed')
                goal = ('Inspect only this public Sahibinden page. Do not navigate, click, type, log in, or send messages. '
                        'Choose DONE when the requested search results or listing details have loaded. '
                        'Use scrolling or WAIT only if needed to see loaded content. Stop BLOCKED at any access challenge. '
                        'An independent DOM reader will extract and verify the result. Task: ' +
                        ('Datça land search, first page only.' if job['tool']=='sahibinden.search' else 'Read listing ' + job['arguments']['listing_id']))
                try:
                    decision = (self.chooser or choose)(scoped_page(page), goal, history)
                except Exception:
                    progress('model_failed')
                    raise JevStopped() from None
                choice = decision['choice']
                if choice in {'DONE','BLOCKED'}:
                    progress(choice)
                    if choice == 'BLOCKED':
                        raise JevStopped()
                    if not browser.fresh(page):
                        continue
                    result = verify_receipt(job, browser.evaluate(READ_STATE))
                    progress('verified')
                    return result
                action = next((a for a in scoped_page(page)['actions'] if a['id']==choice),None)
                if not action:
                    raise JevStopped()
                self.sleep(2)  # No Jev-speed click loop on Sahibinden.
                try:
                    browser.act(action,page)
                except StalePage:
                    continue
                progress('WAIT' if action['kind']=='wait' else 'SCROLL_DOWN' if action['delta']>0 else 'SCROLL_UP')
                history.append({'action':action['label'],'kind':action['kind'],'text':None,'page_changed':None})
            raise JevStopped()
        finally:
            try:
                browser.close()
            except Exception:
                pass  # Preserve the source result/incident if tab cleanup fails.


def run_worker(store, *, env_file=None, continuous=False, reader=None):
    from verda.agency.browser import BrowserBridge
    load_env(env_file)
    bridge, reader = BrowserBridge(store), reader or JevReader()
    worker = 'jev-' + uuid4().hex
    completed = 0
    def status(reason):
        path = store.path.with_suffix('.jev-status.json')
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'checked_at':time.time(), 'reason':reason}, out)
    reason = reader.ready()
    status(reason)
    if reason:
        return {'driver':'jev','state':'waiting_setup','reason':reason,'completed':0}
    try:
        while True:
            bridge.expire()
            reason = reader.ready()
            status(reason)
            if reason:
                return {'driver':'jev','state':'waiting_setup','reason':reason,'completed':completed}
            bridge.attach(worker,'Browser Use · Jev Ultrafast',driver='jev',ttl=30)
            job = bridge.claim(worker)
            if job:
                # A bounded job is allowed up to ten minutes; idle health lasts 30s.
                bridge.attach(worker,'Browser Use · Jev Ultrafast',driver='jev',ttl=300)
                report = lambda phase: bridge.progress(job,worker,phase)
                try:
                    result = reader.read(target_url(store,job),job,report)
                except JevStopped as error:
                    bridge.finish(job['id'],worker,job['token'],error=error.code)
                except Exception:
                    report('read_failed')
                    bridge.finish(job['id'],worker,job['token'],error='read_failed')
                else:
                    bridge.finish(job['id'],worker,job['token'],result=result)
                completed += 1
            if not continuous:
                return {'driver':'jev','state':'idle','completed':completed}
            time.sleep(5)
    finally:
        bridge.detach(worker)
