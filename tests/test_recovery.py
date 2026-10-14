import urllib.error

from verda.agency import message, recovery


def _store(tmp_path):
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def _open(store, reason, at=1000.0):
    with store.transaction() as con:
        con.execute("INSERT OR IGNORE INTO resources(key) VALUES('tkgm')")
        con.execute("UPDATE resources SET halted=? WHERE key='tkgm'", (reason,))
        con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES('i1','tkgm','local',?,'open',?)", (reason, at))


def _answer(code):
    def fetch(url):
        if code:
            raise urllib.error.HTTPError(url, code, 'x', {}, None)
        return {}
    return fetch


def _state(store):
    with store.transaction() as con:
        return con.execute("SELECT state FROM source_incidents WHERE id='i1'").fetchone()[0]


def test_one_probe_per_hour_and_it_heals_only_on_a_normal_answer(tmp_path):
    store = _store(tmp_path)
    _open(store, 'tkgm_http_403')
    assert recovery.heal(store, fetch=_answer(0), now=1000 + 3599)['due_in'] == 1       # not yet an hour
    assert recovery.heal(store, fetch=_answer(403), now=1000 + 3600)['probe'] == 'blocked'
    assert _state(store) == 'open'
    assert recovery.heal(store, fetch=_answer(0), now=1000 + 3600 + 100)['due_in'] == 3500   # the wait restarts after every probe
    assert recovery.heal(store, fetch=_answer(0), now=1000 + 7200)['probe'] == 'ok'
    assert _state(store) == 'resolved'
    assert recovery.heal(store, fetch=_answer(0), now=1000 + 99999) is None


def test_browser_blocks_are_left_to_the_user(tmp_path):
    store = _store(tmp_path)
    _open(store, 'unusual_access')
    assert recovery.heal(store, fetch=_answer(0), now=1000 + 99999) is None
    assert _state(store) == 'open'


def test_an_unreachable_network_does_not_open_the_source(tmp_path):
    store = _store(tmp_path)
    _open(store, 'tkgm_http_429')
    def down(url):
        raise urllib.error.URLError('offline')
    assert recovery.heal(store, fetch=down, now=1000 + 3600)['probe'] == 'unreachable'
    assert _state(store) == 'open'



def _script(tmp_path):
    path = tmp_path / 'modem-reset'
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)
    return path


def test_the_modem_script_runs_then_the_line_is_probed_and_a_send_in_flight_defers_it(tmp_path):
    store, ran = _store(tmp_path), []
    kw = dict(run=lambda command: ran.append(1) or 0, command=_script(tmp_path), sleep=lambda s: None,
              run_status=lambda command: '{"wan_pppoe_address": "100.1.1.%d"}' % (1 + len(ran)))
    with store.transaction() as con:
        message.ensure(con)
        con.execute("INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,created_at,updated_at) VALUES('o','1','local','[]','t','h','t','f','sending',1,1)")
    assert recovery.reset_modem(store, _answer(0), **kw) == {'modem': 'deferred', 'why': 'send_in_progress'} and not ran
    with store.transaction() as con:
        con.execute('DELETE FROM outbox')
    assert recovery.reset_modem(store, _answer(0), **kw) == {'modem': 'reset', 'probe': 'ok', 'address_before': '100.1.1.1', 'address_after': '100.1.1.2', 'address_changed': True} and len(ran) == 1


def test_without_the_users_script_nothing_runs(tmp_path):
    assert recovery.reset_modem(_store(tmp_path), _answer(0), command=tmp_path / 'missing') == {'modem': 'no_command'}
