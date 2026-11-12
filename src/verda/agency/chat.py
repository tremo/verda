"""Verda as someone to talk to: the tools the chat agent answers from, and the hand-over of a question to it.

The chat agent (`verda` in default.json) has no write tool. It reads what every screen reads: the one ranking, a listing's facts, the day's
decisions and the system's own state. A question from the owner becomes a task for that agent; the `answer` it completes with is the reply.
"""
import json
import time

AGENT = 'verda'
QUESTION_MAX = 1500             # characters of one question
HISTORY_TURNS = 4               # earlier exchanges handed to the agent with a new question
HISTORY_ANSWER = 1200           # characters of an earlier answer kept as context
ANSWER_MAX = 3500               # characters of one answer (the agent is told 3000)


def _row(r):
    """One ranked listing in a few hundred bytes: what a person asks about, with the links to check it."""
    new, land, moved = r.get('new') or {}, r.get('land') or {}, r.get('price_trend') or {}
    out = {'listing_id': r['listing_id'], 'parcel': r.get('parcel'), 'neighborhood': r.get('neighborhood'), 'type': r.get('type'),
           'price_tl': r.get('price_tl'), 'area_m2': round(r['area_m2']) if r.get('area_m2') else None,
           'unit_price': round(r['price_tl'] / r['area_m2']) if r.get('price_tl') and r.get('area_m2') else None,
           'score': new.get('score'), 'label': new.get('label'), 'expected_unit_price': new.get('fair_unit_price'), 'gap_pct': new.get('gap_pct'),
           'quality': (new.get('parts') or {}).get('quality'), 'road': new.get('road'), 'state': r.get('state'), 'missing': r.get('missing') or [],
           'mertur_minutes': r.get('route_minutes_max'), 'owner_mark': r.get('mark'), 'caps': new.get('caps') or [],
           'tkgm': (r.get('links') or {}).get('tkgm'), 'sahibinden': (r.get('links') or {}).get('sahibinden') or f"https://www.sahibinden.com/{r['listing_id']}"}
    if land.get('slope_pct') is not None:
        out['land'] = {'slope_pct': land['slope_pct'], 'slope_class': land.get('slope_class'), 'faces': land.get('faces'), 'elevation_m': land.get('elevation_m'), 'coast_m': land.get('coast_m')}
    if moved:
        out['price_move'] = {'from': moved['from'], 'to': moved['to'], 'change_pct': moved['change_pct'], 'drops': moved['drops'], 'since_days': moved['since_days']}
    if (r.get('seller') or {}).get('kind'):
        out['seller'] = r['seller']['kind']
    return out


def _parcel(text):
    import re
    numbers = re.findall(r'\d+', str(text or ''))
    return tuple(int(n) for n in numbers[:2]) if len(numbers) >= 2 else None


SORTS = {'score': lambda r: -(r['score'] or 0), 'price': lambda r: r['price_tl'] or 1e18, 'area': lambda r: -(r['area_m2'] or 0),
         'unit_price': lambda r: r['unit_price'] or 1e18, 'quality': lambda r: -(r['quality'] or 0), 'mertur': lambda r: r['mertur_minutes'] if r['mertur_minutes'] is not None else 1e9}


def listings(store, args=None, *, mode='local'):
    """Agent tool `chat.listings`: the candidates, filtered and sorted as asked, compact. Without filters: the best by score."""
    from verda.agency import opportunity
    from verda.facts.text import fold
    args = args or {}
    rows = [_row(r) for r in opportunity.rank_all(store, mode=mode)['ranked']]
    total = len(rows)
    wanted = {str(i) for i in args.get('ids') or []}
    if wanted:
        rows = [r for r in rows if r['listing_id'] in wanted]
    if args.get('parcel'):                             # "149/2", also written "149-2" or "ada 149 parsel 2"
        want = _parcel(args['parcel'])
        rows = [r for r in rows if want and _parcel(r['parcel']) == want]
    if args.get('neighborhood'):
        rows = [r for r in rows if fold(args['neighborhood']) in fold(r['neighborhood'] or '')]
    if args.get('type'):
        rows = [r for r in rows if fold(args['type']) in fold(r['type'] or '')]
    if args.get('min_area_m2') is not None:
        rows = [r for r in rows if (r['area_m2'] or 0) >= args['min_area_m2']]
    if args.get('max_price_tl') is not None:
        rows = [r for r in rows if r['price_tl'] is not None and r['price_tl'] <= args['max_price_tl']]
    if args.get('max_slope_pct') is not None:
        rows = [r for r in rows if (r.get('land') or {}).get('slope_pct') is not None and r['land']['slope_pct'] <= args['max_slope_pct']]
    if args.get('owner_mark'):
        rows = [r for r in rows if r['owner_mark'] == args['owner_mark']]
    if args.get('only_complete'):
        rows = [r for r in rows if r['state'] in ('eligible', 'conditional')]
    rows.sort(key=SORTS.get(args.get('sort') or 'score', SORTS['score']))
    limit = int(args.get('limit') or 10)
    return {'total_candidates': total, 'matching': len(rows), 'listings': rows[:limit],
            'note': 'score: fırsat puanı. expected_unit_price: büyüklük, mahalle, deniz ve rakıma göre beklenen ilan fiyatı (₺/m²); gap_pct: ilan bunun yüzde kaç altında (eksi: üstünde). '
                    'quality: arazi kalitesi (eğim, bakı, deniz, su, yol, büyüklük). state: eligible/conditional taraması bitmiş, research_required eksik var.'}


def today(store, args=None, *, mode='local'):
    """Agent tool `chat.today`: the few decisions put forward for today (the same list the dashboard shows)."""
    from verda.agency import decisions
    picked = decisions.today(store, mode=mode)
    return {'items': picked['items'], 'counts': picked['counts'], 'text': decisions.as_text(picked)}


def system(store, args=None, *, mode='local'):
    """Agent tool `chat.system`: how the system itself is doing: services, stopped sources, what waits and why."""
    from verda.agency import message
    from verda.publish import status
    page = status.build(store, mode=mode)
    return {'health': [{'level': h['level'], 'text': h['text']} for h in page['health']], 'listings_by_state': {x['label']: x['count'] for x in page['listings']},
            'waiting_work': page['waiting'], 'waiting_total': page['waitingTotal'], 'skipped': page['skipped'], 'stuck_last_6h': page['stuck'],
            'done_today': page['done'], 'sender_mode': message.mode_of(store), 'generated_at': page['generatedAt']}


def ask(store, text, history=(), *, key, now=None, mode='local'):
    """Hands one question of the owner to the chat agent; -> task id. `history`: earlier (question, answer) pairs, oldest first."""
    now = time.time() if now is None else now
    with store.transaction() as con:                   # the same message delivered twice is one question: the first hand-over stands
        seen = con.execute('SELECT id FROM inbox WHERE request_key=?', (key,)).fetchone()
    if seen:
        return seen['id']
    turns = [{'question': q[:QUESTION_MAX], 'answer': a[:HISTORY_ANSWER]} for q, a in list(history)[-HISTORY_TURNS:]]
    return store.submit(agent=AGENT, objective='Sohbet: sahibin sorusunu kayıtlardan cevapla', inputs={'routine': 'chat', 'message': text[:QUESTION_MAX], 'history': turns},
                        request_key=key, source='chat', mode=mode, priority=90, now=now)


WHY = {'turn_budget_reached': 'soru için ayrılan adım sayısı yetmedi; daha dar sor', 'configuration_changed': 'sistem güncellendi; soruyu yeniden yaz',
       'worker_lost': 'ajan işçisi cevap verirken durdu', 'chat_timed_out': 'ajan işçisi meşgul ya da kapalı'}


def _why(reason):
    reason = str(reason or '')
    if reason.startswith('provider_unavailable'):
        return 'modele ulaşılamadı'
    if reason.startswith('turn_error'):
        return 'cevap hazırlanırken hata oldu'
    return WHY.get(reason, reason or 'sebep kaydı yok')


def answer(store, task_id):
    """-> (state, text): 'pending' while the agent works; 'done' with its answer; 'stopped' with the reason when it could not answer."""
    with store.transaction() as con:
        row = con.execute('SELECT state, reason, result FROM inbox WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return 'stopped', 'görev bulunamadı'
    if row['state'] == 'complete':
        try:
            result = json.loads(row['result'] or '{}') or {}
        except ValueError:
            return 'stopped', 'cevap okunamadı'
        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        text = data.get('answer') if isinstance(data.get('answer'), str) and data['answer'].strip() else result.get('summary')
        return 'done', str(text or '').strip()[:ANSWER_MAX] or 'Cevap boş döndü.'
    if row['state'] == 'waiting_user' and row['reason'] and row['reason'] != 'turn_budget_reached':
        return 'done', str(row['reason'])[:ANSWER_MAX]              # the agent asked something back instead of answering
    if row['state'] in ('blocked', 'uncertain', 'waiting_user', 'failed', 'cancelled'):
        return 'stopped', _why(row['reason'])
    return 'pending', ''


def give_up(store, task_id, now=None):
    """A question nobody picked up in time is withdrawn, so it is not answered hours later into the void. -> True when it was withdrawn;
    False when the agent is already on it (its answer will still come)."""
    now = time.time() if now is None else now
    with store.transaction() as con:
        task = con.execute("SELECT * FROM inbox WHERE id=? AND state='queued' AND source='chat'", (task_id,)).fetchone()
        if task is None:
            return False
        con.execute("UPDATE inbox SET state='cancelled',reason='chat_timed_out',pending=NULL,token=NULL,lease_until=NULL WHERE id=?", (task_id,))
        store._event(con, task, 'task_cancelled', {'reason': 'chat_timed_out'}, now)
        return True
