from verda.agency.jev import own_tab


def test_verda_reuses_its_own_tab_and_opens_a_window_only_when_that_tab_is_gone(tmp_path):
    # 25 Nov 2026: a new tab per job landed in the owner's working window. Verda keeps one tab in a window of its own.
    calls, pages, made = [], [{'targetId': 'USER', 'type': 'page'}], []

    def cdp(method, **params):
        calls.append((method, params))
        if method == 'Target.getTargets':
            return {'targetInfos': list(pages)}
        if method == 'Target.createTarget':
            made.append(1)
            pages.append({'targetId': f'V{len(made)}', 'type': 'page'})
            return {'targetId': pages[-1]['targetId']}
    path = tmp_path / 'tab.json'
    first = own_tab(cdp, path)
    assert first == 'V1' and ('Target.createTarget', {'url': 'about:blank', 'newWindow': True}) in calls
    calls.clear()
    assert own_tab(cdp, path) == 'V1' and not any(m == 'Target.createTarget' for m, _ in calls)      # still open: the same tab, no new window
    pages[:] = [p for p in pages if p['targetId'] != 'V1']                                          # the owner closed Verda's window
    assert own_tab(cdp, path) == 'V2'
    path.write_text('not json')
    assert own_tab(cdp, path) == 'V3'                                                               # a broken record makes a new tab, never fails


# 26 Nov 2026 review of PR #54: a job let go of Verda's tab but kept its CDP session when setup or the read failed.
import sys
import types

import pytest

from verda.agency import jev
from verda.agency.jev import InboxReader, JevReader, JevStopped, ThreadReader, visible_browser


class FakeBrowser:
    """Stands in for jev_ultrafast.browser.Browser: call() and observe() go to the session's cdp."""
    def call(self, method, **params):
        return self._cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        return self.call('Runtime.evaluate', expression=expression).get('value')

    def observe(self, screenshot=True):
        return self._cdp('observe', session_id=self.session)


@pytest.fixture
def chrome(monkeypatch):
    """A fake harness: records every CDP call; `answers[method]` is a value, an exception or a function of the params."""
    calls, answers = [], {'Target.attachToTarget': {'sessionId': 'S1'}, 'Page.navigate': {'frameId': 'F'},
                          'Runtime.evaluate': {'value': 'complete'}}

    def cdp(method, **params):
        calls.append((method, params))
        answer = answers.get(method, {})
        if isinstance(answer, BaseException):
            raise answer
        return answer(params) if callable(answer) else answer
    FakeBrowser._cdp = staticmethod(cdp)
    monkeypatch.setitem(sys.modules, 'jev_ultrafast.browser', types.SimpleNamespace(Browser=FakeBrowser, StalePage=ValueError))
    monkeypatch.setitem(sys.modules, 'jev_ultrafast.model', types.SimpleNamespace(choose=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, 'browser_harness.admin', types.SimpleNamespace(ensure_daemon=lambda: None))
    monkeypatch.setitem(sys.modules, 'browser_harness.helpers', types.SimpleNamespace(cdp=cdp))
    monkeypatch.setattr(jev, 'own_tab', lambda cdp: 'VTAB')
    monkeypatch.setattr(jev, 'wait_loaded', lambda evaluate: evaluate('document.readyState') == 'complete')
    return types.SimpleNamespace(calls=calls, answers=answers, methods=lambda: [m for m, _ in calls])


def detaches(chrome):
    return [p for m, p in chrome.calls if m == 'Target.detachFromTarget']


@pytest.mark.parametrize('error', [TimeoutError('slow'), RuntimeError('gone'), ConnectionError('cut'), KeyboardInterrupt()])
def test_a_failed_navigation_lets_go_of_the_session_and_keeps_the_first_error(chrome, error):
    chrome.answers['Page.navigate'] = error
    with pytest.raises(type(error)):
        visible_browser('https://example.test/')
    assert [d['sessionId'] for d in detaches(chrome)] == ['S1'] and 'Target.closeTarget' not in chrome.methods()


@pytest.mark.parametrize('reply', [{'frameId': 'F', 'errorText': 'net::ERR_NAME_NOT_RESOLVED'}, {}, None])
def test_a_navigation_that_says_it_failed_or_does_not_answer_is_not_read(chrome, reply):
    chrome.answers['Page.navigate'] = lambda params: reply
    with pytest.raises(JevStopped) as stop:
        visible_browser('https://example.test/')
    assert stop.value.code == 'read_failed' and len(detaches(chrome)) == 1
    assert 'Runtime.evaluate' not in chrome.methods()                     # the previous page is never read as if it were the new one


def test_a_lost_connection_while_waiting_for_the_page_still_lets_go(chrome):
    chrome.answers['Runtime.evaluate'] = ConnectionError('cut')
    with pytest.raises(ConnectionError):
        visible_browser('https://example.test/')
    assert len(detaches(chrome)) == 1


def test_when_attaching_fails_there_is_no_session_to_let_go_and_no_tab_is_closed(chrome):
    chrome.answers['Target.attachToTarget'] = RuntimeError('refused')
    with pytest.raises(RuntimeError):
        visible_browser('https://example.test/')
    assert not detaches(chrome) and 'Target.closeTarget' not in chrome.methods()


def test_a_failing_detach_does_not_replace_the_navigation_error(chrome, caplog):
    chrome.answers['Page.navigate'] = TimeoutError('slow')
    chrome.answers['Target.detachFromTarget'] = ConnectionError('secret ws://127.0.0.1 detail')
    with pytest.raises(TimeoutError):
        visible_browser('https://example.test/')
    assert 'ConnectionError' in caplog.text and 'secret' not in caplog.text and 'S1' not in caplog.text


def test_close_detaches_once_keeps_the_tab_and_a_closed_object_sends_nothing(chrome):
    browser = visible_browser('https://example.test/')
    assert detaches(chrome) == [] and browser.close_keeps_tab
    browser.close()
    browser.close()
    assert len(detaches(chrome)) == 1 and detaches(chrome)[0]['_response_timeout'] == 5
    assert 'Target.closeTarget' not in chrome.methods()
    sent = len(chrome.calls)
    for use in (lambda: browser.evaluate('1'), lambda: browser.call('Input.dispatchMouseEvent'), lambda: browser.observe()):
        with pytest.raises(JevStopped):
            use()
    assert len(chrome.calls) == sent                                      # never falls back to the harness's default tab


class Tab:
    """A reader's browser whose read fails; `close_keeps_tab` like Verda's own tab, or not like the plain one."""
    def __init__(self, keeps, error):
        self.close_keeps_tab, self.error, self.closed = keeps, error, 0

    def evaluate(self, expression):
        raise self.error

    def observe(self, screenshot=True):
        raise self.error

    def close(self):
        self.closed += 1


def reader_runs():
    yield 'inbox', lambda factory: InboxReader(browser_factory=factory, keep_open=True).read(None, {'arguments': {}}, lambda *a: None)
    yield 'thread', lambda factory: ThreadReader(browser_factory=factory, keep_open=True).read(None, {'arguments': {'listing_id': '1', 'row': {'title': 'Bir ilan', 'counterpart': 'Satıcı', 'last_at_text': 'Dün'}}}, lambda *a: None)
    yield 'listing', lambda factory: JevReader(browser_factory=factory, keep_open=True).read('https://example.test/', {'arguments': {}}, lambda *a: None)


@pytest.mark.parametrize('name,run', list(reader_runs()))
@pytest.mark.parametrize('error', [JevStopped('captcha'), JevStopped('unusual_access'), ConnectionError('cut')])
def test_a_failed_read_in_verdas_tab_lets_go_of_the_session_but_a_plain_tab_stays_for_the_user(chrome, name, run, error):
    kept = Tab(True, error)
    with pytest.raises(type(error)) as raised:
        run(lambda url: kept)
    assert kept.closed == 1                                               # Verda's tab stays open anyway; only the session goes
    if isinstance(error, JevStopped):
        assert raised.value.code == error.code                            # the security stop stays a security stop
    plain = Tab(False, error)
    with pytest.raises(type(error)):
        run(lambda url: plain)
    assert plain.closed == 0                                              # unchanged: a security check page is left for the user


def test_a_close_that_fails_after_a_security_stop_keeps_the_stop(chrome):
    class Failing(Tab):
        def close(self):
            raise ConnectionError('cut')
    with pytest.raises(JevStopped) as raised:
        InboxReader(browser_factory=lambda url: Failing(True, JevStopped('captcha')), keep_open=True).read(None, {'arguments': {}}, lambda *a: None)
    assert raised.value.code == 'captcha'
