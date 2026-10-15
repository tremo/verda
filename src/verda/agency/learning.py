"""What the system has done to learn what a listing still lacks, and what came of it.

Read-only and exact: every line comes from a record (the last read of the Sahibinden conversation, a message the old
system sent, an open task, an open source incident, a note waiting for the user). Nothing here is a model's opinion and
nothing is written. Where nothing was done the line says so, and where sending is not connected it says that too.
"""
import json
import re
from datetime import datetime

from verda.facts.text import fold

CRITERIA = ('parcel_key', 'shared_title', 'archaeological_sit', 'natural_sit', 'access')
ASKABLE = ('parcel_key', 'archaeological_sit', 'natural_sit', 'access', 'sale_scope')    # a seller can answer these in a message
OPEN_TASK_STATES = ('queued', 'running', 'waiting_browser', 'waiting_children', 'waiting_user', 'blocked', 'uncertain')
TASK_STATE = {'queued': 'sırada', 'running': 'çalışıyor', 'waiting_browser': 'tarayıcıda çalışıyor', 'waiting_children': 'alt görevleri bekliyor',
              'waiting_user': 'senden bir şey bekliyor', 'blocked': 'durdu', 'uncertain': 'sonucu belirsiz'}
CHECKS = {'sahibinden.read_listing': 'İlan sayfası okundu', 'sahibinden.read_thread': 'Satıcı konuşması kontrol edildi',
          'tkgm.lookup': 'Resmî parsel (TKGM) sorgulandı', 'tkgm.corridor': 'Yol koridoru incelendi', 'geo.route': 'Mertur rotası hesaplandı'}
RESOURCE_LABEL = {'tkgm': 'TKGM erişimi', 'sahibinden': 'Sahibinden erişimi', 'sahibinden_login': 'Sahibinden girişi', 'geography': 'Rota servisi'}
PLAN_FIELDS = {'ask:sit': ('archaeological_sit', 'natural_sit'), 'ask:access': ('access',), 'ask:parcel_identity': ('parcel_key',), 'ask:sale_scope': ('sale_scope',),
               'sahibinden.read_thread': ASKABLE, 'sahibinden.read_listing': ('parcel_key', 'shared_title') + ASKABLE,
               'tkgm.lookup': ('parcel_key',), 'geo.route': ('route_minutes',)}
PLAN_LABEL = {'sahibinden.read_thread': 'Satıcı konuşması okunacak', 'sahibinden.read_listing': 'İlan sayfası okunacak',
              'tkgm.lookup': 'Resmî parsel sorgusu sırada', 'geo.route': 'Rota hesabı sırada'}
FIELD_TR = {'parcel_key': 'ada/parsel', 'archaeological_sit': 'arkeolojik sit', 'natural_sit': 'doğal sit', 'access': 'kadastral yol', 'shared_title': 'hisse', 'sale_scope': 'satış kapsamı'}
SENDING = {'off': 'Mesaj gönderme kapalı (Ayarlar); açılınca sorulacak.', 'dry_run': 'Şu an deneme modunda: mesaj yazılır ve doğrulanır ama gitmez.', 'live': 'Sıradaki gönderim çalışmasında tek mesaj olarak sorulacak.'}


def _epoch(value):
    """Seconds since the epoch from an ISO text, a 'dd.mm.yyyy HH:MM' text or a number; None when unknown."""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def legacy_fields(names):
    """The fields an old outbox message asked about (its names are the old system's)."""
    out = set()
    for name in names or []:
        n = str(name).lower()
        if 'archaeolog' in n:
            out.add('archaeological_sit')
        if 'natural' in n:
            out.add('natural_sit')
        if n == 'sit_status' or n.endswith('_sit_status'):
            out.update(('archaeological_sit', 'natural_sit'))
        if 'road' in n or 'frontage' in n:
            out.add('access')
        if n.startswith('ada') or 'parcel' in n:
            out.add('parcel_key')
    return out


def covers(text):
    """The fields a sent message asks about, from its own words."""
    f = fold(text)
    out = set()
    if re.search(r'\barkeolojik\b', f):
        out.add('archaeological_sit')
    if re.search(r'\bdoğal\b', f):
        out.add('natural_sit')
    if re.search(r'\bsit\b|\bsitl|\bsit alan', f) and not ({'archaeological_sit', 'natural_sit'} & out):
        out.update(('archaeological_sit', 'natural_sit'))
    if re.search(r'\byol', f):
        out.add('access')
    if re.search(r'\bparsel|\bada\b', f):
        out.add('parcel_key')
    if re.search(r'\bhisse|\btapu|m²|\bm2\b|metrekare|\bdönüm', f):
        out.add('sale_scope')                 # what exactly is for sale: shares, deeds, the area
    return out


def gather(store, listing_id, mode='local', archive=None, snapshot=None):
    """The records about one listing, as plain data. A caller reading many listings passes one open archive and its snapshot id."""
    from verda.agency.research import archive_connection
    from verda.facts.flags import open_flags
    with store.transaction() as con:
        thread = con.execute("""SELECT payload,captured_at FROM observations WHERE mode=? AND listing_ref=? AND tool='sahibinden.read_thread'
            ORDER BY (json_extract(payload, '$.found') = 1) DESC, captured_at DESC, id DESC LIMIT 1""", (mode, listing_id)).fetchone()   # a later read that did not find the conversation (found: false) never hides one that was read
        checks = {r['tool']: r['at'] for r in con.execute(f"""SELECT tool, MAX(captured_at) AS at FROM observations WHERE mode=? AND listing_ref=?
            AND tool IN ({','.join('?' * len(CHECKS))}) GROUP BY tool""", (mode, listing_id, *CHECKS))}
        tasks = [dict(r) for r in con.execute(f"""SELECT id,agent,objective,state,reason,created_at FROM inbox WHERE mode=? AND listing_ref=?
            AND state IN ({','.join('?' * len(OPEN_TASK_STATES))}) ORDER BY created_at DESC LIMIT 6""", (mode, listing_id, *OPEN_TASK_STATES))]
        incidents = [dict(r) for r in con.execute("SELECT resource,reason,created_at FROM source_incidents WHERE mode=? AND state='open' ORDER BY created_at", (mode,))]
        flags = open_flags(con, listing_id, mode)
        from verda.agency import message
        mine = message.rows(con, listing_id, mode)
    outbox, own = [], archive is None
    archive = archive if archive is not None else (archive_connection(store) if mode == 'local' else None)
    if archive:
        try:
            if snapshot is None:
                found = archive.execute('SELECT id FROM legacy_snapshots ORDER BY imported_at DESC LIMIT 1').fetchone()
                snapshot = found['id'] if found else None
            for row in archive.execute("SELECT payload FROM legacy_rows WHERE snapshot_id=? AND table_name='outbox' AND subject_id=?", (snapshot, listing_id)) if snapshot else []:
                o = json.loads(row['payload'])
                outbox.append({'state': o.get('state'), 'asked': sorted(legacy_fields(_json_list(o.get('missing_fields_json')))), 'prepared_at': _epoch(o.get('prepared_at')),
                               'sent_at': _epoch(o.get('confirmed_at')), 'text': (o.get('message_text') or '')[:600], 'url': o.get('message_url') or None})
        finally:
            if own:
                archive.close()
    # this system's own attempts, next to the old system's
    outbox += [{'state': o['state'], 'asked': list(o['fields']), 'prepared_at': o['created_at'], 'sent_at': o['sent_at'], 'text': o['text'][:600], 'url': o['thread_url'], 'updated_at': o['updated_at']} for o in mine]
    return {'thread': None if thread is None else {**json.loads(thread['payload']), 'checked_at': thread['captured_at']}, 'checks': checks,
            'tasks': tasks, 'incidents': incidents, 'flags': flags, 'outbox': outbox, 'sender_mode': __import__('verda.agency.message', fromlist=['x']).mode_of(store)}


def _json_list(value):
    try:
        data = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _conversation(thread):
    if not thread:
        return None
    messages = [{'who': 'sen' if m.get('role') == 'user' else 'satıcı', 'text': (m.get('text') or '')[:600], 'at': _epoch(m.get('sent_at_text'))}
                for m in thread.get('messages', [])]
    return {'found': bool(thread.get('found')), 'url': thread.get('url') if thread.get('found') else None, 'checked_at': thread.get('checked_at'), 'messages': messages,
            'older_omitted': bool(thread.get('older_omitted'))}


def build(data, unknown, next_action=None, context=None):
    """Per unknown field the state and the plain sentence for it, plus the list of what was done (newest first)."""
    convo = _conversation(data['thread'])
    flags = {f['field']: f for f in data['flags']}
    sent = [o for o in data['outbox'] if o['state'] == 'confirmed']
    undelivered = [o for o in data['outbox'] if o['state'] == 'delivery_unknown']
    drafts = [o for o in data['outbox'] if o['state'] == 'prepared']
    dry = [o for o in data['outbox'] if o['state'] == 'dry_run']
    going = [o for o in data['outbox'] if o['state'] == 'sending']
    halted = {i['resource']: i for i in data['incidents']}
    plan = (next_action or {}).get('action', '')
    about = PLAN_FIELDS.get(plan, ())
    context = context or {}
    mode = data.get('sender_mode', 'off')
    link = convo['url'] if convo and convo['url'] else None

    def asked_about(field):
        """(message index in the live conversation or None, sent-at, link) for the latest question that covers the field."""
        index = None
        if convo:
            for i, m in enumerate(convo['messages']):
                if m['who'] == 'sen' and field in covers(m['text']):
                    index = i
        legacy = [o for o in sent if field in o['asked']]
        if index is None and not legacy:
            return None
        o = max(legacy, key=lambda x: x['sent_at'] or 0) if legacy else None
        at = (convo['messages'][index]['at'] if index is not None else None) or (o['sent_at'] if o else None)
        return {'index': index, 'at': at, 'url': link or (o['url'] if o else None)}

    fields, blocking = {}, set()          # blocking: the halted sources that really hold this listing back
    for field in unknown:
        out = {'state': 'none', 'label': 'Henüz bir aksiyon alınmadı', 'at': None, 'url': None}
        flag = flags.get(field)
        ask = asked_about(field) if field in ASKABLE else None
        if flag:
            out = {'state': 'unclear', 'label': 'Satıcı cevapladı ama cevap net değil; senin netleştirmeni bekliyor', 'at': flag['created_at'], 'url': link}
        elif ask:
            replied = convo and ask['index'] is not None and any(m['who'] == 'satıcı' for m in convo['messages'][ask['index'] + 1:])
            if replied:
                out = {'state': 'replied', 'label': 'Satıcı yanıtladı; bu konuda kesin bilgi çıkmadı', 'at': ask['at'], 'url': ask['url']}
            else:
                out = {'state': 'asked', 'label': 'Soru satıcıya gönderildi; yanıt bekleniyor', 'at': ask['at'], 'url': ask['url']}
        elif field in ASKABLE and any(field in o['asked'] for o in undelivered):
            o = next(o for o in undelivered if field in o['asked'])
            out = {'state': 'unknown_delivery', 'label': 'Mesajın gittiği doğrulanamadı; senin kararını bekliyor (tekrar gönderilmez)', 'at': o['sent_at'] or o.get('updated_at'), 'url': o['url'] or link}
        elif field in ASKABLE and any(field in o['asked'] for o in going):
            out = {'state': 'sending', 'label': 'Mesaj şu an gönderiliyor', 'at': next(o for o in going if field in o['asked']).get('updated_at'), 'url': None}
        elif field in ASKABLE and any(field in o['asked'] for o in dry):
            o = next(o for o in dry if field in o['asked'])
            out = {'state': 'dry_run', 'label': 'Deneme yapıldı: mesaj yazıldı ve doğrulandı, GÖNDERİLMEDİ (Ayarlar > Mesaj gönderme)', 'at': o.get('updated_at'), 'url': None}
        elif field in ASKABLE and any(field in o['asked'] for o in drafts):
            out = {'state': 'draft', 'label': 'Eski sistemde mesaj taslağı hazırlanmıştı ama gönderilmemişti. ' + SENDING[mode], 'at': max((o['prepared_at'] or 0 for o in drafts), default=None) or None, 'url': None}
        elif field in about and plan.startswith('ask:'):
            out = {'state': 'to_ask', 'label': 'Satıcıya sorulacak. ' + SENDING[mode], 'at': None, 'url': None}
        elif field in ASKABLE and 'tkgm' in halted and field == 'parcel_key':
            out = {'state': 'halted', 'label': f"{RESOURCE_LABEL['tkgm']} durdu ({halted['tkgm']['reason']}); resmî parsel sorgusu bekliyor", 'at': halted['tkgm']['created_at'], 'url': None}
            blocking.add('tkgm')
        elif field == 'shared_title':
            out = {'state': 'scan', 'label': 'İlan metninde hisse ibaresi aranıyor; ayrıca soru gerekmiyor', 'at': data['checks'].get('sahibinden.read_listing'), 'url': None}
        elif field == 'route_minutes' and 'geography' in halted:
            out = {'state': 'halted', 'label': f"{RESOURCE_LABEL['geography']} durdu ({halted['geography']['reason']})", 'at': halted['geography']['created_at'], 'url': None}
            blocking.add('geography')
        elif field == 'route_minutes' and not context.get('parcel_known', True):
            out = {'state': 'waiting', 'label': 'Önce ada/parsel netleşmeli; rota ondan sonra hesaplanır', 'at': None, 'url': None}
        elif field == 'route_minutes' and not context.get('official_known', True):
            out = {'state': 'waiting', 'label': 'Önce resmî parsel (TKGM) sorgusu gerekiyor; rota ondan sonra hesaplanır', 'at': None, 'url': None}
        elif field in about and plan in PLAN_LABEL:
            out = {'state': 'planned', 'label': PLAN_LABEL[plan], 'at': None, 'url': None}
        fields[field] = out

    events = []
    for tool, label in CHECKS.items():
        if data['checks'].get(tool):
            events.append({'kind': 'check', 'label': label, 'at': data['checks'][tool], 'url': link if tool == 'sahibinden.read_thread' else None})
    for o in dry:
        events.append({'kind': 'draft', 'label': 'Deneme: mesaj yazıldı ve doğrulandı, GÖNDERİLMEDİ · ' + ', '.join(FIELD_TR.get(f, f) for f in o['asked']), 'at': o.get('updated_at'), 'url': None, 'text': o['text']})
    for o in undelivered:
        events.append({'kind': 'unknown', 'label': 'Mesajın gittiği doğrulanamadı · ' + ', '.join(FIELD_TR.get(f, f) for f in o['asked']), 'at': o['sent_at'] or o.get('updated_at'), 'url': o['url'] or link, 'text': o['text']})
    for o in sent:
        events.append({'kind': 'sent', 'label': 'Satıcıya mesaj gönderildi' + (' · ' + ', '.join(FIELD_TR.get(f, f) for f in o['asked']) if o['asked'] else ''),
                       'at': o['sent_at'], 'url': o['url'] or link, 'text': o['text']})
    if convo and convo['found']:
        seller = [m for m in convo['messages'] if m['who'] == 'satıcı']
        if seller:
            events.append({'kind': 'reply', 'label': f"Satıcı {len(seller)} mesaj yazdı", 'at': seller[-1]['at'], 'url': link, 'text': seller[-1]['text']})
    for t in data['tasks']:
        events.append({'kind': 'task', 'label': f"Görev {TASK_STATE.get(t['state'], t['state'])}" + (f" · {t['reason']}" if t['reason'] else ''), 'at': t['created_at'], 'url': None, 'text': (t['objective'] or '')[:160]})
    for resource, incident in halted.items():
        if resource not in blocking:
            continue
        events.append({'kind': 'halt', 'label': f"{RESOURCE_LABEL.get(resource, resource)} durdu · {incident['reason']}", 'at': incident['created_at'], 'url': None})
    events.sort(key=lambda e: e['at'] or 0, reverse=True)
    return {'conversation': None if not convo else {k: convo[k] for k in ('found', 'url', 'checked_at', 'older_omitted')} | {'sent': sum(m['who'] == 'sen' for m in convo['messages']),
                                                                                                                     'received': sum(m['who'] == 'satıcı' for m in convo['messages'])},
            'fields': fields, 'events': events[:12]}


def unknown_fields(status):
    """The criteria a listing still has no value for, from its fact status."""
    known = status.get('facts') or {}
    scope = ['sale_scope'] if 'sale_scope' in (status.get('conflicts') or []) else []     # the official parcel is not (all of) what is for sale
    return [f for f in CRITERIA if f not in known] + ([] if known.get('route_minutes') else ['route_minutes']) + scope


def context_of(status):
    known = status.get('facts') or {}
    area = known.get('sale_area_m2')
    area = [area] if isinstance(area, dict) else area or []
    return {'parcel_known': 'parcel_key' in known, 'official_known': any(isinstance(c, dict) and c.get('grade') == 'resmi' for c in area)}


# the most telling state first; the short sentence is what a list row shows
SUMMARY_ORDER = ('unknown_delivery', 'unclear', 'replied', 'asked', 'sending', 'dry_run', 'draft', 'to_ask', 'halted', 'planned', 'waiting')
SUMMARY_LABEL = {'unknown_delivery': 'Mesajın gittiği doğrulanamadı, senin kararını bekliyor', 'sending': 'Mesaj gönderiliyor', 'dry_run': 'Deneme yapıldı: yazıldı, doğrulandı, gönderilmedi', 'unclear': 'Satıcı cevabı net değil, senin netleştirmeni bekliyor', 'replied': 'Satıcı yanıtladı, kesin bilgi çıkmadı', 'asked': 'Soru gönderildi, yanıt bekleniyor',
                 'draft': 'Eski taslak hazır, gönderilmemişti', 'to_ask': 'Satıcıya sorulacak', 'halted': 'Kaynak durdu',
                 'planned': 'Sırada', 'waiting': 'Önceki adım bekleniyor'}


def summary(built):
    """One line for a list: the most telling action among the unknown fields, or None when nothing is unknown or nothing was done."""
    fields = built['fields']
    for state in SUMMARY_ORDER:
        hit = [(f, v) for f, v in fields.items() if v['state'] == state]
        if hit:
            field, v = hit[0]
            return {'state': state, 'label': SUMMARY_LABEL[state] if state not in ('halted', 'planned', 'waiting') else v['label'], 'at': v['at'], 'url': v['url'], 'count': len(hit)}
    return {'state': 'none', 'label': 'Henüz bir aksiyon alınmadı', 'at': None, 'url': None, 'count': len(fields)} if fields else None


def learning(store, listing_id, unknown, next_action=None, context=None, *, mode='local'):
    return build(gather(store, listing_id, mode), unknown, next_action, context)
