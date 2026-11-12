"""The few things worth the owner's attention today, most valuable first.

The screening ends at the owner: on 9 Nov 2026, 85 listings were fully checked, none had a final review, and the panel counted 488 open topics.
This picks at most LIMIT of them. Each names one listing (or one switch), says why now and what deciding it changes. Nothing here acts or sends;
the dashboard, the panel and the chat all show this same list.
"""
import json
import time

from verda.agency import signals

LIMIT = 5
PER_KIND = 2
READY_FROM = 60                 # a finished listing is put forward from this score
FRESH_DAYS = 14                 # how long a price cut or a removal stays news
DROP_FROM_PCT = 10              # an unmarked listing is put forward for a cut of this size

ORDER = ('marked_price_drop', 'marked_removed', 'verify_cheap', 'ready', 'price_drop', 'clarify')      # what comes first when there is more than fits

MARK_TR = {'shortlist': 'Kısa listendeki', 'maybe': '"Belki" dediğin'}


def money(value):
    if value is None:
        return '—'
    if value >= 1_000_000:
        return f'{value / 1_000_000:.2f}'.rstrip('0').rstrip('.').replace('.', ',') + ' M ₺'
    return f'{value:,.0f}'.replace(',', '.') + ' ₺'


def _ago(seconds):
    days = int(seconds // 86400)
    return 'bugün' if days <= 0 else 'dün' if days == 1 else f'{days} gün önce'


def _item(kind, row, title, why, ask, stamp=''):
    new = row.get('new') or {}
    listing = row['listing_id']
    return {'key': f'{kind}:{listing}:{stamp}', 'kind': kind, 'title': title, 'why': why, 'ask': ask, 'listingId': listing, 'parcel': row.get('parcel'),
            'neighborhood': row.get('neighborhood'), 'type': row.get('type'), 'score': new.get('score'), 'priceTl': row.get('price_tl'), 'areaM2': row.get('area_m2'),
            'links': {'tkgm': (row.get('links') or {}).get('tkgm'), 'listing': (row.get('links') or {}).get('sahibinden') or f'https://www.sahibinden.com/{listing}'}}


def _facts_line(row):
    new = row.get('new') or {}
    parts = [f"puan {new.get('score')}"]
    if new.get('gap_pct') is not None:
        parts.append(f"{new.get('label')} {'+' if new['gap_pct'] >= 0 else '−'}%{abs(new['gap_pct']):g}".replace('.', ','))
    parts.append('yol: ' + str(new.get('road')))
    land = row.get('land') or {}
    if land.get('slope_pct') is not None:
        parts.append(f"eğim %{land['slope_pct']:g}".replace('.', ',') + (f" {land['faces']}" if land.get('faces') else ''))
    if land.get('coast_m') is not None:
        parts.append(f"denize {land['coast_m'] / 1000:.1f} km".replace('.', ','))
    for signal in new.get('signals') or []:
        parts.append(signal['text'])
    return ' · '.join(parts)


def _removed_marks(store, marks, ranked_ids, mode, now):
    """Marked listings that left Sahibinden lately: [(listing id, removed at, title)]."""
    wanted = [l for l, mark in marks.items() if mark in MARK_TR and l not in ranked_ids]
    out = []
    with store.transaction() as con:
        for listing in wanted:
            row = con.execute("SELECT max(captured_at) AS at FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref=? "
                              "AND payload LIKE '%\"unavailable\":true%'", (mode, listing)).fetchone()
            if row and row['at'] and now - row['at'] <= FRESH_DAYS * 86400:
                title = con.execute("SELECT json_extract(payload, '$.title') AS t FROM observations WHERE tool='sahibinden.read_listing' AND mode=? AND listing_ref=? "
                                    "AND json_extract(payload, '$.title') IS NOT NULL ORDER BY captured_at DESC LIMIT 1", (mode, listing)).fetchone()
                out.append((listing, row['at'], title['t'] if title else None))
    return out


def today(store, *, mode='local', now=None, ranked=None, queue=None, limit=LIMIT):
    """-> {'items': [...], 'counts': {...}} from the ranked rows (opportunity.rank_all), the owner's marks and the waiting work."""
    from verda.agency import message, opportunity
    now = time.time() if now is None else now
    if ranked is None:
        ranked = opportunity.rank_all(store, mode=mode, now=now)['ranked']
    marks = signals.user_marks(store)
    candidates = {'marked_price_drop': [], 'marked_removed': [], 'ready': [], 'price_drop': [], 'verify_cheap': [], 'clarify': []}
    for row in ranked:
        new, mark, moved = row.get('new') or {}, row.get('mark'), row.get('price_trend')
        if mark == 'excluded':
            continue
        fell = moved and moved['last_move'] == 'down' and -moved['change_pct'] >= signals.MIN_DROP_PCT and now - moved['last_change_at'] <= FRESH_DAYS * 86400
        path = f"{money(moved['from'])} → {money(moved['to'])} · {signals.describe(moved)} · son değişiklik {_ago(now - moved['last_change_at'])}" if moved else None
        if mark in MARK_TR and fell:
            candidates['marked_price_drop'].append(_item('marked_price_drop', row, f'{MARK_TR[mark]} ilanın fiyatı düştü', path, 'Görmeye ya da teklif vermeye değer mi?', int(moved['last_change_at'])))
        if any('çok ucuz' in cap for cap in new.get('caps') or []):
            fair = new.get('fair_unit_price')
            rate = round(row['price_tl'] / row['area_m2']) if row.get('price_tl') and row.get('area_m2') else None
            candidates['verify_cheap'].append(_item('verify_cheap', row, 'Çok ucuz görünüyor: fiyatı ve alanı doğrula',
                                                    f"{rate} ₺/m², beklenen ≈ {fair} ₺/m²" + (f' · {path}' if path else ''),
                                                    'İlandaki alanı tapu kaydıyla karşılaştır; tapuyu görmeden kapora verme.'))
        elif not mark and not row.get('provisional') and (new.get('score') or 0) >= READY_FROM:
            candidates['ready'].append(_item('ready', row, 'Taraması bitti, puanı yüksek', _facts_line(row), 'Kısa listeye al, "belki" de ya da ele.'))
        elif not mark and fell and -moved['change_pct'] >= DROP_FROM_PCT:
            candidates['price_drop'].append(_item('price_drop', row, 'Fiyatı düşen aday', path, 'Bakmaya değer mi?', int(moved['last_change_at'])))
        if row.get('needs_clarification') and not mark:
            candidates['clarify'].append(_item('clarify', row, 'Satıcının cevabı net değil', _facts_line(row), 'İlan sayfasında cevabı okuyup değeri sen gir.'))
    ranked_ids = {r['listing_id'] for r in ranked}
    for listing, at, title in _removed_marks(store, marks, ranked_ids, mode, now):
        row = {'listing_id': listing, 'new': {}, 'links': {}}
        candidates['marked_removed'].append(_item('marked_removed', row, f'{MARK_TR[marks[listing]]} ilan yayından kalktı',
                                                  (title or 'İlan') + f' · {_ago(now - at)} "yayında değil" göründü', 'Listenden çıkar; benzeri varsa ona bak.', int(at)))
    items, used = [], set()
    for kind in ORDER:
        rows = sorted(candidates[kind], key=lambda i: -(i['score'] or 0))
        taken = 0
        for entry in rows:
            if entry['listingId'] in used or taken >= PER_KIND or len(items) >= limit:
                continue
            items.append(entry)
            used.add(entry['listingId'])
            taken += 1
    waiting = 0
    if message.mode_of(store) == 'off':
        if queue is None:
            from verda.agency.queue import work_queue
            queue = work_queue(store, {}, mode=mode, now=now)
        waiting = (queue.get('skipped') or {}).get('waits_for_sender', 0)
        if waiting:
            asked = set((queue.get('skipped_ids') or {}).get('waits_for_sender') or [])
            top = sum(1 for r in ranked[:30] if r['listing_id'] in asked)
            items.append({'key': f'sender_off:{waiting}', 'kind': 'sender_off', 'title': f'Mesaj gönderimi kapalı: {waiting} ilan satıcıya soru bekliyor',
                          'why': f'İlk 30 puandaki {top} ilan da bunların içinde; cevap gelmeden taramaları bitmez.',
                          'ask': 'Ayarlar > Mesaj gönderme: önce "Deneme"yi aç, taslakları gör.', 'listingId': None, 'links': {}})
    return {'items': items, 'counts': {kind: len(rows) for kind, rows in candidates.items()} | {'waits_for_sender': waiting}, 'generatedAt': now}


def as_text(result, limit=LIMIT + 1):
    """The same list as plain lines for a chat: every listing with its parcel and both links on its own line."""
    lines = []
    for n, entry in enumerate(result['items'][:limit], 1):
        lines.append(f"{n}. {entry['title']}")
        if entry.get('listingId'):
            where = ' · '.join(str(x) for x in (entry.get('neighborhood'), entry.get('parcel'), money(entry.get('priceTl')) if entry.get('priceTl') else None,
                                                 f"{entry['areaM2']:,.0f} m²".replace(',', '.') if entry.get('areaM2') else None) if x)
            links = ' · '.join(f'{label}: {url}' for label, url in (('TKGM', entry['links'].get('tkgm')), ('Sahibinden', entry['links'].get('listing'))) if url)
            lines.append(f"   #{entry['listingId']}" + (f' · {where}' if where else '') + (f' · {links}' if links else ''))
        lines.append(f"   {entry['why']}")
        lines.append(f"   → {entry['ask']}")
    return '\n'.join(lines) if lines else 'Bugün senden bekleyen bir karar yok.'
