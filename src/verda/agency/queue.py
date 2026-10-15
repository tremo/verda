"""What is waiting to be done, listing by listing, with the arguments ready.

Bookkeeping only: no site access and no decisions. The manager reads this and hands batches to the operators; the
rule engine's next action per listing decides what is waiting, so a result that arrives (from any source) moves a
listing to its next step on the following sweep.
"""
import json
import time

from verda.agency import says

from verda.agency.tuning import number

OPERATOR = {'sahibinden.read_listing': 'sahibinden', 'sahibinden.read_thread': 'sahibinden', 'sahibinden.send_message': 'sahibinden',
            'tkgm.lookup': 'parcel', 'tkgm.corridor': 'parcel', 'tkgm.neighbors': 'parcel', 'sit.says': 'parcel', 'geo.route': 'research', 'listing.photos': 'sahibinden'}
ORDER = ['tkgm.lookup', 'sit.says', 'tkgm.corridor', 'tkgm.neighbors', 'geo.route', 'sahibinden.read_thread', 'sahibinden.read_listing', 'sahibinden.send_message', 'listing.photos']   # photos last: they never crowd out a message
RESOURCE_OF = {'tkgm.lookup': 'tkgm', 'tkgm.corridor': 'tkgm', 'tkgm.neighbors': 'tkgm', 'sit.says': 'says', 'geo.route': 'geography', 'sahibinden.read_listing': 'sahibinden', 'sahibinden.read_thread': 'sahibinden', 'sahibinden.send_message': 'sahibinden'}
ONE_AT_A_TIME = ('sahibinden.send_message',)           # a message is its own task: one listing, one message
TERMINAL = ('complete', 'failed', 'done')
DAY = 86400
PHOTO_STATES = ('conditional', 'eligible')       # listings that will be listed; the ones heading for elimination get no photos


def legacy_open_ids(store):
    """Listing numbers the old archive holds in a state other than excluded (sorted, for a stable order)."""
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return []
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        rows = archive.execute("SELECT listing_id FROM cases WHERE snapshot_id=? AND lifecycle!='excluded' ORDER BY listing_id", (snapshot['id'],)).fetchall() if snapshot else []
        return [r['listing_id'] for r in rows]
    finally:
        archive.close()


def neighborhood_of(store, listing_id):
    """The place the listing page itself names, from the newest detail read. None when no read has it."""
    with store.transaction() as con:
        for row in con.execute("SELECT payload FROM observations WHERE listing_ref=? AND mode='local' AND tool='sahibinden.read_listing' ORDER BY captured_at DESC,id DESC", (listing_id,)):
            data = json.loads(row['payload'])
            if data.get('listing_id') == listing_id and data.get('neighborhood'):
                return data['neighborhood']
    return None


def _conversation_unread(store, listing, mode):
    """The last send attempt stopped because the conversation already ends with our message, and no conversation read is newer than that."""
    from verda.agency import message
    with store.transaction() as con:
        rows = message.rows(con, listing, mode)
        last = rows[-1] if rows else None
        if not last or last['state'] != 'prepared' or last.get('reason') != 'waiting_for_reply':
            return False
        newer = con.execute("SELECT 1 FROM observations WHERE tool='sahibinden.read_thread' AND mode=? AND listing_ref=? AND captured_at>? LIMIT 1", (mode, listing, last['updated_at'])).fetchone()
        return newer is None


def _listing_changed_unread(store, listing, mode):
    """The last send attempt found the live listing different from the one the message was prepared from, and no listing read is newer than that:
    read the listing again first (it refreshes the facts and the digest), instead of opening the same page for another failed send every sweep."""
    from verda.agency import message
    with store.transaction() as con:
        rows = message.rows(con, listing, mode)
        last = rows[-1] if rows else None
        if not last or last['state'] != 'prepared' or last.get('reason') != 'listing_changed':
            return False
        newer = con.execute("SELECT 1 FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref=? AND captured_at>? LIMIT 1", (mode, listing, last['updated_at'])).fetchone()
        return newer is None


def _photos_exhausted(con, listing, now, within=7 * 86400):
    """The last photo save for this listing already did all it can: everything listed was saved or already there (two photos can be the same picture,
    so the stored count can stay below the listed count for good), or nothing was saved and the rest failed. Offering it again every sweep only repeats that."""
    row = con.execute("SELECT result,started_at FROM tool_calls WHERE tool='listing.photos' AND state='complete' AND arguments LIKE ? ORDER BY started_at DESC LIMIT 1", ('%"' + listing + '"%',)).fetchone()
    if not row or now - row['started_at'] > within:
        return False
    try:
        out = (json.loads(row['result']) or {}).get('output') or {}
    except ValueError:
        return False
    from verda.agency.photos import MAX_PHOTOS
    done = int(out.get('saved') or 0) + int(out.get('already') or 0)
    return done >= min(int(out.get('photos_listed') or 0), MAX_PHOTOS) or (bool(out.get('failed')) and not out.get('saved'))


def _has_assets(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_assets'").fetchone() is not None


def _open_listings(con):
    """Listings with unfinished work: by the task's own listing, or named inside a batch task's items.

    A task that stopped (blocked, waiting for the user) no longer holds its listings: they are offered again, and the
    "stuck" counter (failures per step per day) is what reports a step that keeps failing. An uncertain task still holds
    its listing: something may have been done there."""
    busy = set()
    for row in con.execute("SELECT listing_ref,inputs FROM inbox WHERE state IN ('queued','running','waiting_browser','waiting_children','uncertain')"):
        if row['listing_ref']:
            busy.add(row['listing_ref'])
        try:
            inputs = json.loads(row['inputs'] or '{}')
        except ValueError:
            continue
        for item in inputs.get('items', []) if isinstance(inputs, dict) else []:
            if isinstance(item, dict) and item.get('listing_id'):
                busy.add(str(item['listing_id']))
        if isinstance(inputs, dict) and inputs.get('listing_id'):
            busy.add(str(inputs['listing_id']))
    return busy


def _interpreted_today(con, listing, now):
    """True when a reply-reading batch for this listing was already handed out in the last day (one try a day, then it is reported)."""
    return con.execute("SELECT 1 FROM inbox WHERE created_at>? AND inputs LIKE '%reply_unresolved%' AND inputs LIKE ? LIMIT 1", (now - DAY, '%' + listing + '%')).fetchone() is not None


def _stuck_listing_read(store, listing, now, limit):
    with store.transaction() as con:
        return _stuck(con, 'sahibinden.read_listing', listing, now, limit)


def _stuck_read(store, listing, now, limit):
    with store.transaction() as con:
        return _stuck(con, 'sahibinden.read_thread', listing, now, limit)


def stuck_after(store):
    return number(store, 'queue.stuck_after')


def _stuck(con, tool, needle, now, limit):
    rows = con.execute("SELECT count(*) FROM tool_calls WHERE tool=? AND state IN ('failed','uncertain') AND arguments LIKE ? AND finished_at>?",
                       (tool, '%' + needle + '%', now - DAY)).fetchone()
    return rows[0] >= limit


def _sends_stopped(con, listing, now):
    """Send attempts in the last day that opened the page and stopped before the click. The tool call itself completes (the stop is its answer),
    so `_stuck` never saw them: 1340799649 was tried 32 times in a day, each stopping on the same 'listing_changed'."""
    return con.execute("""SELECT count(*) FROM tool_calls WHERE tool='sahibinden.send_message' AND state='complete' AND json_extract(arguments,'$.listing_id')=?
        AND json_extract(result,'$.output.status')='not_sent' AND finished_at>?""", (listing, now - DAY)).fetchone()[0]


def says_parcels(current):
    facts = current.get('facts') or {}
    main = (facts.get('parcel_key') or {}).get('value') if isinstance(facts.get('parcel_key'), dict) else None
    listed = (facts.get('sale_parcels') or {}).get('value') if isinstance(facts.get('sale_parcels'), dict) else None
    return ([main] if main else []) + [p for p in (listed or []) if p != main]


def work_queue(store, args, *, mode, status=None, now=None):
    from verda.agency.research import fact_status
    status = status or (lambda listing: fact_status(store, listing, mode=mode))
    now = time.time() if now is None else now
    batch = int(args.get('batch') or number(store, 'queue.batch'))
    limit, rereads = number(store, 'queue.stuck_after'), number(store, 'queue.rereads_per_day')
    with store.transaction() as con:
        listings = [r[0] for r in con.execute("SELECT DISTINCT listing_ref FROM assessments WHERE mode=? ORDER BY listing_ref", (mode,))]
        busy = _open_listings(con)
        halted = {r['key'] for r in con.execute('SELECT key FROM resources WHERE halted IS NOT NULL')}
    # Listings the old system still treated as open but this system has not looked at yet: they enter the work too.
    known = set(listings)
    listings += [i for i in legacy_open_ids(store) if i not in known]
    from verda.agency import message
    sender_on = message.mode_of(store) != 'off'
    sender_reasons = {}
    groups, skipped = {}, {'open_task': 0, 'stuck': 0, 'no_action': 0, 'waits_for_sender': 0, 'sender_refused': 0, 'source_halted': 0, 'needs_neighborhood': 0, 'human': 0, 'waits_for_says': 0, 'listing_changed_unreadable': 0, 'no_listing_url': 0}
    skipped_ids = {k: [] for k in skipped}
    def skip(reason, listing):
        skipped[reason] += 1
        skipped_ids[reason].append(listing)
    photo_candidates = []
    for listing in listings:
        current = status(listing)
        if (current.get('decision') or {}).get('state') in PHOTO_STATES and listing not in busy:
            photo_candidates.append(listing)
        seen_here = current.get('seen') or {}
        if seen_here.get('corridor_enclosed') and not seen_here.get('neighbors_checked') and listing not in busy and 'tkgm' not in halted \
                and (current.get('decision') or {}).get('state') in ('research_required', 'conditional', 'eligible'):
            with store.transaction() as con:
                stuck_here = _stuck(con, 'tkgm.neighbors', listing, now, limit)
            if not stuck_here:
                groups.setdefault('tkgm.neighbors', []).append({'listing_id': listing, 'why': 'neighbour_types_unchecked'})
        # The official sit layer is asked before any message: a parcel the map puts in a first-degree sit is eliminated, not written to.
        says_due = seen_here.get('tkgm_looked_up') and 'says' not in halted \
            and (current.get('decision') or {}).get('state') in ('research_required', 'conditional', 'eligible') \
            and says.stale(store, listing, mode, says_parcels(current))      # never checked, or the sale's parcels changed since
        says_stuck = False
        if says_due:
            with store.transaction() as con:
                says_stuck = _stuck(con, 'sit.says', listing, now, limit)
            if not says_stuck and listing not in busy:
                groups.setdefault('sit.says', []).append({'listing_id': listing, 'why': 'official_sit_unchecked'})
        action = current['next_action']['action']
        if action in OPERATOR and RESOURCE_OF.get(action) in halted:
            skip('source_halted', listing)             # the source is stopped: nothing is handed out until a person resumes it
            continue
        if action in OPERATOR:
            if listing in busy:
                skip('open_task', listing)
                continue
            item = {'listing_id': listing, 'why': current['next_action'].get('why')}
            needle = listing
            if action == 'sahibinden.read_listing':
                from verda.agency.jev import known_listing_url
                if not known_listing_url(store, listing):
                    # Known only from a conversation, never seen in a search: no address to open. A read would fail every time
                    # (1236920275: eleven failures in four days), so it is shown to the user instead of being tried.
                    skip('no_listing_url', listing)
                    continue
            if action == 'tkgm.lookup':
                parcel = current['next_action'].get('parcel') or ((current.get('facts') or {}).get('parcel_key') or {}).get('value')
                if current['next_action'].get('part'):
                    item['part'] = True                 # one more deed of the same sale: its own area only
                hood = neighborhood_of(store, listing)
                if parcel and not hood:
                    # The page named no place (or was read by an older reader): read it again, at most twice a day.
                    with store.transaction() as con:
                        reads = con.execute("SELECT count(*) FROM observations WHERE listing_ref=? AND tool='sahibinden.read_listing' AND mode=? AND captured_at>?", (listing, mode, now - DAY)).fetchone()[0]
                        stuck = _stuck(con, 'sahibinden.read_listing', listing, now, limit)
                    if reads < rereads and not stuck:
                        groups.setdefault('sahibinden.read_listing', []).append({'listing_id': listing, 'why': 'neighborhood_missing'})
                    else:
                        skip('needs_neighborhood', listing)
                    continue
                if not parcel:
                    skip('needs_neighborhood', listing)
                    continue
                item.update(parcel_key=parcel, neighborhood=hood)
                needle = parcel
            with store.transaction() as con:
                if _stuck(con, action, needle, now, limit):
                    skip('stuck', listing)
                    continue
            groups.setdefault(action, []).append(item)
        elif action.startswith('ask:'):
            if says_due and not says_stuck:
                skip('waits_for_says', listing)         # the official sit layer is asked first
            elif not sender_on:
                skip('waits_for_sender', listing)      # sending is switched off: the question waits
            elif listing in busy:
                skip('open_task', listing)
            else:
                verdict = message.preview(store, listing, mode, now, current=current)
                with store.transaction() as con:
                    stuck = _stuck(con, 'sahibinden.send_message', listing, now, limit) or _sends_stopped(con, listing, now) >= limit
                    tried = verdict.get('replied') and _interpreted_today(con, listing, now)
                changed = verdict['ok'] and _listing_changed_unread(store, listing, mode)
                if 'sahibinden' in halted:
                    skip('source_halted', listing)
                elif verdict.get('reason') == 'conversation_unconfirmed':
                    # An earlier read opened this conversation, the latest did not: read it again; no question until it is found.
                    if _stuck_read(store, listing, now, limit):
                        skip('stuck', listing)
                    else:
                        groups.setdefault('sahibinden.read_thread', []).append({'listing_id': listing, 'why': 'conversation_missing'})
                elif changed and _stuck_listing_read(store, listing, now, limit):
                    skip('listing_changed_unreadable', listing)   # the page cannot be read again: a send would only stop on the old digest once more
                elif verdict.get('replied') and not tried and not _stuck_read(store, listing, now, limit):
                    # We asked and the seller answered, but no value came out of the answer: read it once a day and interpret it.
                    groups.setdefault('sahibinden.read_thread', []).append({'listing_id': listing, 'why': 'reply_unresolved'})
                elif verdict['ok'] and _conversation_unread(store, listing, mode) and not _stuck_read(store, listing, now, limit):
                    # The page showed a conversation that already ends with our message, but this system has not read it: read it first, so the
                    # same listing is not offered for sending every sweep.
                    groups.setdefault('sahibinden.read_thread', []).append({'listing_id': listing, 'why': 'conversation_exists'})
                elif changed:
                    groups.setdefault('sahibinden.read_listing', []).append({'listing_id': listing, 'why': 'listing_changed'})
                elif stuck:
                    skip('stuck', listing)
                elif verdict['ok']:
                    groups.setdefault('sahibinden.send_message', []).append({'listing_id': listing, 'why': current['next_action'].get('why'), 'asks': verdict['fields'], 'dry_run': verdict['dry_run']})
                else:
                    skip('sender_refused', listing)
                    sender_reasons[verdict['reason']] = sender_reasons.get(verdict['reason'], 0) + 1
        elif action in {'human_review', 'user_review'}:
            skip('human', listing)
        else:
            skip('no_action', listing)
    if photo_candidates:
        from verda.agency.photos import MAX_PHOTOS
        with store.transaction() as con:
            for listing in photo_candidates:
                row = con.execute("SELECT payload FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref=? ORDER BY captured_at DESC LIMIT 1", (mode, listing)).fetchone()
                listed = len([u for u in (json.loads(row['payload']).get('photos') or []) if isinstance(u, str)]) if row else 0
                saved = con.execute('SELECT count(*) FROM evidence_assets WHERE listing_ref=? AND mode=?', (listing, mode)).fetchone()[0] if _has_assets(con) else 0
                if min(listed, MAX_PHOTOS) > saved and not _stuck(con, 'listing.photos', listing, now, limit) and not _photos_exhausted(con, listing, now):
                    groups.setdefault('listing.photos', []).append({'listing_id': listing, 'why': 'photos_missing'})
    out = [{'action': a, 'operator': OPERATOR[a], 'waiting': len(groups[a]), 'batch': groups[a][:1 if a in ONE_AT_A_TIME else 4 if a == 'tkgm.neighbors' else batch]} for a in ORDER if a in groups]
    return {'groups': out, 'skipped': skipped, 'skipped_ids': {k: v[:60] for k, v in skipped_ids.items() if k != 'no_action'}, 'waiting_total': sum(len(v) for v in groups.values()),
            'sender': {'mode': message.mode_of(store), 'refusals': sender_reasons}, 'advice': 'dispatch_batches' if out else 'nothing_to_do'}
