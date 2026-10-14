"""Sahibinden access signals, ported from the legacy `sahibinden_access.py`.

A security control is never a read error: it stops the shared source (the user's rule,
24 h cool-down in the old policy). Nothing here tries to pass a control; it only names it.
"""
import re
from urllib.parse import urlparse

# Sahibinden's own "checking your browser / connection" interstitials live under /cs/*loading
# (the old system saw /cs/hloading and /cs/tloading; v2 met /cs/checkLoading).
_CHECK_PATH = re.compile(r'^/cs/[a-z]*loading', re.I)


def classify_url(url) -> str | None:
    """A blocker code for a page address alone, or None. Cheap and text-free."""
    parsed = urlparse(str(url or ''))
    if parsed.hostname == 'secure.sahibinden.com' or '/giris' in parsed.path:
        return 'session_lost'                 # ordinary login redirect: stop, the user logs in by hand
    if _CHECK_PATH.match(parsed.path):
        return 'unusual_access'
    return None


def _configured_account():
    """The one account this app acts as: a local setting, never part of the repository (VERDA_ACCOUNT_NAME, or .local/account.json
    {"name": "..."}). Without it no account check passes: no message is sent and no sign-in click is allowed."""
    import json
    import os
    from pathlib import Path
    name = os.environ.get('VERDA_ACCOUNT_NAME')
    if not name:
        try:
            name = json.loads((Path(__file__).resolve().parents[3] / '.local' / 'account.json').read_text()).get('name')
        except (OSError, ValueError, AttributeError):
            name = None
    return {'name': str(name).strip() or None if name else None}


# The one account this app acts as. Everything that touches messages checks it before acting.
ACCOUNT = _configured_account()


def _fold(text):
    from verda.facts.text import fold
    return fold(text)


def account_matches(displayed, expected=None) -> bool:
    """Sahibinden shortens the name ("Kaan Y." for "Deniz Kaan Yıldırım"): any consecutive run of the
    expected name's words, each word whole or as an initial, is the same person; another name is not."""
    shown, wanted = _fold(displayed).split(), _fold((expected or ACCOUNT['name'])).split()
    if not shown or len(shown) > len(wanted):
        return False
    same = lambda w, t: w == t or (len(w) == 1 and t.startswith(w))
    return any(all(same(w, t) for w, t in zip(shown, wanted[i:])) for i in range(len(wanted) - len(shown) + 1))


def login_state(page_text) -> str:
    """From the visible header of any Sahibinden page. 'logged_out' shows both "Giriş Yap" and "Hesap Aç"."""
    head = str(page_text or '')[:700]
    if not head.strip():
        return 'unknown'
    return 'logged_out' if 'Giriş Yap' in head and 'Hesap Aç' in head else 'logged_in_or_unknown'
