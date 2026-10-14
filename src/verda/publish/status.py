"""One status document for the dashboard's "Durum" page: how the system is doing, what is waiting, what was done today.

Everything is counted from v2's own records; nothing here reaches a site. Wording is Turkish because the page shows it as written."""
import datetime
import subprocess
import time
from zoneinfo import ZoneInfo

SERVICES = {'com.verda.agency-worker': 'Ajan işçisi', 'com.verda.jev-worker': 'Tarayıcı işçisi', 'com.verda.panel': 'Panel', 'com.verda.publisher': 'Pano yayıncısı'}
TOOL_LABEL = {'sahibinden.read_listing': 'İlan okuma', 'sahibinden.read_thread': 'Konuşma okuma', 'sahibinden.send_message': 'Mesaj gönderimi', 'sahibinden.list_inbox': 'Gelen kutusu',
              'sahibinden.search': 'Arama', 'tkgm.lookup': 'TKGM parsel sorgusu', 'tkgm.corridor': 'TKGM yol koridoru', 'geo.route': 'Rota', 'facts.propose': 'Olgu önerisi',
              'listing.photos': 'Fotoğraf kaydı', 'network.reset_modem': 'Modem sıfırlama', 'replies.pending': 'Cevap yorumlama', 'scan.delta': 'Tarama farkı', 'inbox.plan': 'Gelen kutusu planı'}
ACTION_LABEL = {'tkgm.lookup': 'TKGM parsel sorgusu', 'tkgm.corridor': 'TKGM yol koridoru', 'geo.route': 'Rota hesabı', 'sahibinden.read_thread': 'Konuşma okuma', 'sahibinden.read_listing': 'İlan okuma',
                'sahibinden.send_message': 'Satıcıya mesaj', 'listing.photos': 'Fotoğraf kaydı'}
STATE_LABEL = {'eligible': 'Uygun', 'conditional': 'Koşullu', 'research_required': 'Araştırılıyor', 'excluded': 'Elendi', 'outside_search': 'Arama dışı'}
SENDER_REFUSAL = {'outside_window': 'Mesaj saati dışında', 'too_soon': 'Önceki mesajdan sonra bekleniyor', 'waiting_for_reply': 'Satıcı yanıtı bekleniyor', 'seller_closed': 'Satıcı mesaj almıyor',
                  'delivery_unresolved': 'Önceki teslim doğrulanmadı', 'one_message_per_run': 'Bu turda mesaj gitti', 'sender_off': 'Gönderim kapalı', 'nothing_to_ask': 'Sorulacak kalmadı'}


def iso(epoch):
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat() if epoch else None


def midnight(now):
    local = datetime.datetime.fromtimestamp(now, ZoneInfo('Europe/Istanbul'))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def services():
    try:
        out = subprocess.run(['launchctl', 'list'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] in SERVICES:
            found[parts[2]] = parts[0] != '-'
    return found


def age_text(seconds):
    seconds = int(seconds)
    return f'{seconds} sn' if seconds < 90 else f'{seconds // 60} dk' if seconds < 5400 else f'{seconds // 3600} sa'


def build(store, *, mode='local', now=None, running=None, problems=()):
    """`problems`: what the caller could not compute this time (shown first, never dropped)."""
    from verda.agency import message
    from verda.agency.queue import work_queue
    now = time.time() if now is None else now
    since = midnight(now)
    running = services() if running is None else running
    with store.transaction() as con:
        message.ensure(con)
        states = dict(con.execute("SELECT a.state, count(*) FROM assessments a JOIN (SELECT listing_ref, max(created_at) m FROM assessments WHERE mode=? GROUP BY 1) x "
                                  "ON a.listing_ref=x.listing_ref AND a.created_at=x.m WHERE a.mode=? GROUP BY 1", (mode, mode)).fetchall())
        today = [dict(r) for r in con.execute("SELECT tool, state, count(*) n FROM tool_calls WHERE started_at>=? GROUP BY 1,2", (since,))]
        sent = [dict(r) for r in con.execute("SELECT listing_ref, sent_at FROM outbox WHERE mode!='dry_run' AND state='confirmed' AND sent_at>=? ORDER BY sent_at DESC", (since,))]
        last_sent = con.execute("SELECT max(sent_at) FROM outbox WHERE mode!='dry_run' AND state='confirmed'").fetchone()[0]
        last_event = con.execute('SELECT max(at) FROM agency_events').fetchone()[0]
        incidents = [dict(r) for r in con.execute("SELECT resource, reason, created_at FROM source_incidents WHERE state='open'")]
        resets = con.execute("SELECT count(*) FROM tool_calls WHERE tool='network.reset_modem' AND started_at>=?", (since,)).fetchone()[0]
        facts_today = con.execute("SELECT count(*) FROM claims WHERE status='accepted' AND source_kind='thread' AND created_at>=?", (since,)).fetchone()[0]
        photos_today = con.execute("SELECT count(*) FROM evidence_assets WHERE created_at>=?", (since,)).fetchone()[0] if con.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_assets'").fetchone() else 0
        stuck = [dict(r) for r in con.execute("SELECT agent, reason, count(*) n FROM inbox WHERE state IN ('blocked','waiting_user') AND created_at>? GROUP BY 1,2 ORDER BY n DESC LIMIT 6", (now - 6 * 3600,))]
        recent = []
        for r in con.execute("SELECT listing_ref, sent_at FROM outbox WHERE mode!='dry_run' AND state='confirmed' ORDER BY sent_at DESC LIMIT 6"):
            recent.append({'at': r['sent_at'], 'kind': 'message', 'text': f"Mesaj gönderildi ve doğrulandı · #{r['listing_ref']}"})
        for r in con.execute("SELECT resource, reason, created_at, resolved_at FROM source_incidents ORDER BY created_at DESC LIMIT 6"):
            recent.append({'at': r['created_at'], 'kind': 'incident', 'text': f"Kaynak durdu: {r['resource']} · {r['reason']}"})
            if r['resolved_at']:
                recent.append({'at': r['resolved_at'], 'kind': 'recovered', 'text': f"Kaynak yeniden açıldı: {r['resource']}"})
        for r in con.execute("SELECT started_at FROM tool_calls WHERE tool='network.reset_modem' ORDER BY started_at DESC LIMIT 3"):
            recent.append({'at': r['started_at'], 'kind': 'modem', 'text': 'Modem yeniden başlatıldı'})
    queue = work_queue(store, {}, mode=mode)
    waiting = [{'action': g['action'], 'label': ACTION_LABEL.get(g['action'], g['action']), 'waiting': g['waiting']} for g in queue['groups']]
    refusals = [{'reason': k, 'label': SENDER_REFUSAL.get(k, k), 'count': v} for k, v in (queue.get('sender', {}).get('refusals') or {}).items()]
    ok = {(t['tool'], t['state']): t['n'] for t in today}
    done = sorted(({'tool': tool, 'label': TOOL_LABEL.get(tool, tool), 'done': ok.get((tool, 'complete'), 0), 'failed': ok.get((tool, 'failed'), 0)}
                   for tool in {t['tool'] for t in today} if tool in TOOL_LABEL), key=lambda x: -x['done'])
    health = [{'level': 'bad', 'text': p} for p in problems]
    # The watchdog, run here too: the dashboard shows a broken rule even when the agent worker (which runs it on a timer) is down.
    from verda.agency import watchdog
    try:
        guard = watchdog.check(store, mode=mode, now=now)
    except Exception as error:
        guard = {'checked_at': now, 'ok': False, 'findings': [], 'rules': [], 'error': type(error).__name__}
        health.append({'level': 'bad', 'text': f'Nöbetçi çalışmadı ({type(error).__name__}); bozulan bir kural görünmüyor olabilir'})
    for f in guard['findings']:
        health.append({'level': 'warn' if f['severity'] == watchdog.WARN else 'bad',
                       'text': ('KRİTİK · ' if f['severity'] == watchdog.CRITICAL else 'Nöbetçi · ') + f['title'] + (' · #' + ', #'.join(f['listings'][:5]) if f['listings'] else '')})
    for label, up in running.items():
        health.append({'level': 'ok' if up else 'bad', 'text': f"{SERVICES[label]}: {'çalışıyor' if up else 'KAPALI'}"})
    for inc in incidents:
        health.append({'level': 'bad', 'text': f"Kaynak engelli: {inc['resource']} · {inc['reason']} (açılalı {age_text(now - inc['created_at'])})"})
    if not incidents:
        health.append({'level': 'ok', 'text': 'Açık kaynak engeli yok'})
    if last_event:
        idle = now - last_event
        health.append({'level': 'ok' if idle < 1200 else 'warn', 'text': f'Son hareket {age_text(idle)} önce'})
    in_window = message.within_window(now)
    sender = message.mode_of(store)
    if sender == 'live':
        if not in_window:
            health.append({'level': 'ok', 'text': 'Mesaj saati dışında (08:30–21:00); gönderim bekliyor'})
        elif last_sent and now - last_sent > 7200 and queue['groups'] and any(g['action'] == 'sahibinden.send_message' for g in queue['groups']):
            health.append({'level': 'warn', 'text': f'Son mesaj {age_text(now - last_sent)} önce, sırada mesaj var'})
        else:
            health.append({'level': 'ok', 'text': f'Mesaj gönderimi canlı · bugün {len(sent)} mesaj'})
    else:
        health.append({'level': 'warn', 'text': f'Mesaj gönderimi: {sender}'})
    if resets:
        health.append({'level': 'warn' if resets > 3 else 'ok', 'text': f'Modem bugün {resets} kez yeniden başlatıldı (TKGM günlük sınırı)'})
    return {
        'generatedAt': iso(now), 'health': health,
        'listings': [{'state': k, 'label': STATE_LABEL.get(k, k), 'count': states.get(k, 0)} for k in STATE_LABEL],
        'waiting': waiting, 'senderRefusals': refusals,
        'waitingTotal': queue['waiting_total'], 'skipped': queue['skipped'],
        'done': done, 'sentToday': [{'listingId': s['listing_ref'], 'at': iso(s['sent_at'])} for s in sent], 'factsToday': facts_today, 'photosToday': photos_today,
        'stuck': [{'agent': s['agent'], 'reason': str(s['reason'] or '')[:80], 'count': s['n']} for s in stuck],
        'recent': [{**e, 'at': iso(e['at'])} for e in sorted(recent, key=lambda e: -(e['at'] or 0))[:12]],
        'watchdog': {'checkedAt': iso(guard['checked_at']), 'ok': guard['ok'], 'findings': guard['findings'],
                     'rules': [{k: r.get(k) for k in ('rule', 'label', 'status', 'count')} for r in guard['rules']]},
    }
