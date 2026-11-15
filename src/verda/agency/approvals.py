"""The owner's word on each seller question: ask it, hold it, or drop the listing.

Until 15 Nov 2026 every candidate with a missing road, sit or parcel was queued for a seller message as soon as sending was switched on;
the owner had no say per listing. Now no message goes to a listing the owner has not marked `ask`, in any sender mode (`message.evaluate`).
`hold` keeps the listing and its question on the board without sending; dropping a listing is the owner's `user_excluded` fact, the same
one the dashboard and the chat record. Every choice is kept in a log; the latest one per listing decides.
"""
import time

CHOICES = ('ask', 'hold')
CHOICE_TR = {'ask': 'sor', 'hold': 'beklet', None: 'karar yok'}


def ensure(con):
    con.execute('''CREATE TABLE IF NOT EXISTS ask_choices(id INTEGER PRIMARY KEY AUTOINCREMENT, listing_ref TEXT NOT NULL, mode TEXT NOT NULL,
        choice TEXT, note TEXT, by TEXT NOT NULL, at REAL NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS ask_choices_listing ON ask_choices(listing_ref, mode, id)')


def choices(con, mode='local'):
    """{listing id: {'choice', 'note', 'by', 'at'}}, the latest per listing; a choice taken back (choice NULL) is left out."""
    ensure(con)
    out = {}
    for r in con.execute('SELECT * FROM ask_choices WHERE mode=? ORDER BY id', (mode,)):
        out[r['listing_ref']] = {'choice': r['choice'], 'note': r['note'], 'by': r['by'], 'at': r['at']}
    return {k: v for k, v in out.items() if v['choice']}


def choice_of(con, listing_ref, mode='local'):
    ensure(con)
    row = con.execute('SELECT choice FROM ask_choices WHERE listing_ref=? AND mode=? ORDER BY id DESC LIMIT 1', (listing_ref, mode)).fetchone()
    return row['choice'] if row else None


def approved(con, listing_ref, mode='local'):
    return choice_of(con, listing_ref, mode) == 'ask'


def choose(store, listing_ref, choice, *, mode='local', note='', by='panel', now=None):
    """Records the owner's choice: 'ask', 'hold', 'exclude' (the owner's own elimination, a fact) or 'clear' (back to no decision).
    -> {'choice', 'state'?}; raises ValueError for anything else."""
    now = time.time() if now is None else now
    if not str(listing_ref).isdigit():
        raise ValueError('listing number expected')
    note = (note or '').strip()[:300]
    if choice == 'exclude':
        with store.transaction() as con:
            done = store.facts.decide(con, listing_ref, mode, 'user_excluded', True, note or 'Soru onayı sayfasından elendi', now)
            if done.get('status') != 'accepted':
                raise ValueError('elimination not accepted: ' + str(done.get('reason')))
            ensure(con)
            con.execute('INSERT INTO ask_choices(listing_ref,mode,choice,note,by,at) VALUES(?,?,?,?,?,?)', (listing_ref, mode, None, note or 'elendi', by, now))
        return {'choice': 'exclude', 'state': (done.get('assessment') or {}).get('state')}
    if choice not in CHOICES + ('clear',):
        raise ValueError('unknown choice')
    with store.transaction() as con:
        ensure(con)
        con.execute('INSERT INTO ask_choices(listing_ref,mode,choice,note,by,at) VALUES(?,?,?,?,?,?)',
                    (listing_ref, mode, None if choice == 'clear' else choice, note or None, by, now))
    return {'choice': None if choice == 'clear' else choice}


def board(store, *, mode='local', now=None):
    """The approval list: every candidate the rule engine wants a seller answer for, and every listing the owner already chose for.
    Each row: the listing (parcel, both links), today's score, what would be asked (or why nothing can be asked now), the owner's
    choice, the last message attempt, and what the score becomes once the seller answers."""
    from verda.agency import message, opportunity
    now = time.time() if now is None else now
    drafts = {}

    def asked(listing, current):
        """The question this listing would get, drafted once; the score scenarios follow the fields it asks."""
        if not str((current.get('next_action') or {}).get('action') or '').startswith('ask:'):
            drafts[listing] = {'ok': False, 'reason': 'no_ask_step', 'message': message.REFUSALS['no_ask_step']}
            return None
        drafts[listing] = message.draft(store, listing, mode, now, current=current)
        return drafts[listing].get('fields') if drafts[listing].get('ok') else None      # nothing to send now (a reply is awaited): every open field
    ranked = opportunity.rank_all(store, mode=mode, now=now, scenarios=asked)['ranked']
    with store.transaction() as con:
        chosen = choices(con, mode)
        message.ensure(con)
        last = {r['listing_ref']: dict(r) for r in con.execute(
            'SELECT listing_ref, state, reason, sent_at, updated_at FROM outbox WHERE mode=? ORDER BY created_at, id', (mode,))}
    out = []
    for r in ranked:
        listing = r['listing_id']
        draft = drafts.get(listing) or {}
        if draft.get('reason') == 'no_ask_step' and listing not in chosen:
            continue
        sent = last.get(listing)
        out.append({'listing_id': listing, 'title': r.get('title'), 'neighborhood': r.get('neighborhood'), 'type': r.get('type'), 'area_m2': r.get('area_m2'),
                    'price_tl': r.get('price_tl'), 'parcel': r.get('parcel'), 'links': r.get('links') or {}, 'score': r['new']['score'], 'label': r['new']['label'],
                    'road': r['new']['road'], 'caps': r['new'].get('caps') or [], 'missing': r.get('missing') or [], 'mark': r.get('mark'),
                    'asks': draft.get('fields') or [], 'asks_tr': [message.FIELD_NAME.get(f, f) for f in draft.get('fields') or []],
                    'text': draft.get('text'), 'why_not': None if draft.get('ok') else draft.get('message'), 'why_code': None if draft.get('ok') else draft.get('reason'),
                    'choice': (chosen.get(listing) or {}).get('choice'), 'chosen_at': (chosen.get(listing) or {}).get('at'),
                    'last_message': sent and {k: sent[k] for k in ('state', 'reason', 'sent_at', 'updated_at')},
                    'if_answered': r.get('if_answered')})
    out.sort(key=lambda x: (x['choice'] is not None, not x['text'], -x['score']))       # undecided and askable first
    askable = [x for x in out if x['text']]
    return {'rows': out, 'sender_mode': message.mode_of(store),
            'counts': {'ask': sum(x['choice'] == 'ask' for x in out), 'hold': sum(x['choice'] == 'hold' for x in out),
                       'undecided': sum(x['choice'] is None for x in askable), 'not_askable_now': len(out) - len(askable)}}
