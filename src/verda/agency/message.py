"""Seller messages: what may be asked, when, and the durable record of every attempt.

The text is written by code from what is still unknown, never by a model. A message is sent only when every gate here
agrees; the attempt is recorded before the click and checked after it. Nothing in this module touches a browser.

Outbox states: prepared -> sending -> confirmed | delivery_unknown | dry_run (typed and checked, not sent) | cancelled.
`sending` with phase `click_intent` means the click may have happened: such a row is never retried by itself.
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from verda.facts.text import fold
from verda.legacy import digest

TZ = ZoneInfo('Europe/Istanbul')
WINDOW_START, WINDOW_END = (8, 30), (21, 0)            # 08:30 included, 21:00 excluded
TEMPLATE = 'v1'
ASK_ORDER = ('parcel_key', 'access', 'natural_sit', 'archaeological_sit')
QUESTIONS = {'parcel_key': 'ada/parsel bilgisini paylaşabilir misiniz?',
             'access': 'Parselin doğrudan kadastral yol cephesi var mı?',
             'natural_sit': 'Parsel herhangi bir doğal sit statüsünde mi?',
             'archaeological_sit': 'Parsel herhangi bir arkeolojik sit alanında mı?'}
QUESTION_ENCLOSED = 'Parselin kadastral yol cephesi görünmüyor; yola geçiş hakkınız (irtifak, mahkeme yolu vb.) var mı?'      # for a parcel the official geometry shows enclosed by numbered neighbours
FIELD_NAME = {'parcel_key': 'ada/parsel', 'access': 'kadastral yol', 'natural_sit': 'doğal sit', 'archaeological_sit': 'arkeolojik sit'}
MODES = ('off', 'dry_run', 'live')
CANDIDATE_STATES = ('research_required', 'conditional', 'eligible')
ACTIVE = ('prepared', 'sending', 'dry_run', 'confirmed', 'delivery_unknown')

REFUSALS = {
    'sender_off': 'Mesaj gönderme kapalı (Ayarlar).',
    'too_soon': 'Önceki gerçek mesajın üzerinden yeterli süre geçmedi.',
    'outside_window': 'Mesaj saati dışında (08:30–21:00).',
    'one_message_per_run': 'Bir çalışmada en çok bir mesaj gönderilir.',
    'not_a_candidate': 'İlan artık aday değil (elendi, yayından kalktı ya da 45 dk dışında).',
    'no_ask_step': 'Kural motoru bu ilan için şu an satıcıya soru sormayı öngörmüyor.',
    'nothing_to_ask': 'Sorulacak eksik kalmadı.',
    'waiting_for_reply': 'Satıcıya soru gönderildi, cevap bekleniyor; tekrar yazılmaz.',
    'delivery_unresolved': 'Önceki mesajın teslimi doğrulanmadı; sen karar verene kadar yeni mesaj yazılmaz.',
    'seller_closed': 'Satıcı mesaj almıyor ya da ilan yayında değil; tekrar denenmez.',
    'already_asked': 'Bu sorular zaten gönderildi.',
    'dry_run_done': 'Bu ilan için deneme zaten yapıldı.',
    'dry_run_limit': 'Deneme sınırına ulaşıldı; önce denemeleri incele.',
    'listing_changed': 'İlan metni okunduğundan beri değişti; önce yeniden okunacak.',
    'text_not_allowed': 'Mesaj metni kurallara uymuyor.',
    'asked_before': 'Bu konu satıcıyla olan konuşmada zaten geçti (daha önce soruldu ya da satıcı kendisi yazdı); tekrar sorulmaz, yanıtı sen değerlendir.',
    'conversation_unconfirmed': 'Bu ilanın konuşması daha önce okunmuştu ama son okuma onu bulamadı; konuşma yeniden bulunmadan mesaj yazılmaz.',
}


# ---- time -----------------------------------------------------------------------------------------------------------
def local(now):
    return datetime.fromtimestamp(now, TZ)


def within_window(now):
    t = local(now)
    return WINDOW_START <= (t.hour, t.minute) < WINDOW_END


# ---- text -----------------------------------------------------------------------------------------------------------
def render(listing_id, fields, enclosed=False):
    """The one message for these fields, in a fixed order. Raises ValueError for fields that cannot be asked."""
    wanted = [f for f in ASK_ORDER if f in set(fields)]
    if not wanted or set(fields) - set(ASK_ORDER):
        raise ValueError('Nothing askable')
    sentences = [QUESTION_ENCLOSED if (f == 'access' and enclosed) else QUESTIONS[f] for f in wanted]
    sentences[0] = sentences[0][0].lower() + sentences[0][1:]            # follows "için"
    return f"Merhaba, {listing_id} numaralı taşınmaz için " + ' '.join(sentences) + ' Teşekkürler.'


FORBIDDEN = ('asistan', 'yapay zeka', 'yapay zekâ', 'arayın', 'aramayın', 'whatsapp', 'karavan', 'tiny house', 'iban', 'tc kimlik')


def check_text(text, listing_id=None):
    """The message must be exactly what the template makes: short, no phone, no AI mention, no other topic."""
    folded = fold(text)
    rest = text.replace(str(listing_id), '', 1) if listing_id else text
    if len(text) > 700 or re.search(r'\d{7,}', rest) or any(fold(w) in folded for w in FORBIDDEN):
        raise ValueError('Message text not allowed')
    return text


# ---- page signals (the sender reads these from the form page) -----------------------------------------------------------
STANDARD_BANNER = re.compile(r'mesaj gönderdiğinizde [\d+()\s-]+ numaralı telefonunuz karşı tarafla paylaşılacaktır\.?')
UNAVAILABLE = (('bu ilan yayında değil', 'listing_not_live'), ('ilan sahibi ilanıyla ilgili mesaj almamayı tercih ediyor', 'seller_messages_disabled'))


def normalize(text):
    return ' '.join(str(text or '').split())


def tr_lower(text):
    """Lower case for Turkish: İ is i and I is ı (str.casefold would leave a combining dot)."""
    return normalize(text).replace('İ', 'i').replace('I', 'ı').lower()


def unavailable_reason(page_text):
    low = tr_lower(page_text)
    return next((code for needle, code in UNAVAILABLE if needle in low), None)


def phone_banner(page_text):
    """'none', 'standard' (Sahibinden's own reciprocal-sharing banner, which the user accepted) or 'other' (stop)."""
    low = tr_lower(page_text)
    if not (('telefon numaranız' in low or 'numaralı telefonunuz' in low) and any(w in low for w in ('paylaş', 'göster', 'ilet'))):
        return 'none'
    return 'standard' if STANDARD_BANNER.search(low) else 'other'


def exact_in(message, text):
    return normalize(message) in normalize(text)


# ---- the outbox -----------------------------------------------------------------------------------------------------
def ensure(con):
    con.execute('''CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, listing_ref TEXT NOT NULL, mode TEXT NOT NULL, fields TEXT NOT NULL,
        text TEXT NOT NULL, text_hash TEXT NOT NULL, template TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, phase TEXT,
        reason TEXT, call_id TEXT, task_id TEXT, thread_url TEXT, evidence TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, sent_at REAL)''')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS outbox_unique ON outbox(listing_ref, mode, fingerprint)')


def _row(r):
    d = dict(r)
    d['fields'] = json.loads(d['fields'])
    d['evidence'] = json.loads(d['evidence']) if d.get('evidence') else None
    return d


def rows(con, listing_ref, mode='local'):
    ensure(con)
    return [_row(r) for r in con.execute('SELECT * FROM outbox WHERE listing_ref=? AND mode=? ORDER BY created_at, id', (listing_ref, mode))]


def count(con, state, mode='local'):
    ensure(con)
    return con.execute('SELECT count(*) FROM outbox WHERE mode=? AND state=?', (mode, state)).fetchone()[0]


def fingerprint(listing_id, fields, text):
    return digest({'listing': listing_id, 'fields': sorted(fields), 'text': text, 'template': TEMPLATE})[:32]


def prepare(con, *, listing_ref, mode, fields, text, now):
    """The draft for these fields: the existing row when the same one was prepared before, else a new `prepared` row."""
    ensure(con)
    fp = fingerprint(listing_ref, fields, text)
    old = con.execute('SELECT * FROM outbox WHERE listing_ref=? AND mode=? AND fingerprint=?', (listing_ref, mode, fp)).fetchone()
    if old:
        return _row(old)
    from uuid import uuid4
    key = uuid4().hex
    con.execute('''INSERT INTO outbox(id,listing_ref,mode,fields,text,text_hash,template,fingerprint,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)''',
                (key, listing_ref, mode, json.dumps(sorted(fields)), text, digest({'t': text})[:32], TEMPLATE, fp, now, now))
    return _row(con.execute('SELECT * FROM outbox WHERE id=?', (key,)).fetchone())


def claim(con, outbox_id, call_id, task_id, now, *, live):
    """Hand the row to one tool call. Only a prepared row (or a dry run, when going live) can be claimed."""
    allowed = ('prepared', 'dry_run') if live else ('prepared',)
    cur = con.execute(f"UPDATE outbox SET state='sending',phase=NULL,call_id=?,task_id=?,reason=NULL,updated_at=? WHERE id=? AND state IN ({','.join('?' * len(allowed))})",
                      (call_id, task_id, now, outbox_id, *allowed))
    if cur.rowcount != 1:
        raise ValueError('Outbox row is not available to send')


def checkpoint(con, outbox_id, call_id, text_hash, now):
    """The click intent. True only while this call still owns the row and the text is the one that was approved."""
    cur = con.execute("UPDATE outbox SET phase='click_intent',updated_at=? WHERE id=? AND call_id=? AND state='sending' AND phase IS NULL AND text_hash=?",
                      (now, outbox_id, call_id, text_hash))
    return cur.rowcount == 1


def clicked_possibly(con, outbox_id):
    row = con.execute('SELECT state,phase FROM outbox WHERE id=?', (outbox_id,)).fetchone()
    return bool(row and row['state'] == 'sending' and row['phase'] == 'click_intent')


def finish(con, outbox_id, call_id, status, now, *, reason=None, thread_url=None, evidence=None):
    """Move the row to where the receipt says it is. A row that was not claimed by this call is left alone."""
    row = con.execute('SELECT * FROM outbox WHERE id=? AND call_id=?', (outbox_id, call_id)).fetchone()
    if row is None or row['state'] != 'sending':
        raise ValueError('Outbox row is not owned by this call')
    possibly_clicked = row['phase'] == 'click_intent'
    if status == 'not_sent':
        # Nothing was clicked: back to prepared, unless the click may already have happened.
        state = 'delivery_unknown' if possibly_clicked else 'prepared'
    elif status in ('confirmed', 'dry_run', 'cancelled', 'delivery_unknown'):
        state = status
    else:
        raise ValueError('Unknown send status')
    if state == 'dry_run' and possibly_clicked:
        state = 'delivery_unknown'                        # a dry run never clicks: an intent here means something is wrong
    sent_at = now if state == 'confirmed' else None
    con.execute('''UPDATE outbox SET state=?,phase=NULL,reason=?,thread_url=COALESCE(?,thread_url),evidence=?,sent_at=COALESCE(?,sent_at),updated_at=? WHERE id=?''',
                (state, reason, thread_url, json.dumps(evidence, ensure_ascii=False) if evidence is not None else None, sent_at, now, outbox_id))
    return state


def resolve_unknown(con, outbox_id, delivered, now, note):
    """The user's (or a thread read's) answer for a message whose delivery was unknown."""
    row = con.execute("SELECT * FROM outbox WHERE id=? AND state='delivery_unknown'", (outbox_id,)).fetchone()
    if row is None:
        raise ValueError('Only an unconfirmed message can be settled')
    con.execute("UPDATE outbox SET state=?,reason=?,sent_at=CASE WHEN ? THEN COALESCE(sent_at,?) ELSE sent_at END,updated_at=? WHERE id=?",
                ('confirmed' if delivered else 'cancelled', note[:300], 1 if delivered else 0, now, now, outbox_id))


def reconcile_from_thread(con, listing_ref, mode, messages, now):
    """A later read of the conversation settles a message whose delivery was unknown: our exact text in it means it went out."""
    ensure(con)
    settled = 0
    for row in con.execute("SELECT * FROM outbox WHERE listing_ref=? AND mode=? AND state='delivery_unknown'", (listing_ref, mode)).fetchall():
        if any(m.get('role') == 'user' and exact_in(row['text'], m.get('text')) for m in messages or []):
            con.execute("UPDATE outbox SET state='confirmed',reason='found_in_a_later_thread_read',sent_at=COALESCE(sent_at,?),updated_at=? WHERE id=?", (now, now, row['id']))
            settled += 1
    return settled


# ---- the second check: the whole history, not the plan ---------------------------------------------------------------------
# The plan reads one conversation read. This check reads every record itself, so a later read that missed the conversation, a
# reply that was never turned into a fact, or a planner mistake cannot make the system ask the same thing twice.
def overlap(fields, messages):
    """The fields of a new message that the conversation already covers. messages: [{'who': 'sen' | 'satıcı', 'text'}].
    'asked': we asked about it before (answered or not); 'seller_wrote': the seller brought it up without being asked."""
    from verda.agency.learning import covers
    asked, told = set(), set()
    for m in messages:
        (asked if m['who'] == 'sen' else told).update(covers(m.get('text') or ''))
    wanted = set(fields)
    return {'asked': [f for f in ASK_ORDER if f in wanted & asked], 'seller_wrote': [f for f in ASK_ORDER if f in wanted & told]}


def legacy_sent(store, listing_ref):
    """The texts the old system sent (or may have sent) to this listing's seller."""
    from verda.agency.research import archive_connection
    archive = archive_connection(store)
    if not archive:
        return []
    try:
        snapshot = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
        rows = archive.execute("SELECT payload FROM legacy_rows WHERE snapshot_id=? AND table_name='outbox' AND subject_id=?", (snapshot['id'], listing_ref)) if snapshot else []
        return [{'who': 'sen', 'text': o.get('message_text') or ''} for o in (json.loads(r['payload']) for r in rows) if o.get('state') in ('confirmed', 'delivery_unknown')]
    finally:
        archive.close()


def history(store, listing_ref, mode):
    """Every message any read of this listing's conversation ever showed, and every message this system or the old one sent."""
    messages, reads = [], []
    with store.transaction() as con:
        for r in con.execute("SELECT payload FROM observations WHERE listing_ref=? AND mode=? AND tool='sahibinden.read_thread' ORDER BY captured_at, id", (listing_ref, mode)):
            p = json.loads(r['payload'])
            reads.append(bool(p.get('found')))
            messages += [{'who': 'sen' if m.get('role') == 'user' else 'satıcı', 'text': m.get('text') or ''} for m in p.get('messages') or []]
        ensure(con)
        messages += [{'who': 'sen', 'text': r['text']} for r in con.execute(
            "SELECT text FROM outbox WHERE listing_ref=? AND mode=? AND (state IN ('confirmed','delivery_unknown') OR phase='click_intent')", (listing_ref, mode))]
    if mode == 'local':
        messages += legacy_sent(store, listing_ref)
    return {'messages': messages, 'found_before': any(reads), 'latest_found': reads[-1] if reads else None}


def history_refusal(store, listing_ref, mode, fields):
    """None when the whole history allows asking these fields, else the refusal. Run at the gate and again right before the click."""
    seen = history(store, listing_ref, mode)
    hit = overlap(fields, seen['messages'])
    if hit['asked'] or hit['seller_wrote']:
        return {'ok': False, 'reason': 'asked_before', 'message': REFUSALS['asked_before'], 'fields': hit}
    if seen['found_before'] and seen['latest_found'] is False:
        return {'ok': False, 'reason': 'conversation_unconfirmed', 'message': REFUSALS['conversation_unconfirmed']}
    return None


# ---- the gates ------------------------------------------------------------------------------------------------------------
def askable_fields(current, built):
    """The unknown fields that may be asked now, in message order.

    A field is asked only when its own prerequisite is met (the conversation was read; the road only after the official
    geometry check) and nothing is already waiting on it (asked, answered unclearly, or noted for the user)."""
    seen = current.get('seen') or {}
    fields = built['fields']
    out = []
    for f in ASK_ORDER:
        state = (fields.get(f) or {}).get('state')
        if state not in ('to_ask', 'draft', 'dry_run', 'none', 'planned', 'waiting'):      # a dry run sent nothing: the field is still unasked
            continue
        if f == 'parcel_key' or seen.get('thread_read'):
            if f == 'access' and not (seen.get('corridor_checked') or not seen.get('tkgm_looked_up')):
                continue
            out.append(f)
    return out


def plan(*, listing_id, current, built, convo, outbox, mode, now, sent_this_run=0, dry_runs=0, dry_run_limit=3, last_sent_at=None, gap_minutes=3):
    """Everything that decides whether, and what, to send. Returns {'ok': True, fields, text, fingerprint} or {'ok': False, 'reason'}.

    current: the listing's fact status; built: learning.build(...); convo: the last conversation read; outbox: this listing's rows."""
    def no(code):
        return {'ok': False, 'reason': code, 'message': REFUSALS[code]}
    if mode not in ('dry_run', 'live'):
        return no('sender_off')
    if mode == 'live' and not within_window(now):                # a dry run sends nothing, so the clock does not matter for it
        return no('outside_window')
    if sent_this_run >= 1:
        return no('one_message_per_run')
    if mode == 'live' and last_sent_at is not None and now - last_sent_at < gap_minutes * 60:
        return no('too_soon')
    decision = (current.get('decision') or {}).get('state')
    if decision not in CANDIDATE_STATES or current.get('legacy_lifecycle') == 'excluded' or (current.get('seen') or {}).get('listing_removed'):
        return no('not_a_candidate')
    if not str((current.get('next_action') or {}).get('action', '')).startswith('ask:'):
        return no('no_ask_step')
    if any(o['state'] == 'cancelled' and o.get('reason') in ('seller_messages_disabled', 'listing_not_live') for o in outbox):
        return no('seller_closed')                               # the page said so; asking again every sweep would only repeat the refusal
    if any(o['state'] in ('sending', 'delivery_unknown') for o in outbox):
        return no('delivery_unresolved')
    seller_wrote_last = bool(convo and convo.get('found') and convo.get('messages') and convo['messages'][-1]['who'] != 'sen')
    last_row = outbox[-1] if outbox else None
    if last_row and last_row['state'] == 'prepared' and last_row.get('reason') == 'waiting_for_reply' and now - (last_row.get('updated_at') or 0) < 86400 and not seller_wrote_last:
        return no('waiting_for_reply')         # the page itself showed our message last; a conversation read that cannot confirm it does not undo that
    if convo and convo.get('found') and convo.get('messages') and convo['messages'][-1]['who'] == 'sen':
        return no('waiting_for_reply')
    fields = askable_fields(current, built)
    if not fields:
        return {**no('nothing_to_ask'), 'replied': [f for f in ASK_ORDER if (built['fields'].get(f) or {}).get('state') == 'replied']}
    text = render(listing_id, fields, enclosed=bool((current.get('seen') or {}).get('corridor_enclosed')))
    try:
        check_text(text, listing_id)
    except ValueError:
        return no('text_not_allowed')
    fp = fingerprint(listing_id, fields, text)
    same = [o for o in outbox if o['fingerprint'] == fp]
    if any(o['state'] == 'confirmed' for o in same):
        return no('already_asked')
    if mode == 'dry_run':
        if any(o['state'] == 'dry_run' for o in same):
            return no('dry_run_done')
        if dry_runs >= dry_run_limit:
            return no('dry_run_limit')
    return {'ok': True, 'fields': fields, 'text': text, 'fingerprint': fp, 'dry_run': mode == 'dry_run'}


def mode_of(store):
    from verda.agency.tuning import choice
    return choice(store, 'sender.mode')


def last_sent_at(con, mode='local'):
    """When the most recent real message went out (or may have)."""
    ensure(con)
    row = con.execute("SELECT max(COALESCE(sent_at, updated_at)) AS at FROM outbox WHERE mode=? AND state IN ('confirmed','delivery_unknown')", (mode,)).fetchone()
    return row['at'] if row and row['at'] else None


def evaluate(store, listing_id, run_mode, now, *, task_id=None, current=None):
    """The gates for one listing, from the records. Returns (verdict, detail, state) and writes nothing."""
    from verda.agency.learning import _conversation, build, context_of, gather, unknown_fields
    from verda.agency.research import fact_status
    from verda.agency.tuning import number
    mode = mode_of(store)
    if mode == 'off' or run_mode != 'local':
        return {'ok': False, 'reason': 'sender_off', 'message': REFUSALS['sender_off']}, None, mode
    current = current or fact_status(store, listing_id, mode=run_mode)
    data = gather(store, listing_id, run_mode)
    built = build(data, unknown_fields(current), current['next_action'], context_of(current))
    with store.transaction() as con:
        existing = rows(con, listing_id, run_mode)
        sent_this_run = con.execute("SELECT count(*) FROM tool_calls WHERE task_id=? AND tool='sahibinden.send_message'", (task_id,)).fetchone()[0] if task_id else 0
        dry_runs, last = count(con, 'dry_run', run_mode), last_sent_at(con, run_mode)
        page = con.execute("SELECT payload FROM observations WHERE listing_ref=? AND mode=? AND tool='sahibinden.read_listing' ORDER BY captured_at DESC, id DESC LIMIT 1",
                           (listing_id, run_mode)).fetchone()
    detail = json.loads(page['payload']) if page else None
    verdict = plan(listing_id=listing_id, current=current, built=built, convo=_conversation(data['thread']), outbox=existing, mode=mode, now=now,
                   sent_this_run=sent_this_run, dry_runs=dry_runs, dry_run_limit=number(store, 'sender.dry_run_limit'), last_sent_at=last,
                   gap_minutes=number(store, 'sender.gap_minutes'))
    if verdict['ok'] and (not detail or detail.get('listing_id') != listing_id or not detail.get('url')):
        verdict = {'ok': False, 'reason': 'no_ask_step', 'message': REFUSALS['no_ask_step']}
    if verdict['ok']:
        verdict = history_refusal(store, listing_id, run_mode, verdict['fields']) or verdict
    return verdict, detail, mode


def preview(store, listing_id, run_mode, now, current=None):
    """What would be asked now, or why not. For the queue and the panel; nothing is prepared or written."""
    return evaluate(store, listing_id, run_mode, now, current=current)[0]


def gate(store, task, arguments, now):
    """Everything between an agent asking to send and the browser being asked: the verdict and, when it is yes, the frozen job.

    {'ok': False, 'reason', 'message'} or {'ok': True, 'arguments': {...}}. The model supplies only the listing number; the text,
    the fields and the listing digest come from records. Creates (or reuses) the `prepared` outbox row."""
    from verda.agency.send import live_digest
    listing_id = arguments['listing_id']
    verdict, detail, _ = evaluate(store, listing_id, task['mode'], now, task_id=task['id'])
    if not verdict['ok']:
        return verdict
    with store.transaction() as con:
        row = prepare(con, listing_ref=listing_id, mode=task['mode'], fields=verdict['fields'], text=verdict['text'], now=now)
    return {'ok': True, 'outbox': row, 'arguments': {'listing_id': listing_id, 'outbox_id': row['id'], 'text': row['text'], 'text_hash': row['text_hash'], 'fields': row['fields'],
                                                     'dry_run': verdict['dry_run'], 'listing_url': detail['url'], 'listing_digest': live_digest(detail)}}
