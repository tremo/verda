"""The watchdog: rules that must always hold, checked against the records.

Read-only and deterministic: it repairs nothing and decides nothing. A broken rule becomes a finding the user sees, in the panel's
attention list and on the dashboard's status page, with the listings and the numbers behind it. It runs two ways that do not depend on
each other: as a timer task of the manager (tool `watchdog.check`, code only, no model call; every result is kept as a record) and inside
the publisher, so the dashboard still shows a broken rule when the agent worker itself is down. A rule that cannot be checked says so; it
never passes quietly.

The rules come from the incidents of 14 Oct 2026: a question re-asked after the seller had answered, fifteen conversations reported
missing after a stalled inbox list, 32 send attempts on one listing, seller answers left uninterpreted for a week, a two-deed listing
excluded on one deed's area, a weekly scan that never reached its last page. Each one would have been a finding here the hour it began.
"""
import json
import math
import time
from pathlib import Path

CRITICAL, ACTION, WARN = 'critical', 'action', 'warn'
DAY = 86400
MAX_LISTED = 25
SEND_ATTEMPTS = 4            # send attempts on one listing in a day
STEP_REPEATS = 8             # calls of one browser / official-source tool for one subject in a day
FAILURE_RATE, FAILURE_MIN_CALLS = 0.20, 10
REPLY_WAIT = DAY             # a seller answer neither recorded nor flagged after this long
STALE_DAYS = 8               # an active listing not seen in any search or page read for this long
SCAN_SILENCE = 36 * 3600     # no search page read for this long
FULL_SCAN_EVERY = 8 * DAY
PUBLISH_SILENCE = 20 * 60
WORKER_SILENCE = 30 * 60
WATCHED_TOOLS = ('sahibinden.read_listing', 'sahibinden.read_thread', 'listing.photos', 'tkgm.lookup', 'tkgm.corridor', 'tkgm.neighbors', 'sit.says', 'geo.route')
# (the inbox list and the search pages run on timers many times a day by design: they have no subject to loop on)
ACTIVE = ('eligible', 'conditional', 'research_required')


def _finding(rule, severity, title, detail, listings=(), count=None):
    listings = sorted({str(l) for l in listings if l})
    return {'rule': rule, 'severity': severity, 'title': title, 'detail': detail[:400], 'listings': listings[:MAX_LISTED],
            'count': count if count is not None else max(1, len(listings))}


def _latest_states(con, mode):
    """listing -> (state, reasons) of its newest assessment."""
    rows = con.execute("""SELECT a.listing_ref, a.state, a.reasons FROM assessments a JOIN (SELECT listing_ref, max(seq) m FROM assessments WHERE mode=? GROUP BY 1) x
        ON a.listing_ref=x.listing_ref AND a.seq=x.m""", (mode,))
    return {r['listing_ref']: (r['state'], json.loads(r['reasons'] or '[]')) for r in rows}


# ---- messages ---------------------------------------------------------------------------------------------------------------
def repeat_question(store, mode, now):
    """No message may ask what the conversation already covered when it went out (asked before, or raised by the seller unasked),
    and none may go out while a conversation seen before is missing from the latest read."""
    from verda.agency import message
    with store.transaction() as con:
        message.ensure(con)
        sends = [dict(r) for r in con.execute("""SELECT * FROM outbox WHERE mode=? AND state IN ('confirmed','delivery_unknown')
            AND COALESCE(sent_at, updated_at)>=? ORDER BY COALESCE(sent_at, updated_at)""", (mode, now - 7 * DAY))]
        before = {}
        for s in sends:
            at = s['sent_at'] or s['updated_at']
            reads = [json.loads(r['payload']) for r in con.execute("""SELECT payload FROM observations WHERE listing_ref=? AND mode=? AND tool='sahibinden.read_thread'
                AND captured_at<? ORDER BY captured_at, id""", (s['listing_ref'], mode, at))]
            earlier = [r['text'] for r in con.execute("""SELECT text FROM outbox WHERE listing_ref=? AND mode=? AND id!=? AND state IN ('confirmed','delivery_unknown')
                AND COALESCE(sent_at, updated_at)<?""", (s['listing_ref'], mode, s['id'], at))]
            before[s['id']] = (reads, earlier)
    out = []
    for s in sends:
        reads, earlier = before[s['id']]
        legacy = message.legacy_sent(store, s['listing_ref']) if mode == 'local' else []
        seen = [{'who': 'sen' if m.get('role') == 'user' else 'satıcı', 'text': m.get('text') or ''} for p in reads for m in p.get('messages') or []]
        seen += [{'who': 'sen', 'text': t} for t in earlier] + legacy
        seen = [m for m in seen if not message.exact_in(s['text'], m['text'])]          # the message itself is no evidence against itself
        hit = message.overlap(json.loads(s['fields']), seen)
        found = [bool(p.get('found')) for p in reads]
        lost = any(found) and not found[-1]
        if hit['asked'] or hit['seller_wrote'] or lost:
            why = []
            if hit['asked']:
                why.append('daha önce sorulmuştu: ' + ', '.join(message.FIELD_NAME[f] for f in hit['asked']))
            if hit['seller_wrote']:
                why.append('satıcı zaten yazmıştı: ' + ', '.join(message.FIELD_NAME[f] for f in hit['seller_wrote']))
            if lost:
                why.append('konuşma daha önce okunmuştu, son okuma bulamamıştı')
            out.append(_finding('repeat_question', CRITICAL, f"#{s['listing_ref']} ilanına tekrar soru gitti",
                                f"{time.strftime('%d.%m %H:%M', time.localtime(s['sent_at'] or s['updated_at']))} mesajı: " + '; '.join(why) + '.', [s['listing_ref']]))
    return out


def send_policy(store, mode, now):
    """Live messages only inside 08:30-21:00 Istanbul time and never closer together than the configured gap."""
    from verda.agency import message
    from verda.agency.tuning import number
    gap = number(store, 'sender.gap_minutes') * 60
    with store.transaction() as con:
        message.ensure(con)
        sent = [dict(r) for r in con.execute("SELECT listing_ref, sent_at FROM outbox WHERE mode=? AND state='confirmed' AND sent_at>=? ORDER BY sent_at", (mode, now - 7 * DAY))]
    outside = [s['listing_ref'] for s in sent if not message.within_window(s['sent_at'])]
    close = [b['listing_ref'] for a, b in zip(sent, sent[1:]) if b['sent_at'] - a['sent_at'] < gap - 1]
    out = []
    if outside:
        out.append(_finding('send_policy', CRITICAL, f'{len(outside)} mesaj izinli saat dışında gitti', 'Mesajlar yalnız 08:30–21:00 arasında gider.', outside))
    if close:
        out.append(_finding('send_policy', CRITICAL, f'{len(close)} mesaj bir öncekine çok yakın gitti', f'İki mesaj arasında en az {gap // 60:.0f} dakika olmalı.', close))
    return out


def send_loop(store, mode, now):
    """One listing, many send attempts in a day: every stop before the click is an attempt too."""
    with store.transaction() as con:
        rows = con.execute("""SELECT json_extract(arguments,'$.listing_id') AS listing, count(*) AS n FROM tool_calls WHERE tool='sahibinden.send_message'
            AND started_at>? GROUP BY 1 HAVING n>=?""", (now - DAY, SEND_ATTEMPTS)).fetchall()
    return [_finding('send_loop', ACTION, f"#{r['listing']} için 24 saatte {r['n']} gönderim denemesi",
                     'Aynı ilana tekrar tekrar gönderim deneniyor; bir kapı ya da sayfa sürekli aynı yerde durduruyor.', [r['listing']], r['n']) for r in rows]


def replies_waiting(store, mode, now):
    """Every seller answer ends as a fact, a flag for the user, or nothing to say. None waits a day unread."""
    from verda.agency.replies import waiting
    late = [i for i in waiting(store, mode, now) if now - (i.get('read_at') or now) > REPLY_WAIT]
    if not late:
        return []
    oldest = min(i['read_at'] for i in late)
    return [_finding('replies_waiting', ACTION, f'{len(late)} ilanda satıcı cevabı işlenmeden bekliyor',
                     f"En eskisi {(now - oldest) / DAY:.0f} gündür ne olguya çevrildi ne de netleştirme için işaretlendi.", [i['listing_id'] for i in late])]


# ---- reading the inbox ---------------------------------------------------------------------------------------------------------
def conversation_lost(store, mode, now):
    """A conversation once opened is never "not found" afterwards: the latest read of every such listing must have found it."""
    from verda.agency.inbox import lost_conversations
    with store.transaction() as con:
        states = _latest_states(con, mode)
    lost = [l for l in lost_conversations(store, mode) if states.get(l, ('', []))[0] not in ('excluded', 'outside_search')]
    return [_finding('conversation_lost', ACTION, f'{len(lost)} ilanın konuşması son okumada bulunamadı',
                     'Bu konuşmalar daha önce okunmuştu. Yeniden bulunana kadar bu ilanlara soru gitmez; kuyruk günde bir kez yeniden okutur.', lost)] if lost else []


def inbox_list_short(store, mode, now):
    """An inbox list marked complete is never far shorter than the longest complete one: conversations do not vanish by the hundred."""
    from verda.agency.inbox import FULL_LIST_SHARE
    with store.transaction() as con:
        lists = [(r['captured_at'], len(json.loads(r['payload'])['rows'])) for r in con.execute(
            "SELECT captured_at, payload FROM observations WHERE tool='sahibinden.list_inbox' AND mode=? AND json_extract(payload,'$.complete')=1", (mode,))]
    longest = max((n for _, n in lists), default=0)
    short = [(at, n) for at, n in lists if at > now - DAY and n < FULL_LIST_SHARE * longest]
    return [_finding('inbox_list_short', WARN, f'Gelen kutusu listesi eksik okunmuş ({n} satır, en uzunu {longest})',
                     'Liste yüklenirken takılmış. Konuşma yokluğu bu listeye dayanılarak söylenmez.', count=1) for _, n in short[-1:]]


# ---- work that loops or fails ------------------------------------------------------------------------------------------------
def step_loop(store, mode, now):
    """One step for one listing or parcel, repeated over and over in a day."""
    marks = ','.join('?' * len(WATCHED_TOOLS))
    with store.transaction() as con:
        rows = con.execute(f"""SELECT tool, COALESCE(json_extract(arguments,'$.listing_id'), json_extract(arguments,'$.parcel_key'), json_extract(arguments,'$.row.title')) AS subject,
            count(*) AS n FROM tool_calls WHERE tool IN ({marks}) AND started_at>? GROUP BY 1, 2 HAVING n>=? AND subject IS NOT NULL""", (*WATCHED_TOOLS, now - DAY, STEP_REPEATS)).fetchall()
    return [_finding('step_loop', ACTION, f"{r['tool']} aynı konu için 24 saatte {r['n']} kez çalıştı", f"Konu: {str(r['subject'])[:60]}. Bir döngü olabilir.",
                     [r['subject']] if str(r['subject']).isdigit() else [], r['n']) for r in rows]


def tool_failures(store, mode, now):
    """No tool fails more than a fifth of its calls in a day."""
    with store.transaction() as con:
        rows = con.execute("""SELECT tool, count(*) AS n, sum(state IN ('failed','uncertain')) AS bad FROM tool_calls WHERE started_at>? GROUP BY tool""", (now - DAY,)).fetchall()
        out = []
        for r in rows:
            if r['n'] >= FAILURE_MIN_CALLS and r['bad'] / r['n'] > FAILURE_RATE:
                errors = con.execute("""SELECT json_extract(result,'$.error') AS e, count(*) AS n FROM tool_calls WHERE tool=? AND started_at>? AND state IN ('failed','uncertain')
                    GROUP BY 1 ORDER BY n DESC LIMIT 3""", (r['tool'], now - DAY)).fetchall()
                out.append(_finding('tool_failures', WARN, f"{r['tool']} çağrılarının %{100 * r['bad'] / r['n']:.0f}'i başarısız ({r['bad']}/{r['n']})",
                                    'En sık hata: ' + ', '.join(f"{e['e']} ×{e['n']}" for e in errors), count=r['bad']))
    return out


# ---- decisions ----------------------------------------------------------------------------------------------------------------
def exclusion_doubt(store, mode, now):
    """An area exclusion rests on the land that is for sale: the listing's own area agrees with the official one."""
    from verda.facts.assemble import SCOPE_MISMATCH
    from verda.policy import Policy
    minimum = Policy().min_area_m2
    with store.transaction() as con:
        excluded = [l for l, (state, reasons) in _latest_states(con, mode).items() if state == 'excluded' and 'area_below_minimum' in reasons]
        doubt = []
        for listing in excluded:
            areas = {r['grade']: json.loads(r['value']) for r in con.execute("""SELECT grade, value FROM claims WHERE listing_ref=? AND mode=? AND field='sale_area_m2'
                AND status='accepted' ORDER BY seq""", (listing, mode))}
            official, stated = areas.get('resmi'), areas.get('beyan')
            if isinstance(official, (int, float)) and isinstance(stated, (int, float)) and stated >= minimum and abs(stated - official) / max(stated, official) > SCOPE_MISMATCH:
                doubt.append(listing)
    return [_finding('exclusion_doubt', ACTION, f'{len(doubt)} ilan şüpheli bir alanla elenmiş',
                     'İlanın kendi alanı en az alanı geçiyor ama resmî alan çok farklı: yanlış parsel, birden çok tapu ya da hisse olabilir.', doubt)] if doubt else []


# ---- coverage and liveness -----------------------------------------------------------------------------------------------------
def scan_coverage(store, mode, now):
    """The market is read: a search page at least every day and a full scan to the last page every week."""
    out = []
    with store.transaction() as con:
        last = con.execute("SELECT max(captured_at) FROM observations WHERE tool='sahibinden.search' AND mode=?", (mode,)).fetchone()[0]
        full = con.execute("SELECT id, created_at FROM inbox WHERE mode=? AND json_extract(inputs,'$.routine')='full_scan' ORDER BY created_at DESC LIMIT 1", (mode,)).fetchone()
        pages = [json.loads(r['payload']) for r in con.execute("SELECT payload FROM observations WHERE task_id=? AND tool='sahibinden.search' ORDER BY captured_at", (full['id'],))] if full else []
    if not last or now - last > SCAN_SILENCE:
        out.append(_finding('scan_coverage', WARN, 'İlan taraması yapılmıyor', f"Son arama sayfası {'hiç okunmadı' if not last else f'{(now - last) / 3600:.0f} saat önce okundu'}; yeni ilanlar görülmüyor."))
    if not full or now - full['created_at'] > FULL_SCAN_EVERY:
        out.append(_finding('scan_coverage', WARN, 'Haftalık tam tarama yapılmadı', 'Eski ilanlardaki fiyat değişiklikleri ve kalkan ilanlar görülmüyor.'))
    elif pages:
        top = max(pages, key=lambda p: p.get('page', 0))
        total = math.ceil((top.get('total_reported') or 0) / 20) or top.get('page', 0)
        if top.get('has_more'):
            out.append(_finding('scan_coverage', WARN, f"Tam tarama {top.get('page')}/{total}. sayfada kaldı", 'Kalan sayfalardaki ilanlar bu hafta hiç görülmedi.'))
    return out


def stale_listings(store, mode, now):
    """Every active listing is seen in a search or read again within a week: otherwise it may be sold, removed or repriced unseen."""
    from verda.agency.research import legacy_excluded_ids
    shut = legacy_excluded_ids(store, mode)                 # eliminated by the old system: nobody reads those again, so they are not "unseen"
    with store.transaction() as con:
        active = {l for l, (state, _) in _latest_states(con, mode).items() if state in ACTIVE} - shut
        removed = {r[0] for r in con.execute("SELECT DISTINCT listing_ref FROM observations WHERE mode=? AND tool='sahibinden.read_listing' AND payload LIKE '%\"unavailable\":true%'", (mode,))}
        seen = {r['listing_ref']: r['at'] for r in con.execute("SELECT listing_ref, max(captured_at) AS at FROM observations WHERE mode=? AND tool='sahibinden.read_listing' GROUP BY 1", (mode,))}
        for r in con.execute("SELECT captured_at, payload FROM observations WHERE mode=? AND tool='sahibinden.search' AND captured_at>?", (mode, now - STALE_DAYS * DAY)):
            for item in json.loads(r['payload']).get('listings', []):
                seen[item.get('listing_id')] = max(seen.get(item.get('listing_id'), 0), r['captured_at'])
    stale = [l for l in active - removed if now - seen.get(l, 0) > STALE_DAYS * DAY]
    return [_finding('stale_listings', WARN, f'{len(stale)} aktif ilan {STALE_DAYS} gündür görülmedi',
                     'Ne aramada ne ilan sayfasında görüldü; satılmış, kalkmış ya da fiyatı değişmiş olabilir.', stale)] if stale else []


def stuck_work(store, mode, now):
    """Work the system set aside after repeated failures is shown, not forgotten: a step that failed `queue.stuck_after` times today is not tried again until
    tomorrow, and an inbox row twice. That is right for a failing step and wrong when nobody can see it: two unread conversations sat unread for a day."""
    from verda.agency import inbox
    from verda.agency.tuning import number
    limit = number(store, 'queue.stuck_after')
    with store.transaction() as con:
        steps = [dict(r) for r in con.execute(
            "SELECT tool, json_extract(arguments,'$.listing_id') AS listing, count(*) AS n FROM tool_calls WHERE state IN ('failed','uncertain') AND finished_at>? "
            "AND json_extract(arguments,'$.listing_id') IS NOT NULL GROUP BY 1,2 HAVING n>=?", (now - DAY, limit))]
        # a step that succeeded afterwards is no longer stuck
        steps = [s for s in steps if not con.execute("SELECT 1 FROM tool_calls WHERE tool=? AND state='complete' AND json_extract(arguments,'$.listing_id')=? AND finished_at>? LIMIT 1",
                                                      (s['tool'], s['listing'], now - DAY)).fetchone()]
    rows = [key for key, n in inbox.failed_row_counts(store, now).items() if n >= inbox.STUCK_ROW_AFTER]
    if not steps and not rows:
        return []
    kinds = sorted({s['tool'] for s in steps} | ({'sahibinden.read_thread (gelen kutusu satırı)'} if rows else set()))
    return [_finding('stuck_work', ACTION, f'{len(steps) + len(rows)} iş bugün tekrar tekrar başarısız oldu ve yarına bırakıldı',
                     'Otomatik denenmiyor: ' + ', '.join(kinds) + '. Nedeni çözülmedikçe aynı iş yarın yine takılır.', [s['listing'] for s in steps], count=len(steps) + len(rows))]


def publisher_silent(store, mode, now):
    """The dashboard is fed: the publisher wrote within the last twenty minutes."""
    status = Path(store.path).parent / 'publisher-status.json'
    manifest = Path(store.path).parent / 'firestore-manifest.json'
    try:
        last = json.loads(status.read_text())
    except (OSError, ValueError):
        last = None
    if last is None and not manifest.exists():
        return []
    ok_at = (last or {}).get('last_ok_at') or (manifest.stat().st_mtime if manifest.exists() else 0)
    if now - ok_at <= PUBLISH_SILENCE:
        return []
    error = (last or {}).get('error') or ''
    why = ('Firestore kotası doldu (429): yayıncı bekleme süresini artırarak yeniden deniyor; kota her gün 10:00 civarı (İstanbul) yenilenir. '
           'Kota doluyken pano da veri okuyamaz.') if '429' in error else f'Son hata: {error}.' if error else 'Pano yayıncısı çalışmıyor olabilir.'
    return [_finding('publisher_silent', ACTION if '429' in error else WARN, f'Pano {(now - ok_at) / 60:.0f} dakikadır güncellenmedi', why + ' Panodaki bilgiler eski.')]


def worker_silent(store, mode, now):
    """Work that is due gets picked up: no queued task waits half an hour while the agent worker makes no move."""
    with store.transaction() as con:
        due = con.execute("SELECT count(*) FROM inbox WHERE mode=? AND state='queued' AND available_at<?", (mode, now - WORKER_SILENCE)).fetchone()[0]
        last = con.execute('SELECT max(at) FROM agency_events').fetchone()[0] or 0
    if due and now - last > WORKER_SILENCE:
        return [_finding('worker_silent', WARN, f'{due} iş bekliyor, ajan işçisi {(now - last) / 60:.0f} dakikadır hareketsiz',
                         'Ajan işçisi kapalı ya da takılmış olabilir. Bilerek durdurulduysa bu beklenen bir durumdur.', count=due)]
    return []


RULES = (
    ('repeat_question', 'Cevaplanmış soru tekrar sorulmaz', repeat_question),
    ('send_policy', 'Mesaj saati ve aralığı', send_policy),
    ('send_loop', 'Aynı ilana tekrar tekrar gönderim denenmez', send_loop),
    ('replies_waiting', 'Satıcı cevabı bir günde işlenir', replies_waiting),
    ('conversation_lost', 'Okunmuş konuşma kaybolmaz', conversation_lost),
    ('inbox_list_short', 'Gelen kutusu listesi eksik kalmaz', inbox_list_short),
    ('exclusion_doubt', 'Alan elemesi doğru araziye dayanır', exclusion_doubt),
    ('step_loop', 'Aynı adım döngüye girmez', step_loop),
    ('stuck_work', 'Takılan iş görünür kalır', stuck_work),
    ('tool_failures', 'Araçların hata oranı', tool_failures),
    ('scan_coverage', 'Pazarın tamamı taranır', scan_coverage),
    ('stale_listings', 'Aktif ilanlar haftada bir görülür', stale_listings),
    ('publisher_silent', 'Pano güncel', publisher_silent),
    ('worker_silent', 'Bekleyen iş alınır', worker_silent),
)
ORDER = {CRITICAL: 0, ACTION: 1, WARN: 2}


def check(store, args=None, *, mode='local', now=None):
    """Agent tool `watchdog.check` and the publisher's status check. -> {checked_at, ok, findings (most severe first), rules}."""
    now = time.time() if now is None else now
    findings, rules = [], []
    for key, label, rule in RULES:
        try:
            found = rule(store, mode, now)
        except Exception as error:                     # a rule that cannot be checked is a finding, never a pass
            found = [_finding(key, WARN, f'Nöbetçi kuralı kontrol edilemedi: {label}', type(error).__name__)]
            rules.append({'rule': key, 'label': label, 'status': 'error', 'error': type(error).__name__})
        else:
            rules.append({'rule': key, 'label': label, 'status': 'broken' if found else 'ok', 'count': sum(f['count'] for f in found)})
        findings += found
    findings.sort(key=lambda f: ORDER[f['severity']])
    return {'checked_at': now, 'ok': not findings, 'findings': findings, 'rules': rules}
