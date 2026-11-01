"""A problem that stays on the status page becomes a notification on this Mac. Local only: macOS shows it, nothing is sent anywhere.

Only the page's "bad" lines count (a broken critical rule, a blocked source, a stopped service). One that is gone by the next round is a blip
and says nothing; one that has lasted `AFTER` seconds is told once, and again every `REPEAT` seconds while it lasts. Off with VERDA_NOTIFY=off."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

AFTER = 600
REPEAT = 6 * 3600
SCRIPT = ['on run argv', 'display notification (item 1 of argv) with title (item 2 of argv)', 'end run']


def _key(text):
    """The same problem under a changing age or count ("açılalı 3 dk", "60 ilan"): its wording without the numbers."""
    return re.sub(r'\d+', '#', str(text).split(' (')[0])[:100]


def show(text, title='Verda', run=subprocess.run):
    command = ['osascript', *(arg for line in SCRIPT for arg in ('-e', line)), text[:200], title]
    return run(command, capture_output=True, timeout=10, check=False).returncode == 0


def notify(store, health, now=None, *, run=subprocess.run, state_path=None):
    """`health`: the status page's lines ({level, text}). Returns the texts shown this round."""
    if os.environ.get('VERDA_NOTIFY', '').lower() == 'off':
        return []
    now = time.time() if now is None else now
    path = Path(state_path or Path(store.path).parent / 'notified.json')
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    bad = {_key(h['text']): h['text'] for h in health if h.get('level') == 'bad'}
    next_state, shown = {}, []
    for key, text in bad.items():
        seen = state.get(key) or {'since': now, 'told': None}
        due = now - seen['since'] >= AFTER and (seen['told'] is None or now - seen['told'] >= REPEAT)
        if due and show(text, run=run):
            seen['told'] = now
            shown.append(text)
        next_state[key] = seen                        # anything not bad this round drops out, so its next occurrence starts a new count
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(next_state))
    os.replace(tmp, path)
    return shown
