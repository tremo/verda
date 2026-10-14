"""A source stopped by an HTTP rate answer (403/429) heals on its own, slowly and without evasion (user's decision, 9 Oct 2026).

Every hour the system sends ONE ordinary read-only request (same client, no workaround, no faster pace). A normal answer closes the
incident; another 403/429 just waits for the next hour. Browser blocks (unusual_access, captcha) are not handled here: those need
the user to sign in.
"""
import json
import os
import subprocess
import time
import urllib.error
from pathlib import Path

from verda.agency import message, tkgm

WAIT = 3600
PROBE_URL = f'{tkgm.BASE}/parsel/36.7268000/27.6834000/'      # a point on the peninsula; only the status of the answer matters


def status_of(fetch):
    """'ok' | 'blocked' | 'unreachable'. Any answer other than 403/429, including 404, means the door is open."""
    try:
        fetch(PROBE_URL)
    except urllib.error.HTTPError as error:
        return 'blocked' if error.code in tkgm.RATE_CODES else 'ok'
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 'unreachable'
    return 'ok'


# Daily limit per IP (TKGM's own written answer, 9 Oct 2026): "restart the modem to renew the IP". The command is the user's own local script;
# the system never sees modem credentials. WHEN to restart is the manager agent's decision (its prompt carries the user's windows), not code.
RESET_COMMAND = Path(os.environ.get('VERDA_MODEM_RESET', '.local/modem-reset'))


def _sending(con):
    message.ensure(con)
    return con.execute("SELECT 1 FROM outbox WHERE state='sending' LIMIT 1").fetchone() is not None or \
           con.execute("SELECT 1 FROM tool_calls WHERE tool='sahibinden.send_message' AND state IN ('started','uncertain') LIMIT 1").fetchone() is not None


def _load(con, key, default):
    row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _store_value(con, key, value, now):
    con.execute('INSERT INTO runtime_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                (key, json.dumps(value), now))


def _address(command, run_status):
    """The line's outward address from the script's `status`, or None. Only the address is kept, never the rest of the answer."""
    try:
        out = run_status(command)
        return json.loads(out).get('wan_pppoe_address') if out else None
    except (ValueError, subprocess.TimeoutExpired, OSError, AttributeError):
        return None


def _status_output(command):
    return subprocess.run([str(command), 'status'], timeout=60, capture_output=True, text=True).stdout


def reset_modem(store, fetch=tkgm.fetch_json, run=None, command=None, sleep=time.sleep, now=None, run_status=_status_output):
    """Run the user's modem-restart script, unless a message is being sent (the line would drop mid-send). A capability, not a schedule:
    who calls it, and when, is the caller's decision. Behind carrier-grade NAT the address may stay the same, so before/after are reported."""
    now = time.time() if now is None else now
    command = RESET_COMMAND if command is None else Path(command)
    with store.transaction() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        if _sending(con):
            return {'modem': 'deferred', 'why': 'send_in_progress'}
        if not command.is_file() or not os.access(command, os.X_OK):
            return {'modem': 'no_command'}
        _store_value(con, 'modem:resets', _load(con, 'modem:resets', [])[-49:] + [now], now)
    before = _address(command, run_status)
    run = run or (lambda c: subprocess.run([str(c), 'reboot'], timeout=180, capture_output=True).returncode)
    try:
        code = run(command)
    except (subprocess.TimeoutExpired, OSError) as error:
        return {'modem': 'failed', 'error': type(error).__name__}
    if code != 0:
        return {'modem': 'failed', 'exit': code}
    for _ in range(24):                                            # the line is down for a while; wait up to ~4 minutes for it
        sleep(10)
        if status_of(fetch) != 'unreachable':
            break
    after = _address(command, run_status)
    return {'modem': 'reset', 'probe': status_of(fetch), 'address_before': before, 'address_after': after,
            'address_changed': None if not before or not after else before != after}


def reset_tool(store, args, mode='local'):
    """Agent tool `network.reset_modem`. If the line came back and TKGM answers normally, an open TKGM incident is closed with the evidence."""
    result = reset_modem(store)
    if result.get('modem') == 'reset' and result.get('probe') == 'ok':
        with store.transaction() as con:
            incident = con.execute("SELECT id FROM source_incidents WHERE state='open' AND mode=? AND reason LIKE 'tkgm\\_http\\_%' ESCAPE '\\' LIMIT 1", (mode,)).fetchone()
        if incident:
            try:
                store.resolve_source(incident['id'], f"Modem yeniden başlatıldı ({args['reason'][:120]}); TKGM tek salt okuma isteği normal yanıt aldı.")
                result['incident_closed'] = incident['id']
            except ValueError as error:
                result['incident_not_closed'] = str(error)
    return {**result, 'reason': args['reason']}


def heal(store, mode='local', fetch=tkgm.fetch_json, now=None):
    """At most one probe per call, only when an hour has passed since the incident or the last probe."""
    now = time.time() if now is None else now
    with store.transaction() as con:
        incident = con.execute("SELECT * FROM source_incidents WHERE state='open' AND mode=? AND reason LIKE 'tkgm\\_http\\_%' ESCAPE '\\' LIMIT 1", (mode,)).fetchone()
        if not incident:
            return None
        con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
        key = 'recovery:' + incident['id']
        state = _load(con, key, {'attempts': 0, 'last': None})
        due = (state['last'] or incident['created_at']) + WAIT
        if now < due:
            return {'incident': incident['id'], 'due_in': round(due - now)}
        state['attempts'] += 1
        state['last'] = now
        _store_value(con, key, state, now)
    result = status_of(fetch)
    if result == 'ok':
        try:
            store.resolve_source(incident['id'], f"Otomatik: saatlik tek salt okuma isteği normal yanıt aldı (deneme {state['attempts']}).", now=now)
        except ValueError as error:
            return {'incident': incident['id'], 'probe': result, 'not_resolved': str(error)}
    return {'incident': incident['id'], 'probe': result, 'attempts': state['attempts']}
