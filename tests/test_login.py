import pytest

from verda.agency import login
from verda.agency.login import Hands, heal, may_try, relogin, shown_name
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore

LOGGED_OUT = 'sahibinden.com anasayfasına dön\n\nGiriş yap\nE-posta\nŞifre\nE-posta ile giriş yap\nGoogle ile giriş yap\nApple ile giriş yap'
LOGGED_IN = 'sahibinden.com anasayfasına dön\n\nDetaylı Arama\nKaan Y.\nMesajlar'


class Browser:
    """A scripted Chrome: the login page, a Google chooser (or something else), and what the page shows afterwards."""

    def __init__(self, *, chooser='Choose an account\nDeniz Kaan Yıldırım\nornek@example.com', tiles=1, frame=True, after=LOGGED_IN, start=LOGGED_OUT, url=login.LOGIN_URL):
        self.tabs_by_id, self.current, self.clicks, self.closed = {}, None, [], []
        self.chooser, self.tile_count, self.frame, self.after, self.start, self.url = chooser, tiles, frame, after, start, url
        self.texts = {}

    def new_tab(self, url):
        self.tabs_by_id['S'] = {'targetId': 'S', 'url': self.url}
        self.texts['S'] = self.start
        return 'S'

    def close_tab(self, target):
        self.closed.append(target); self.tabs_by_id.pop(target, None)

    def switch_tab(self, target): self.current = target
    def tabs(self): return list(self.tabs_by_id.values())
    def text_of(self): return self.texts[self.current]

    def js(self, code):
        if code == login.GOOGLE_FRAME:
            return {'x': 400, 'y': 446, 'w': 420, 'h': 44} if self.frame else None
        if code == login.TILES:
            return [{'text': 'Deniz Kaan Yıldırım\nornek@example.com', 'x': 295, 'y': 242}] * self.tile_count if self.tile_count == 1 else \
                   [{'text': 'Deniz Kaan Yıldırım\nornek@example.com', 'x': 295, 'y': 242}, {'text': 'Deniz Kaan Yıldırım\nbaska@example.com', 'x': 295, 'y': 320}]
        raise AssertionError(code)

    def click(self, x, y):
        self.clicks.append((x, y))
        if len(self.clicks) == 1:                                   # the Google button opens the chooser
            self.tabs_by_id['G'] = {'targetId': 'G', 'url': 'https://accounts.google.com/v3/signin/accountchooser'}
            self.texts['G'] = self.chooser
        else:                                                       # the tile: Google closes, the page shows the account
            self.tabs_by_id.pop('G', None)
            self.texts['S'] = self.after

    def hands(self):
        return Hands(self.new_tab, self.close_tab, self.switch_tab, self.js, self.click, self.tabs, self.text_of)


import pytest


@pytest.fixture(autouse=True)
def _unlocked(monkeypatch):
    from verda.agency import login
    monkeypatch.setattr(login, 'screen_locked', lambda: False)        # the tests do not depend on the real screen


def go(browser, **kw):
    return relogin(browser.hands(), sleep=lambda s: None, **kw)


def test_the_header_name_is_read_in_both_header_layouts_and_never_from_a_logged_out_page():
    assert shown_name(LOGGED_IN) == 'Kaan Y.' and shown_name(LOGGED_OUT) is None
    assert shown_name('sahibinden.com\nKaan Y.\nÜcretsiz* İlan Ver\nÖzet\nİlan Yönetimi') == 'Kaan Y.'            # the account pages
    assert shown_name('sahibinden.com anasayfasına dön\n\nDetaylı Arama\nGiriş Yap\nHesap Aç\nÜcretsiz* İlan Ver') is None


def test_it_signs_in_through_the_one_expected_account_tile_and_closes_what_it_opened():
    b = Browser()
    out = go(b)
    assert out == {'ok': True, 'reason': 'signed_in', 'detail': 'Kaan Y.'}
    assert len(b.clicks) == 2 and b.clicks[0] == (610.0, 468.0) and b.clicks[1] == (295, 242)       # the Google button, then the tile
    assert sorted(b.closed) == ['G', 'S'] or b.closed == ['S']                                      # nothing is left open


@pytest.mark.parametrize('kwargs,reason', [
    ({'chooser': 'Enter your password\nDeniz Kaan Yıldırım'}, 'needs_user'),                       # Google asks for a password
    ({'chooser': 'Verify it\'s you\n2-Step Verification'}, 'needs_user'),
    ({'tiles': 2}, 'ambiguous_accounts'),                                                          # two tiles for the same name: no guessing
    ({'frame': False}, 'login_page_changed'),                                                      # the page changed: no clicking blindly
    ({'after': LOGGED_OUT}, 'login_not_confirmed'),                                                # the header never showed the account
    ({'after': 'sahibinden.com\n\nDetaylı Arama\nAli V.\nMesajlar'}, 'login_not_confirmed'),         # a different account
    ({'url': 'https://www.sahibinden.com/cs/checkLoading'}, 'unusual_access'),                    # a security check is never passed
])
def test_any_unexpected_screen_is_a_stop_that_is_reported_and_leaves_the_tabs_for_the_user(kwargs, reason):
    b = Browser(**kwargs)
    out = go(b)
    assert out['ok'] is False and out['reason'] == reason
    if reason == 'needs_user':
        assert len(b.clicks) == 1                                                                    # it never clicked the tile


def test_a_page_that_is_already_logged_in_needs_no_click():
    b = Browser(start=LOGGED_IN)
    out = go(b)
    assert out['ok'] and out['reason'] == 'already_logged_in' and b.clicks == []


def _store(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _incident(store):
    import sqlite3
    con = sqlite3.connect(store.path)
    with con:
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc','sahibinden','local','session_lost','open',1.0)")
        con.execute("INSERT OR IGNORE INTO resources(key) VALUES('sahibinden')")
        con.execute("UPDATE resources SET halted='session_lost' WHERE key='sahibinden'")
    con.close()


def test_heal_reopens_the_source_after_a_successful_sign_in_and_rate_limits_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(login, 'online', lambda *a, **k: True)
    store = _store(tmp_path)
    assert heal(store, Browser().hands(), sleep=lambda s: None, now=10_000.0) is None                   # no incident: nothing to do
    _incident(store)
    out = heal(store, Browser().hands(), sleep=lambda s: None, now=10_000.0)
    assert out['ok']
    with store.transaction() as con:
        assert con.execute("SELECT state FROM source_incidents WHERE id='inc'").fetchone()[0] == 'resolved'
        assert con.execute("SELECT halted FROM resources WHERE key='sahibinden'").fetchone()[0] is None
    # a second incident: a screen that needs the user is tried once, then left alone until the user acts
    import sqlite3
    c = sqlite3.connect(store.path)
    with c:
        c.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('inc2','sahibinden','local','session_lost','open',2.0)")
        c.execute("UPDATE resources SET halted='session_lost' WHERE key='sahibinden'")
    c.close()
    assert heal(store, Browser(chooser='Enter your password').hands(), sleep=lambda s: None, now=20_000.0)['reason'] == 'needs_user'
    assert not may_try(store, now=20_000.0 + 3 * 3600)                                                   # not again by itself
    assert heal(store, Browser().hands(), sleep=lambda s: None, now=20_000.0 + 3 * 3600) is None


def test_without_internet_it_waits_and_does_not_count_a_failed_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(login, 'online', lambda *a, **k: False)
    store = _store(tmp_path)
    _incident(store)
    out = heal(store, Browser().hands(), sleep=lambda s: None, now=5000.0)
    assert out['reason'] == 'offline' and login.last_attempt(store) is None


def test_when_the_scripted_path_does_not_fit_the_page_a_model_may_click_only_allowed_things(monkeypatch):
    # after the tile click the page shows an extra "Devam" step: the scripted path reports login_not_confirmed, the model clicks it
    b = Browser(after=LOGGED_OUT)
    seen = {}

    def assist(target, allow):
        seen['allowed'] = [allow('Deniz Kaan Yıldırım'), allow('Devam'), allow('Tümünü reddet'), allow('İzin ver'), allow('Hesabı sil'), allow('Başka bir hesap kullan')]
        b.texts['S'] = LOGGED_IN                                   # the model's click got it through
    out = relogin(b.hands(), sleep=lambda s: None, assist=assist)
    assert out['ok'] and out['reason'] == 'signed_in_assisted' and out['detail'] == 'Kaan Y.'
    assert seen['allowed'] == [True, True, True, False, False, False]                        # consent and anything unexpected stay out of reach


def test_the_model_is_never_asked_when_the_screen_needs_a_person():
    b = Browser(chooser='Enter your password\nDeniz Kaan Yıldırım')
    called = []
    out = relogin(b.hands(), sleep=lambda s: None, assist=lambda t, a: called.append(1))
    assert out['reason'] == 'needs_user' and not called


def test_the_panel_hides_a_lost_session_while_the_system_still_has_attempts_and_shows_it_when_they_run_out(tmp_path, monkeypatch):
    monkeypatch.setattr('verda.agency.attention.watchdog_items', lambda store, mode, now: [])     # topics of the session only
    from verda.agency.attention import attention
    monkeypatch.setattr(login, 'online', lambda *a, **k: True)
    store = _store(tmp_path)
    _incident(store)
    quiet = {'skipped_ids': {}}
    first = attention(store, now=100.0, queue=quiet)
    assert first['count'] == 0 and [i['kind'] for i in first['items']] == ['session_recovering']      # in progress: only an info line
    # the script fails on a screen a model may handle: the operator agent gets the job, still not the user's concern
    out = heal(store, Browser(after=LOGGED_OUT).hands(), sleep=lambda s: None, now=200.0)
    assert out['reason'] == 'login_not_confirmed'
    with store.transaction() as con:
        assert con.execute("SELECT count(*) FROM inbox WHERE request_key LIKE 'relogin:inc:%'").fetchone()[0] == 1
        task = con.execute("SELECT agent,inputs FROM inbox WHERE request_key='relogin:inc:1'").fetchone()
    assert task['agent'] == 'sahibinden' and '"routine":"relogin"' in task['inputs'].replace(' ', '')
    assert attention(store, now=300.0, queue=quiet)['count'] == 0
    # a screen that needs a person is not handed to a model and shows at once
    heal_store = _store(tmp_path / 'b') if False else None
    login.record(store, {'ok': False, 'reason': 'needs_user', 'detail': 'Google şifre istiyor.'}, now=400.0)
    with store.transaction() as con:
        con.execute("UPDATE inbox SET state='complete' WHERE request_key LIKE 'relogin:%'")
    shown = attention(store, now=500.0, queue=quiet)
    assert shown['count'] == 1 and shown['items'][0]['kind'] == 'source_blocked' and 'needs_user' in shown['items'][0]['detail']


def test_a_screen_that_needs_a_person_is_never_escalated_to_the_agent(tmp_path):
    store = _store(tmp_path)
    _incident(store)
    assert login.escalate(store, 'inc', {'ok': False, 'reason': 'needs_user'}) is None
    assert login.escalate(store, 'inc', {'ok': False, 'reason': 'login_not_confirmed'}, now=1.0) is not None
    assert login.escalate(store, 'inc', {'ok': False, 'reason': 'login_not_confirmed'}, now=2.0) is not None
    assert login.escalate(store, 'inc', {'ok': False, 'reason': 'login_not_confirmed'}, now=3.0) is None      # two agent tasks per incident


def test_a_choice_outside_the_allow_list_is_refused_by_code_whatever_the_agent_asks():
    b = Browser()
    b.tabs_by_id['G'] = {'targetId': 'G', 'url': 'https://accounts.google.com/v3/signin/accountchooser'}
    b.texts['G'] = 'Choose an account'
    b.current = 'G'
    b.js = lambda code: [{'text': 'İzin ver', 'x': 1, 'y': 1}, {'text': 'Deniz Kaan Yıldırım\nornek@example.com', 'x': 2, 'y': 2}]
    for label in ('İzin ver', 'Başka bir hesap kullan', 'Şifre', 'Hesabı sil'):
        assert login.click_choice(b.hands(), label, sleep=lambda s: None)['reason'] == 'label_not_allowed'
    assert b.clicks == []


def test_stale_automation_tabs_are_closed_and_only_those(tmp_path):
    from verda.agency.jev import close_stale_tabs, register_tab
    path = tmp_path / 'tabs.json'
    import json, time
    path.write_text(json.dumps([{'id': 'old1', 'at': 100.0}, {'id': 'old2', 'at': 200.0}, {'id': 'fresh', 'at': 9_900.0}]))
    closed = []
    assert close_stale_tabs(path, closer=closed.append, now=10_000.0) == 2
    assert closed == ['old1', 'old2'] and [e['id'] for e in json.loads(path.read_text())] == ['fresh']


def test_the_display_is_put_back_after_every_attempt_even_one_that_fails():
    from verda.agency import login
    log = []
    hands = login.Hands(new_tab=lambda url: (_ for _ in ()).throw(RuntimeError('no browser')), close_tab=lambda t: None, switch_tab=lambda t: None,
                        js=lambda s: None, click=lambda x, y: None, tabs=lambda: [], text_of=lambda: '', front=lambda: log.append('front'), restore=lambda: log.append('restore'))
    assert login.relogin(hands, sleep=lambda s: None)['ok'] is False
    assert log[-1] == 'restore'


def test_nothing_is_tried_or_counted_while_the_screen_is_locked(monkeypatch, tmp_path):
    from verda.agency import login
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    monkeypatch.setattr(login, 'LOCKED_SKIPS', True)
    monkeypatch.setattr(login, 'screen_locked', lambda: True)
    assert login.may_try(store, now=1000.0) is False
    monkeypatch.setattr(login, 'screen_locked', lambda: False)
    assert login.may_try(store, now=1000.0) is True
