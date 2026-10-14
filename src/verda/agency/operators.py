"""Who is working, who waits, and what stands in each agent's queue. Read-only."""
import time

LIVE = ('queued', 'running', 'waiting_browser', 'waiting_children', 'waiting_user', 'blocked', 'uncertain')
LABEL = {'running': 'çalışıyor', 'queued': 'sırada', 'waiting_browser': 'tarayıcıyı bekliyor', 'waiting_children': 'devrettiği işi bekliyor',
         'waiting_user': 'senden bir şey bekliyor', 'blocked': 'durdu', 'uncertain': 'belirsiz'}


def operator_status(store, mode='local', now=None, limit=40):
    now = time.time() if now is None else now
    with store.transaction() as con:
        tasks = [dict(r) for r in con.execute(
            f"SELECT id,agent,objective,state,reason,source,listing_ref,created_at,lease_until,turns FROM inbox WHERE mode=? AND state IN ({','.join('?' * len(LIVE))}) ORDER BY priority DESC, created_at",
            (mode, *LIVE))]
        workers = [dict(r) for r in con.execute('SELECT mode,state,seen_at,expires_at FROM workers WHERE mode=? AND expires_at>? ORDER BY seen_at DESC', (mode, now))]
        jobs = {r['driver']: r['n'] for r in con.execute("SELECT driver, count(*) n FROM browser_jobs WHERE state IN ('queued','running') GROUP BY driver")}
    out = []
    for agent in store.agents.values():
        mine = [t for t in tasks if t['agent'] == agent.key]
        counts = {}
        for t in mine:
            counts[t['state']] = counts.get(t['state'], 0) + 1
        running = [t for t in mine if t['state'] == 'running' and (t['lease_until'] or 0) > now]
        if running:
            state, note = 'working', running[0]['objective'][:90]
        elif any(t['state'] in ('waiting_browser', 'waiting_children') for t in mine):
            state, note = 'waiting', 'Başka bir işin sonucunu bekliyor'
        elif counts.get('queued'):
            state, note = 'waiting', 'Sırada iş var; işçi alınca başlayacak'
        else:
            state, note = 'idle', 'Boşta'
        out.append({'key': agent.key, 'label': agent.label, 'state': state, 'note': note,
                    'queue': sum(counts.get(k, 0) for k in ('queued', 'running', 'waiting_browser', 'waiting_children')),
                    'needs_you': counts.get('waiting_user', 0) + counts.get('blocked', 0) + counts.get('uncertain', 0), 'counts': counts,
                    'tasks': [{'id': t['id'], 'objective': t['objective'][:160], 'state': t['state'], 'state_label': LABEL.get(t['state'], t['state']),
                               'source': t['source'], 'listing': t['listing_ref'], 'since': t['created_at'], 'turns': t['turns'], 'reason': t['reason']} for t in mine[:limit]]})
    return {'agents': out, 'workers': {'agent_worker_alive': any(w['state'] in ('processing', 'idle') for w in workers), 'seen_at': workers[0]['seen_at'] if workers else None},
            'browser_jobs': jobs, 'computed_at': now}
