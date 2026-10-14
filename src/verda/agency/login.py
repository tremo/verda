"""Signing back in to Sahibinden through "Google ile giriş yap", the way a person clicks through it, unattended.

It only ever uses the Google account tile that is already offered in the user's own Chrome and only for the one expected
account. It never types a password, a code or an e-mail address; any screen that asks for one, a consent step, a security
check, no tile or more than one matching tile is a stop that is reported for the user. Success needs the page header to show
the expected account. The browser actions are injected so the whole flow can be tested without a browser.
"""
import json
import re
import socket
import time
from pathlib import Path

from verda.agency.access import ACCOUNT, account_matches, classify_url, login_state
from verda.facts.text import fold

LOGIN_URL = 'https://secure.sahibinden.com/giris'
GOOGLE_FRAME = """(() => { const f=document.querySelector('iframe[src*="accounts.google.com/gsi/button"]'); if(!f) return null;
  const r=f.getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height}; })()"""
TILES = """(() => [...document.querySelectorAll('[data-identifier],li,div[role=link]')].filter(e => { const b=e.getBoundingClientRect();
  return b.height>30 && b.height<120 && /@/.test(e.innerText||''); }).map(e => { const b=e.getBoundingClientRect();
  return {text:(e.innerText||'').trim(), x:b.x+b.width/2, y:b.y+b.height/2}; }))()"""
# Screens only a person can pass. Google's pages are read whole; Sahibinden's login page has a "Şifre" label of its own.
GOOGLE_STOPS = ('enter your password', 'şifrenizi girin', '2-step', '2 adımlı', 'verify it', 'doğrulama', 'not a robot', 'robot değilim', 'captcha',
                'wants to access', 'erişmek istiyor', 'confirm your recovery', 'unusual activity', 'olağan dışı')
SAHIBINDEN_STOPS = ('doğrulama kodu', 'robot olmadığınızı', 'captcha', 'güvenlik kodu')
ASSIST_REASONS = ('account_not_found', 'google_not_opened', 'login_not_confirmed', 'login_page_changed')   # where a model that can read the page may help
MAX_FAILED_PER_6H = 3
MIN_GAP = 600


def online(host='www.sahibinden.com', port=443, timeout=4):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def shown_name(text):
    """The account name the page header shows. Sahibinden has two header layouts: after the search link on the public pages, and
    right after the logo on the account pages."""
    lines = [l.strip() for l in str(text).split('\n') if l.strip()][:40]
    for i, line in enumerate(lines):
        if line == 'Detaylı Arama' and i + 1 < len(lines) and lines[i + 1] not in ('Giriş Yap', 'Hesap Aç'):
            return lines[i + 1]
    if len(lines) > 2 and lines[0] == 'sahibinden.com' and lines[2].startswith('Ücretsiz'):
        return lines[1]
    return None


class Hands:
    """The few browser actions the flow needs. The worker passes Browser Harness; tests pass a fake."""

    def __init__(self, new_tab, close_tab, switch_tab, js, click, tabs, text_of, front=None, restore=None, emulate=None):
        self.new_tab, self.close_tab, self.switch_tab, self.js, self.click, self.tabs, self.text_of, self.front, self.restore, self.emulate = new_tab, close_tab, switch_tab, js, click, tabs, text_of, front, restore, emulate


def harness_hands():
    from browser_harness import helpers as h
    def new_tab(url):
        target = h.new_tab(url)
        from verda.agency.jev import register_tab
        register_tab(target if isinstance(target, str) else target.get('targetId'))
        return target
    asleep = {'was_idle': False}

    def front():
        import subprocess
        if screen_locked():
            return                                   # nothing to wake: the lock screen would only light up; the page is made to draw instead
        # Google draws its button only on a visible page, and a sleeping display makes every page hidden. If nobody has touched the Mac for a
        # while the display is assumed asleep: it is woken (a real user-activity signal, no faking), and put back to sleep when the attempt ends.
        asleep['was_idle'] = idle_seconds() >= IDLE_AFTER
        subprocess.run(['caffeinate', '-u', '-t', '5'], timeout=10, capture_output=True)
        subprocess.run(['open', '-a', 'Google Chrome'], timeout=10, capture_output=True)      # the browser window itself, not only the tab
        try:
            h.cdp('Page.bringToFront')
        except Exception:
            pass
        time.sleep(2)

    def restore():
        if asleep['was_idle'] and idle_seconds() >= 3:                   # the user did not come back meanwhile
            import subprocess
            subprocess.run(['pmset', 'displaysleepnow'], timeout=10, capture_output=True)
        asleep['was_idle'] = False
    def emulate():
        # A locked screen makes every page "hidden", and Google draws its button only on a visible page. The lock itself is not touched: the page is
        # told it is visible before its scripts run, and loaded again. Only the page's own view of itself changes.
        h.cdp('Emulation.setFocusEmulationEnabled', enabled=True)
        h.cdp('Page.addScriptToEvaluateOnNewDocument', source=VISIBLE_JS)
        h.cdp('Page.reload')
        time.sleep(6)
    return Hands(new_tab=new_tab, close_tab=h.close_tab, switch_tab=h.switch_tab, js=h.js, click=h.click_at_xy, tabs=lambda: h.list_tabs(include_chrome=False),
                 text_of=lambda: h.js('document.body.innerText'), front=front, restore=restore, emulate=emulate)


def _wait(test, seconds, sleep):
    for _ in range(int(seconds / 0.5)):
        value = test()
        if value:
            return value
        sleep(0.5)
    return test()


def _relogin(hands, *, sleep=time.sleep, expected=None, assist=None):
    """One attempt. Returns {ok, reason, detail}; the tabs it opened are closed on success and left for the user on a stop."""
    expected = expected or ACCOUNT['name']
    if not expected:
        return {'ok': False, 'reason': 'account_not_configured', 'detail': 'Beklenen hesap yerel ayarlarda yok (.local/account.json).'}
    opened = []
    def stop(reason, detail=''):
        if assist is not None and reason in ASSIST_REASONS and opened:
            # The scripted path did not fit what is on screen: let a model that reads the page try, within a narrow allow-list.
            helped = _assisted(hands, opened, expected, assist, sleep)
            if helped:
                _close(hands, opened)
                return {'ok': True, 'reason': 'signed_in_assisted', 'detail': helped}
        return {'ok': False, 'reason': reason, 'detail': detail, 'tabs_left_open': len(opened), 'screen': _screen(hands, opened)}
    try:
        tab = _id(hands.new_tab(LOGIN_URL))
        opened.append(tab)
        sleep(4)
        hands.switch_tab(tab)
        text = hands.text_of() or ''
        url = next((t['url'] for t in hands.tabs() if _id(t) == tab), '')
        if classify_url(url) == 'unusual_access':
            return stop('unusual_access', 'Güvenlik denetimi sayfası; elle geçilmeli.')
        if login_state(text) != 'logged_out' and 'Giriş yap' not in text[:300]:
            # The login page sent us on: the session is alive again.
            name = shown_name(text)
            if name and account_matches(name, expected):
                _close(hands, opened)
                return {'ok': True, 'reason': 'already_logged_in', 'detail': name}
        # Google draws its button only while the page is visible. Chrome behind another window counts as hidden, so the tab is brought forward
        # first, and the button is waited for until it has a size (a 0x0 frame would be clicked at an empty spot).
        if hands.front:
            hands.front()
        if hands.emulate and (hands.js('document.visibilityState') == 'hidden'):
            hands.emulate()
        frame = _wait(lambda: (lambda f: f if f and f['w'] > 0 and f['h'] > 0 else None)(hands.js(GOOGLE_FRAME)), 12, sleep)
        if not frame:
            return stop('login_page_changed', '"Google ile giriş yap" düğmesi bulunamadı ya da çizilmedi.')
        before = {_id(t) for t in hands.tabs()}
        chooser = lambda: next((t for t in hands.tabs() if 'accounts.google.com' in t.get('url', '') and _id(t) not in before), None)
        # The frame has a size before the button inside it answers clicks: give it a moment, click, and if nothing opened click once more.
        sleep(3)
        hands.click(frame['x'] + frame['w'] / 2, frame['y'] + frame['h'] / 2)
        google = _wait(chooser, 8, sleep)
        if not google:
            frame = hands.js(GOOGLE_FRAME) or frame
            hands.click(frame['x'] + frame['w'] / 2, frame['y'] + frame['h'] / 2)
            google = _wait(chooser, 15, sleep)
        if not google:
            try:                                    # what the click did lead to, as hosts only (no page content), so a failure explains itself
                fresh = [re.sub(r'^https?://([^/]+).*$', r'\1', t.get('url', '')) for t in hands.tabs() if _id(t) not in before]
                state = hands.js('document.visibilityState')
            except Exception:
                fresh, state = ['?'], '?'
            return stop('google_not_opened', f'Google hesap seçici açılmadı (tıklama sonrası yeni sekmeler: {fresh[:4] or "yok"}; sayfa: {state}; düğme: {int(frame["w"])}x{int(frame["h"])}).')
        gid = _id(google)
        opened.append(gid)
        sleep(2)
        hands.switch_tab(gid)
        page = (hands.text_of() or '').lower()
        if any(word in page for word in GOOGLE_STOPS):
            return stop('needs_user', 'Google şifre, doğrulama ya da izin istiyor.')
        tiles = hands.js(TILES) or []
        mine = [t for t in tiles if fold(expected) in fold(t['text'])]
        if not tiles:
            return stop('account_not_found', 'Hesap kutucuğu görünmüyor.')
        if len({fold(t['text']) for t in mine}) != 1:
            return stop('ambiguous_accounts', 'Beklenen hesap tam olarak bir kez bulunamadı.')
        hands.click(mine[0]['x'], mine[0]['y'])
        sleep(3)
        # success: the Google tab goes away and the Sahibinden tab shows the expected account
        _wait(lambda: not any(_id(t) == gid for t in hands.tabs()), 25, sleep)
        hands.switch_tab(tab)
        name = _wait(lambda: shown_name(hands.text_of() or ''), 20, sleep)
        if name and account_matches(name, expected):
            _close(hands, opened)
            return {'ok': True, 'reason': 'signed_in', 'detail': name}
        if any(word in (hands.text_of() or '').lower() for word in SAHIBINDEN_STOPS):
            return stop('needs_user', 'Giriş sırasında ek doğrulama istendi.')
        return stop('login_not_confirmed', f'Başlıkta beklenen hesap görünmedi ({name or "ad yok"}).')
    except Exception as error:                      # a failed step never raises into the worker loop
        return stop('error', type(error).__name__)


def relogin(hands, **options):
    try:
        return _relogin(hands, **options)
    finally:
        if getattr(hands, 'restore', None):
            hands.restore()


VISIBLE_JS = ("Object.defineProperty(document,'visibilityState',{get:()=>'visible',configurable:true});"
              "Object.defineProperty(document,'hidden',{get:()=>false,configurable:true});document.hasFocus=()=>true;")


def screen_locked():
    """True while the Mac shows its lock screen: a sign-in cannot draw its button then, so nothing is tried (and nothing counted) until it is unlocked."""
    import re
    import subprocess
    try:
        out = subprocess.run(['ioreg', '-n', 'Root', '-d1', '-a'], timeout=10, capture_output=True, text=True).stdout
        return bool(re.search(r'CGSSessionScreenIsLocked</key>\s*<true', out))
    except Exception:
        return False


IDLE_AFTER = 120          # seconds without keyboard or mouse before the display is taken to be asleep


def idle_seconds():
    """Seconds since the last keyboard or mouse input (macOS HIDIdleTime); 0 when it cannot be read, which never puts the display to sleep."""
    import re
    import subprocess
    try:
        out = subprocess.run(['ioreg', '-c', 'IOHIDSystem'], timeout=10, capture_output=True, text=True).stdout
        match = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(match[1]) / 1e9 if match else 0
    except Exception:
        return 0


CHOICES = """(() => [...document.querySelectorAll('[data-identifier],li,div[role=link],button,a[href],[role=button]')].filter(e => { const b=e.getBoundingClientRect();
  return b.width>20 && b.height>16 && (e.innerText||'').trim().length>0 && (e.innerText||'').length<120; }).map(e => { const b=e.getBoundingClientRect();
  return {text:(e.innerText||'').trim(), x:b.x+b.width/2, y:b.y+b.height/2}; }))()"""
ALLOWED_EXTRA = ('devam', 'continue', 'reddet', 'tümünü reddet', 'reject all', 'decline', 'sadece gerekli', 'necessary only')


def _assisted(hands, opened, expected, assist, sleep):
    """Up to a few clicks chosen by the page-reading model. It sees only the expected account tile and plain continue/decline buttons;
    it cannot type, so a password or code screen stays impossible for it. Success still needs the header to show the account."""
    try:
        google = next((t for t in hands.tabs() if 'accounts.google.com' in t.get('url', '')), None)
        target = _id(google) if google else opened[0]
        hands.switch_tab(target)
        page = (hands.text_of() or '').lower()
        if any(word in page for word in GOOGLE_STOPS + SAHIBINDEN_STOPS):
            return None
        allow = lambda label: fold(expected) in fold(label) or fold(label).strip() in ALLOWED_EXTRA
        assist(target, allow)
        sleep(3)
        hands.switch_tab(opened[0])
        name = _wait(lambda: shown_name(hands.text_of() or ''), 15, sleep)
        return name if name and account_matches(name, expected) else None
    except Exception:
        return None


def _screen(hands, opened):
    """What the page in front shows, for the agent that decides the next click: address host, a short text and the allowed choices."""
    try:
        google = next((t for t in hands.tabs() if 'accounts.google.com' in t.get('url', '')), None)
        target = _id(google) if google else (opened[0] if opened else None)
        if target is None:
            return None
        hands.switch_tab(target)
        url = next((t.get('url', '') for t in hands.tabs() if _id(t) == target), '')
        text = (hands.text_of() or '').strip().replace('\n\n', '\n')
        choices = [c['text'].split('\n')[0] for c in (hands.js(CHOICES) or [])][:12]
        return {'host': re.sub(r'^https?://([^/]+).*$', r'\1', url), 'text': text[:600], 'choices': choices}
    except Exception:
        return None


def _id(tab):
    return tab if isinstance(tab, str) else (tab.get('targetId') or tab.get('target_id'))


def _close(hands, opened):
    for target in opened:
        try:
            hands.close_tab(target)
        except Exception:
            pass


# ---- attempts, rate limit, escalation ------------------------------------------------------------------------------
HARD_STOPS = ('needs_user', 'unusual_access')
LOCKED_SKIPS = False         # a locked screen no longer stops an attempt: the page is made to draw (see Hands.emulate)
MAX_AGENT_TASKS = 2
HOUR = 3600


def _ensure(con):
    con.execute('CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, ok INTEGER NOT NULL, reason TEXT NOT NULL, detail TEXT)')
    if 'via' not in {r[1] for r in con.execute('PRAGMA table_info(login_attempts)')}:
        con.execute("ALTER TABLE login_attempts ADD COLUMN via TEXT NOT NULL DEFAULT 'code'")


def record(store, result, now=None, via='code'):
    now = time.time() if now is None else now
    with store.transaction() as con:
        _ensure(con)
        con.execute('INSERT INTO login_attempts(at,ok,reason,detail,via) VALUES(?,?,?,?,?)', (now, int(result['ok']), result['reason'], (result.get('detail') or '')[:200], via))


def last_attempt(store):
    with store.transaction() as con:
        _ensure(con)
        row = con.execute('SELECT at,ok,reason,detail,via FROM login_attempts ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None


def may_try(store, now=None):
    """The scripted attempt: not too often, not again and again, and not again by itself when the screen needs a person."""
    now = time.time() if now is None else now
    if LOCKED_SKIPS and screen_locked():
        return False
    with store.transaction() as con:
        _ensure(con)
        rows = [dict(r) for r in con.execute("SELECT at,ok,reason FROM login_attempts WHERE via='code' AND at>? ORDER BY id DESC", (now - 6 * HOUR,))]
    if rows and now - rows[0]['at'] < MIN_GAP:
        return False
    if rows and not rows[0]['ok'] and rows[0]['reason'] in HARD_STOPS + ('ambiguous_accounts', 'account_not_found'):
        return False
    return sum(1 for r in rows if not r['ok']) < MAX_FAILED_PER_6H


def open_incident(store):
    with store.transaction() as con:
        row = con.execute("SELECT id,created_at FROM source_incidents WHERE state='open' AND resource='sahibinden' AND reason='session_lost' ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None


def resolve_open(store, result, now=None):
    """A successful sign-in reopens the source, whoever achieved it (the code or the operator agent)."""
    incident = open_incident(store)
    if result.get('ok') and incident:
        store.resolve_source(incident['id'], f"Google ile otomatik yeniden giriş yapıldı; başlıkta '{result.get('detail')}' doğrulandı.", now=now)
        return True
    return False


def escalate(store, incident_id, result, now=None):
    """The scripted path failed on something a page-reading model may handle: hand it to the sahibinden operator (Verda's own agent)."""
    if result.get('reason') in HARD_STOPS or result.get('ok'):
        return None
    with store.transaction() as con:
        n = con.execute("SELECT count(*) FROM inbox WHERE request_key LIKE ?", (f'relogin:{incident_id}:%',)).fetchone()[0]
    if n >= MAX_AGENT_TASKS:
        return None
    return store.submit(agent='sahibinden', objective='Sahibinden oturumunu yeniden aç', priority=90, request_key=f'relogin:{incident_id}:{n + 1}',
                        inputs={'routine': 'relogin', 'incident': incident_id, 'scripted_result': result.get('reason'), 'screen': result.get('screen')}, now=now)


def heal(store, hands, *, sleep=time.sleep, now=None, assist=None):
    """If Sahibinden is blocked because the session was lost, sign in again by script and, if the screen is not what the script expects,
    hand the rest to the operator agent. Returns None when there is nothing to do."""
    now = time.time() if now is None else now
    incident = open_incident(store)
    if incident is None or not may_try(store, now):
        return None
    if not online():
        return {'ok': False, 'reason': 'offline', 'detail': 'İnternet yok; bağlantı gelince yeniden denenecek.', 'retry': True}
    result = relogin(hands, sleep=sleep, assist=assist)
    record(store, result, now)
    if result['ok']:
        resolve_open(store, result, now)
    else:
        escalate(store, incident['id'], result, now)
    return result


def status(store, now=None):
    """For the panel. 'recovering' while the system still has attempts left, 'failed' when it has run out and the user must act."""
    now = time.time() if now is None else now
    incident = open_incident(store)
    if incident is None:
        return {'phase': 'none'}
    with store.transaction() as con:
        _ensure(con)
        rows = [dict(r) for r in con.execute('SELECT at,ok,reason,detail,via FROM login_attempts WHERE at>=? ORDER BY id', (incident['created_at'] - 60,))]
        tasks = [dict(r) for r in con.execute("SELECT state FROM inbox WHERE request_key LIKE ?", (f"relogin:{incident['id']}:%",))]
    last = rows[-1] if rows else None
    open_tasks = [t for t in tasks if t['state'] in ('queued', 'running', 'waiting_browser', 'waiting_children')]
    code_failed = sum(1 for r in rows if r['via'] == 'code' and not r['ok'])
    hard = bool(last) and not last['ok'] and last['reason'] in HARD_STOPS
    agents_spent = len(tasks) >= MAX_AGENT_TASKS and not open_tasks
    exhausted = hard and not open_tasks or code_failed >= MAX_FAILED_PER_6H and not open_tasks or agents_spent or now - incident['created_at'] > 24 * HOUR
    return {'phase': 'failed' if exhausted else 'recovering', 'incident': incident['id'], 'since': incident['created_at'], 'attempts': len(rows),
            'last': last, 'agent_tasks': len(tasks)}


def click_choice(hands, label, *, expected=None, sleep=time.sleep):
    """One click chosen by the operator agent. Code decides whether the label is allowed: the expected account tile, or a plain
    continue / decline button. Consent, passwords, other accounts and everything else are refused here, whatever the agent says."""
    expected = expected or ACCOUNT['name']
    if not expected:                                     # an empty name is inside every label: never let that allow a click
        return {'ok': False, 'reason': 'account_not_configured', 'detail': 'Beklenen hesap yerel ayarlarda yok.'}
    wanted = fold(label)
    if not (fold(expected) in wanted or wanted in ALLOWED_EXTRA):
        return {'ok': False, 'reason': 'label_not_allowed', 'detail': 'Bu seçeneğe tıklanamaz.'}
    google = next((t for t in hands.tabs() if 'accounts.google.com' in t.get('url', '')), None)
    login_tab = next((t for t in hands.tabs() if 'secure.sahibinden.com/giris' in t.get('url', '')), None)
    target = _id(google or login_tab) if (google or login_tab) else None
    if target is None:
        return {'ok': False, 'reason': 'no_login_tab', 'detail': 'Açık bir giriş sekmesi yok.'}
    hands.switch_tab(target)
    page = (hands.text_of() or '').lower()
    if any(word in page for word in GOOGLE_STOPS + SAHIBINDEN_STOPS):
        return {'ok': False, 'reason': 'needs_user', 'detail': 'Ekran şifre, doğrulama ya da izin istiyor.'}
    matches = [c for c in (hands.js(CHOICES) or []) if fold(c['text'].split('\n')[0]) == wanted or (fold(expected) in wanted and fold(expected) in fold(c['text']))]
    if len({fold(c['text']) for c in matches}) != 1:
        return {'ok': False, 'reason': 'choice_not_unique', 'detail': 'Seçenek ekranda tam olarak bir kez bulunamadı.'}
    hands.click(matches[0]['x'], matches[0]['y'])
    sleep(3)
    return check_signed_in(hands, expected, sleep)


def check_signed_in(hands, expected=None, sleep=time.sleep):
    """Is the account signed in now? Looks at the open Sahibinden tabs, not at what the last click was supposed to do."""
    expected = expected or ACCOUNT['name']
    if not expected:
        return {'ok': False, 'reason': 'account_not_configured', 'detail': 'Beklenen hesap yerel ayarlarda yok.'}
    for tab in hands.tabs():
        if 'sahibinden.com' in tab.get('url', ''):
            hands.switch_tab(_id(tab))
            name = shown_name(hands.text_of() or '')
            if name and account_matches(name, expected):
                return {'ok': True, 'reason': 'signed_in', 'detail': name}
    return {'ok': False, 'reason': 'not_signed_in', 'detail': 'Başlıkta beklenen hesap görünmüyor.', 'screen': _screen(hands, [])}
