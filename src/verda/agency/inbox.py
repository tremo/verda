"""Which inbox conversations still need reading.

A conversation is read once per last-message time: the row identity (title, counterpart, last message time) is the
"already read" marker, kept by the observation that read it. Rows that the old system already excluded are never
read again: an excluded listing is not proposed again, so its replies cannot change anything.
"""
import json
from verda.facts.text import fold


def read_markers(store):
    """Row identities already read by a successful read_thread, from the append-only observations."""
    seen = set()
    with store.transaction() as con:
        for row in con.execute("SELECT arguments FROM observations WHERE tool='sahibinden.read_thread' AND mode='local' AND kind='tool_observation'"):
            r = json.loads(row['arguments']).get('row')
            if r:
                seen.add((fold(r['title']), fold(r.get('counterpart') or ''), r.get('last_at_text')))
    return seen


def legacy_lifecycles(store):
    """fold(title)[:30] -> set of (listing_id, lifecycle) from the old archive; empty without an archive."""
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return {}
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        out = {}
        for r in archive.execute('SELECT title,listing_id,lifecycle FROM cases WHERE snapshot_id=?', (snapshot['id'],)) if snapshot else []:
            if r['title']:
                out.setdefault(fold(r['title'])[:30], set()).add((r['listing_id'], r['lifecycle']))
        return out
    finally:
        archive.close()


STUCK_ROW_AFTER = 2          # failed reads of the same row within a day, after which it is left alone until tomorrow


def failed_row_counts(store, now=None):
    import time
    from collections import Counter
    now = time.time() if now is None else now
    out = Counter()
    with store.transaction() as con:
        for r in con.execute("SELECT arguments FROM tool_calls WHERE tool='sahibinden.read_thread' AND state='failed' AND arguments LIKE '%\"row\"%' AND started_at>?", (now - 86400,)):
            try:
                row = json.loads(r['arguments']).get('row') or {}
            except ValueError:
                continue
            out[(fold(row.get('title', '')), fold(row.get('counterpart') or ''))] += 1
    return out


def plan(store, rows):
    """[(row, 'read' | 'skip:already_read' | 'skip:legacy_excluded')], in inbox order."""
    seen, legacy, out = read_markers(store), legacy_lifecycles(store), []
    stuck = failed_row_counts(store)
    for row in rows:
        if stuck[(fold(row['title']), fold(row.get('counterpart') or ''))] >= STUCK_ROW_AFTER:
            out.append((row, 'skip:stuck'))        # it failed twice already today; trying it a third time in the same hour only costs the others their turn
            continue
        identity = (fold(row['title']), fold(row.get('counterpart') or ''), row.get('last_at_text'))
        matches = legacy.get(fold(row['title'])[:30], set())
        if int(row.get('unread') or 0) > 0:
            out.append((row, 'read'))          # Sahibinden says there is something new, whatever the time column shows (it is often empty)
        elif identity in seen:
            out.append((row, 'skip:already_read'))
        elif matches and all(lifecycle == 'excluded' for _, lifecycle in matches):
            out.append((row, 'skip:legacy_excluded'))
        else:
            out.append((row, 'read'))
    return out


def run_sync(store, *, step, sleep=None, sync_id=None):
    """List the inbox, then read only the conversations whose last message changed since they were read.

    Rows without an answer cost nothing here: an unchanged row is already marked read. A seller's reply moves the
    row's last-message time, which is what brings the conversation back.
    """
    import hashlib
    import time
    from verda.agency.discovery import _observation, _run_until_done
    sleep = sleep or time.sleep
    sync_id = sync_id or time.strftime('%Y%m%dT%H%M%S')
    key = f'inbox-sync:{sync_id}:list'
    store.submit(agent='sahibinden', objective='Gelen kutusu listesi', request_key=key, executor_tool='sahibinden.list_inbox', inputs={})
    state = _run_until_done(store, key, step, sleep=sleep)
    receipt = _observation(store, state['id'], 'sahibinden.list_inbox') if state and state['state'] == 'complete' else None
    if receipt is None:
        return {'sync_id': sync_id, 'listed': 0, 'stopped_early': (state or {}).get('reason') or 'no_result'}
    todo = [row for row, why in plan(store, receipt['rows']) if why == 'read']
    done = failed = 0
    for row in todo:
        row_key = 'inbox-sync-row-' + hashlib.sha1(json.dumps([row['title'], row['counterpart'], row['last_at_text']], ensure_ascii=False).encode()).hexdigest()[:16]
        store.submit(agent='sahibinden', objective='Konuşmayı oku', request_key=row_key, executor_tool='sahibinden.read_thread',
                     inputs={'row': {k: row[k] for k in ('title', 'counterpart', 'last_at_text')}})
        outcome = _run_until_done(store, row_key, step, sleep=sleep)
        if outcome and outcome['state'] == 'complete':
            done += 1
        else:
            failed += 1
    return {'sync_id': sync_id, 'listed': len(receipt['rows']), 'complete_list': receipt['complete'],
            'to_read': len(todo), 'read': done, 'failed': failed, 'unread_total': sum(r['unread'] for r in receipt['rows'])}


def inbox_plan(store, args, *, mode, task_id):
    """The agent's inbox helper: from the inbox list read in this task, which conversations still need reading."""
    with store.transaction() as con:
        row = con.execute("SELECT payload FROM observations WHERE task_id=? AND tool='sahibinden.list_inbox' AND mode=? ORDER BY captured_at DESC LIMIT 1",
                          (task_id, mode)).fetchone()
        done = {fold(json.loads(r['arguments']).get('row', {}).get('title', '')) + '|' + str(json.loads(r['arguments']).get('row', {}).get('last_at_text'))
                for r in con.execute("SELECT arguments FROM observations WHERE task_id=? AND tool='sahibinden.read_thread' AND mode=?", (task_id, mode))}
    if not row:
        return {'listed': 0, 'advice': 'list_inbox_first', 'to_read': []}
    rows = json.loads(row['payload'])['rows']
    decided = plan(store, rows)
    pending = [r for r, why in decided if why == 'read' and fold(r['title']) + '|' + str(r['last_at_text']) not in done]
    limit = int(args.get('limit', 25))
    counts = {}
    for _, why in decided:
        counts[why] = counts.get(why, 0) + 1
    return {'listed': len(rows), 'counts': counts, 'to_read_total': len(pending),
            'to_read': [{k: r[k] for k in ('title', 'counterpart', 'last_at_text')} for r in pending[:limit]],
            'advice': 'read_threads' if pending else 'finish'}


STOP_AFTER_SETTLED = 15          # a recent-depth listing stops after this many rows in a row that need nothing


def settled_check(store):
    """row -> True when nothing is to be done for it: it was read at this last-message time, or the old system excluded its listing."""
    seen, legacy = read_markers(store), legacy_lifecycles(store)

    def settled(row):
        identity = (fold(row['title']), fold(row.get('counterpart') or ''), row.get('last_at_text'))
        matches = legacy.get(fold(row['title'])[:30], set())
        if int(row.get('unread') or 0) > 0:
            return False
        return identity in seen or bool(matches) and all(lifecycle == 'excluded' for _, lifecycle in matches)
    return settled


def enough_rows(rows, settled, run=STOP_AFTER_SETTLED):
    """The list is newest first: once `run` rows in a row after the last row that needs something are settled, nothing older can need it."""
    last = -1
    for i, row in enumerate(rows):
        if not settled(row):
            last = i
    return len(rows) - (last + 1) >= run


FULL_LIST_SHARE = 0.9        # a "complete" list shorter than this share of the longest one seen stopped loading early; it proves nothing


def known_titles(store, max_full_age=8 * 86400, now=None):
    """Folded titles of every conversation the inbox is known to hold: the newest complete list plus every later one.
    None when no complete list is recent enough, because then the absence of a title proves nothing.

    Conversations do not vanish by the hundred: a list marked complete with far fewer rows than an earlier complete one (30 against
    216 on 14 Oct 2026, when loading stalled) is not complete, and trusting it hid fifteen existing conversations."""
    import time
    now = time.time() if now is None else now
    with store.transaction() as con:
        lists = [(r['captured_at'], json.loads(r['payload'])) for r in con.execute(
            "SELECT captured_at,payload FROM observations WHERE tool='sahibinden.list_inbox' AND mode='local' ORDER BY captured_at,id")]
    longest = max((len(p['rows']) for _, p in lists if p.get('complete')), default=0)
    base = max((i for i, (at, p) in enumerate(lists) if p.get('complete') and now - at <= max_full_age and len(p['rows']) >= FULL_LIST_SHARE * longest), default=None)
    if base is None:
        return None
    return {fold(r['title']) for _, p in lists[base:] for r in p['rows']}


def found_before(store, listing_id, mode='local'):
    """True when some earlier read opened this listing's conversation: then a later "not found" is a reading failure, not an answer."""
    with store.transaction() as con:
        return con.execute("SELECT 1 FROM observations WHERE tool='sahibinden.read_thread' AND mode=? AND listing_ref=? AND json_extract(payload,'$.found')=1 LIMIT 1",
                           (mode, listing_id)).fetchone() is not None
