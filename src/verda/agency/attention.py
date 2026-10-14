"""The things that wait for the user: every stop, stuck step, unread problem and open question in one list.

Nothing here is pushed. The list is computed from the records, so it cannot drift from what the system really holds,
and a topic disappears when its cause does. The user can mark a topic as seen; it returns only if its cause changes.
"""
import hashlib
import json
import time

from verda.agency.queue import DAY, stuck_after, work_queue

ACTION, WAITING, INFO = 'action', 'waiting', 'info'
STATES = {'blocked': 'durdu', 'uncertain': 'belirsiz kaldı', 'waiting_user': 'senden bir şey bekliyor'}
# 403/429 from a public service means "slow down" or "you are blocked": never worked around, only waited out by a person.
RATE_LIMITED = {('tkgm', 'tkgm_http_403'), ('tkgm', 'tkgm_http_429')}
RATE_LIMIT_NOTE = ('TKGM istek sıklığını sınırlıyor ya da bizi engelliyor olabilir. Sistem engeli aşmaya çalışmaz ve kendiliğinden devam etmez. '
                   "Bir süre bekle, parselsorgu.tkgm.gov.tr'yi tarayıcıda elle açıp erişimi kontrol et; açılıyorsa kaynağı devam ettir. "
                   'Devam edince koridor sorguları daha yavaş ve aralıklı gider. O zamana kadar bu kaynağın tüm işleri bekler.')


def _key(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def _ensure(con):
    con.execute('CREATE TABLE IF NOT EXISTS attention_acks(key TEXT PRIMARY KEY, acked_at REAL NOT NULL)')


def acknowledge(store, key, now=None):
    if not isinstance(key, str) or not 4 <= len(key) <= 40:
        raise ValueError('Invalid key')
    with store.transaction() as con:
        _ensure(con)
        con.execute('INSERT OR REPLACE INTO attention_acks VALUES(?,?)', (key, time.time() if now is None else now))


def _failed_steps(con, now, limit):
    """Steps that failed the configured number of times or more today and have not succeeded since: reported, never dropped quietly."""
    out = {}
    for row in con.execute("SELECT tool,arguments,state,result,finished_at FROM tool_calls WHERE finished_at>? ORDER BY finished_at", (now - DAY,)):
        args = json.loads(row['arguments'] or '{}')
        subject = args.get('listing_id') or args.get('parcel_key') or '-'
        slot = out.setdefault((row['tool'], subject), {'fails': 0, 'last_error': None, 'last_at': 0})
        if row['state'] in ('failed', 'uncertain'):
            slot['fails'] += 1
            slot['last_error'] = (json.loads(row['result'] or '{}') or {}).get('error')
            slot['last_at'] = row['finished_at']
        elif row['state'] == 'complete':
            slot['fails'] = 0                                   # a later success closes it
    return {k: v for k, v in out.items() if v['fails'] >= limit}


def attention(store, *, mode='local', now=None, queue=None):
    now = time.time() if now is None else now
    items = []
    queue = queue if queue is not None else work_queue(store, {}, mode=mode, now=now)
    limit = stuck_after(store)
    from verda.agency import login
    session = login.status(store, now)
    with store.transaction() as con:
        _ensure(con)
        acked = {r['key'] for r in con.execute('SELECT key FROM attention_acks')}
        for i in con.execute("SELECT * FROM source_incidents WHERE state='open' AND mode=? ORDER BY created_at", (mode,)):
            if i['resource'] == 'sahibinden' and i['reason'] == 'session_lost':
                if session['phase'] == 'recovering':
                    # The system is still signing in again by itself (script first, then its own operator agent): not yet the user's concern.
                    items.append({'key': 'session:' + i['id'], 'kind': 'session_recovering', 'severity': INFO, 'since': i['created_at'], 'ackable': False,
                                  'title': 'Sahibinden oturumu yenileniyor', 'detail': 'Oturum düşmüştü; sistem Google ile yeniden girmeyi kendisi deniyor. Olmazsa burada ilgi bekleyen konu olarak görünür.', 'listings': []})
                else:
                    last = session.get('last') or {}
                    items.append({'key': 'incident:' + i['id'], 'kind': 'source_blocked', 'severity': ACTION, 'since': i['created_at'], 'ackable': False,
                                  'title': 'Sahibinden oturumu otomatik açılamadı', 'detail': f"Denemeler tükendi ({session.get('attempts', 0)} deneme, son sebep: {last.get('reason', 'bilinmiyor')}"
                                  f"{' — ' + last['detail'] if last.get('detail') else ''}). Chrome'da elle giriş yapıp kaynağı devam ettirmen gerekir.", 'listings': []})
                continue
            advice = RATE_LIMIT_NOTE if (i['resource'], i['reason']) in RATE_LIMITED else 'Sorunu giderince kaynağı devam ettirmen gerekir; o zamana kadar bu kaynağın tüm işleri bekler.'
            items.append({'key': 'incident:' + i['id'], 'kind': 'source_blocked', 'severity': ACTION, 'since': i['created_at'], 'ackable': False,
                          'title': f"{i['resource']} erişimi durdu", 'detail': f"Sebep: {i['reason']}. {advice}", 'listings': []})
        for t in con.execute("SELECT id,agent,objective,state,reason,listing_ref,created_at FROM inbox WHERE mode=? AND state IN ('blocked','uncertain','waiting_user') ORDER BY created_at", (mode,)):
            items.append({'key': 'task:' + t['id'], 'kind': 'task_stopped', 'severity': ACTION, 'since': t['created_at'], 'ackable': True,
                          'title': f"{t['agent']} görevi {STATES.get(t['state'], t['state'])}", 'detail': f"{t['objective'][:140]} — {t['reason'] or 'sebep kaydı yok'}",
                          'listings': [t['listing_ref']] if t['listing_ref'] else []})
        for (tool, subject), v in sorted(_failed_steps(con, now, limit).items()):
            items.append({'key': 'stuck:' + _key(tool, subject, int(v['last_at'] // DAY)), 'kind': 'step_stuck', 'severity': ACTION, 'since': v['last_at'], 'ackable': True,
                          'title': f"{tool} {v['fails']} kez başarısız oldu", 'detail': f"{subject} için son hata: {v['last_error'] or 'bilinmiyor'}. Bu adım artık otomatik denenmiyor; incelemen gerekir.",
                          'listings': [subject] if str(subject).isdigit() else []})
        removed = [r[0] for r in con.execute("SELECT DISTINCT listing_ref FROM observations WHERE mode=? AND tool='sahibinden.read_listing' AND payload LIKE '%\"unavailable\":true%' AND listing_ref IS NOT NULL", (mode,))]
    with store.transaction() as con:
        from verda.facts.flags import open_flags
        flags = open_flags(con, None, mode)
    if flags:
        listings = sorted({f['listing_ref'] for f in flags})
        items.append({'key': 'clarify:' + _key(*sorted(f['id'] for f in flags)), 'kind': 'needs_clarification', 'severity': ACTION, 'since': min(f['created_at'] for f in flags), 'ackable': False,
                      'title': f"{len(listings)} ilanda satıcı cevabı netleştirme bekliyor",
                      'detail': "Satıcının cevabı bir alanı netleştirmiyor. İlan sayfasında kanıtı görüp değeri sen gir ya da 'netleştirdim' de; işaret o zaman kalkar.", 'listings': listings})
    ids = queue.get('skipped_ids', {})
    if ids.get('needs_neighborhood'):
        items.append({'key': 'place:' + _key(*sorted(ids['needs_neighborhood'])), 'kind': 'place_missing', 'severity': ACTION, 'since': now, 'ackable': True,
                      'title': f"{len(ids['needs_neighborhood'])} ilanın mahallesi sayfadan alınamadı",
                      'detail': 'Resmî parsel sorgusu için mahalle gerekir; ilan sayfası yer bilgisini vermiyor ya da okunamıyor.', 'listings': ids['needs_neighborhood']})
    if ids.get('no_listing_url'):
        items.append({'key': 'nourl:' + _key(*sorted(ids['no_listing_url'])), 'kind': 'listing_url_unknown', 'severity': WAITING, 'since': now, 'ackable': True,
                      'title': f"{len(ids['no_listing_url'])} ilanın sayfa adresi bilinmiyor",
                      'detail': 'Bu ilanlar yalnız bir konuşmadan biliniyor, hiçbir aramada görülmedi; adres tahmin edilmez. Yayından kalkmış olabilirler; '
                                'aramada yeniden görünürlerse kendiliğinden okunurlar.', 'listings': ids['no_listing_url']})
    if ids.get('human'):
        items.append({'key': 'human:' + _key(*sorted(ids['human'])), 'kind': 'human_review', 'severity': ACTION, 'since': now, 'ackable': True,
                      'title': f"{len(ids['human'])} ilan senin incelemeni bekliyor", 'detail': 'Kaynaklar çelişiyor ya da bir iddia doğrulanamadı.', 'listings': ids['human']})
    if ids.get('waits_for_sender'):
        items.append({'key': 'ask:' + _key(*sorted(ids['waits_for_sender'])), 'kind': 'questions_waiting', 'severity': WAITING, 'since': now, 'ackable': True,
                      'title': f"{len(ids['waits_for_sender'])} ilan satıcıya soru bekliyor", 'detail': "Mesaj gönderme kapalı (Ayarlar > Mesaj gönderme). Önce 'Deneme'yi açıp taslakları incele; bu ilanlar soru gönderilene kadar ilerlemez.", 'listings': ids['waits_for_sender']})
    with store.transaction() as con:
        from verda.agency import message
        message.ensure(con)
        dry = [r['listing_ref'] for r in con.execute("SELECT listing_ref FROM outbox WHERE mode=? AND state='dry_run' ORDER BY updated_at", (mode,))]
        unclear = con.execute("SELECT listing_ref,min(updated_at) AS at FROM outbox WHERE mode=? AND (state='delivery_unknown' OR (state='sending' AND updated_at<?)) GROUP BY listing_ref", (mode, now - 900)).fetchall()
    if dry and message.mode_of(store) == 'dry_run':
        items.append({'key': 'dryrun:' + _key(*sorted(dry)), 'kind': 'dry_runs_ready', 'severity': ACTION, 'since': now, 'ackable': True,
                      'title': f"{len(dry)} mesaj taslağı hazır (deneme)",
                      'detail': "Form dolduruldu ve metin birebir doğrulandı, mesaj GÖNDERİLMEDİ. İlan sayfasında metni ve yapılanları incele; uygunsa Ayarlar > Mesaj gönderme'yi 'Canlı' yap.", 'listings': sorted(set(dry))})
    if unclear:
        items.append({'key': 'unknown:' + _key(*sorted(r['listing_ref'] for r in unclear)), 'kind': 'delivery_unknown', 'severity': ACTION, 'since': min(r['at'] for r in unclear), 'ackable': False,
                      'title': f"{len(unclear)} mesajın gittiği doğrulanamadı",
                      'detail': "Gönder'e basıldı ama konuşmada metin aynen görülemedi. Tekrar gönderilmez. İlan sayfasında konuşmayı açıp 'gitti' ya da 'gitmedi' de; konuşma sonradan okunursa metin görülünce kendiliğinden kapanır.",
                      'listings': sorted(r['listing_ref'] for r in unclear)})
    if removed:
        items.append({'key': 'removed:' + _key(*sorted(removed)), 'kind': 'listings_removed', 'severity': INFO, 'since': now, 'ackable': True,
                      'title': f"{len(removed)} ilan yayından kalkmış", 'detail': 'Sayfa "ilan yayında değil" diyor; bu ilanlar kuyruktan ve sıralamadan çıkarıldı.', 'listings': sorted(removed)})
    items += watchdog_items(store, mode, now)
    shown = [i for i in items if i['key'] not in acked]
    return {'items': shown, 'count': sum(i['severity'] == ACTION for i in shown),
            'waiting': sum(i['severity'] == WAITING for i in shown), 'info': sum(i['severity'] == INFO for i in shown), 'computed_at': now}


def watchdog_items(store, mode, now):
    """The watchdog's broken rules as topics. A finding about listings is seen once per set of listings; one without listings once a day."""
    from verda.agency import watchdog
    try:
        report = watchdog.check(store, mode=mode, now=now)
    except Exception as error:
        return [{'key': 'watch:failed:' + str(int(now // DAY)), 'kind': 'watchdog_failed', 'severity': ACTION, 'since': now, 'ackable': True,
                 'title': 'Nöbetçi çalışmadı', 'detail': f'Kurallar kontrol edilemedi ({type(error).__name__}); bozulan bir kural görünmüyor olabilir.', 'listings': []}]
    out = []
    for f in report['findings']:
        scope = f['listings'] or [str(int(now // DAY))]
        out.append({'key': 'watch:' + _key(f['rule'], f['severity'], *scope), 'kind': 'watchdog:' + f['rule'],
                    'severity': ACTION if f['severity'] in (watchdog.CRITICAL, watchdog.ACTION) else INFO, 'since': now, 'ackable': True,
                    'title': ('KRİTİK · ' if f['severity'] == watchdog.CRITICAL else 'Nöbetçi · ') + f['title'], 'detail': f['detail'], 'listings': f['listings']})
    return out
