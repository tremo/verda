"""Durable fan-out with a separate delivery receipt for each recipient."""
import json
import re
from uuid import uuid4

from verda.legacy import canonical

EVENT_TYPES = {'observation.recorded', 'task.complete', 'task.blocked', 'task.uncertain',
               'task.failed', 'task.cancelled', 'task.waiting_user', 'task.retry_scheduled',
               'source.blocked', 'source.recovered'}
DELIVERY_DELAYS = (5, 30)
READ_DELAYS = (60, 300)


def migrate_v5(con):
    con.execute('''CREATE TABLE record_events(id TEXT PRIMARY KEY, type TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES inbox(id), trace_id TEXT NOT NULL, listing_ref TEXT,
        mode TEXT NOT NULL, agent TEXT NOT NULL, record_kind TEXT NOT NULL, record_id TEXT NOT NULL,
        data TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(type,record_id))''')
    con.execute('CREATE INDEX record_event_trace ON record_events(trace_id,created_at)')
    con.execute('''CREATE TABLE record_subscriptions(key TEXT PRIMARY KEY, target_agent TEXT NOT NULL,
        event_types TEXT NOT NULL, source_agents TEXT NOT NULL, mode TEXT NOT NULL,
        wake INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)''')
    con.execute('ALTER TABLE record_deliveries RENAME TO legacy_deliveries')
    con.execute('DROP INDEX record_delivery_queue')
    con.execute('''CREATE TABLE record_deliveries(id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES record_events(id), outcome_id TEXT REFERENCES record_outcomes(id),
        task_id TEXT NOT NULL REFERENCES inbox(id), mode TEXT NOT NULL, route TEXT NOT NULL,
        target_agent TEXT NOT NULL, target_task_id TEXT REFERENCES inbox(id), wake INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
        created_at REAL NOT NULL, delivered_at REAL, available_at REAL NOT NULL,
        processing_state TEXT NOT NULL DEFAULT 'waiting', started_at REAL, processed_at REAL,
        UNIQUE(event_id,target_agent))''')
    con.execute('CREATE INDEX record_delivery_queue ON record_deliveries(mode,state,available_at)')
    # Preserve all existing deliveries; do not subscribe new recipients to old records.
    for row in con.execute('SELECT * FROM record_outcomes').fetchall():
        con.execute('INSERT INTO record_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('legacy:'+row['id'], 'task.'+row['state'], row['task_id'], row['trace_id'],
                     row['listing_ref'], row['mode'], row['agent'], 'outcome', row['id'], '{}', row['recorded_at']))
    con.execute('''INSERT INTO record_deliveries(id,event_id,outcome_id,task_id,mode,route,target_agent,
        target_task_id,wake,state,attempts,error,created_at,delivered_at,available_at,processing_state)
        SELECT id,'legacy:'||outcome_id,outcome_id,task_id,mode,route,target_agent,target_task_id,1,
        state,attempts,error,created_at,delivered_at,created_at,
        CASE WHEN state='delivered' THEN 'legacy' ELSE 'waiting' END FROM legacy_deliveries''')
    con.execute('DROP TABLE legacy_deliveries')
    for action in ('UPDATE','DELETE'):
        con.execute(f"CREATE TRIGGER record_events_no_{action.lower()} BEFORE {action} ON record_events "
                    "BEGIN SELECT RAISE(ABORT,'events_are_append_only'); END")
    con.execute('ALTER TABLE inbox ADD COLUMN retry TEXT')
    con.execute('ALTER TABLE tool_calls ADD COLUMN operation_key TEXT')
    con.execute('ALTER TABLE tool_calls ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1')
    con.execute('''CREATE TABLE source_incidents(id TEXT PRIMARY KEY, resource TEXT NOT NULL,
        mode TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
        resolved_at REAL, resolution TEXT)''')
    con.execute("CREATE UNIQUE INDEX one_source_incident ON source_incidents(resource,mode) WHERE state='open'")
    con.execute('''CREATE TABLE incident_tasks(incident_id TEXT NOT NULL REFERENCES source_incidents(id),
        task_id TEXT NOT NULL REFERENCES inbox(id), PRIMARY KEY(incident_id,task_id))''')
    con.execute('UPDATE agency_meta SET version=5')


class EventBus:
    @staticmethod
    def register(con, agents, *, key, target_agent, event_types, source_agents=(), mode='local', wake=False, enabled=True):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',key) or target_agent not in agents:
            raise ValueError('Invalid subscription identity')
        if not event_types or set(event_types)-EVENT_TYPES or set(source_agents)-set(agents):
            raise ValueError('Invalid subscription filter')
        if mode not in {'local','synthetic','both'} or type(wake) is not bool or type(enabled) is not bool:
            raise ValueError('Invalid subscription policy')
        con.execute('''INSERT INTO record_subscriptions VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
            target_agent=excluded.target_agent,event_types=excluded.event_types,source_agents=excluded.source_agents,
            mode=excluded.mode,wake=excluded.wake,enabled=excluded.enabled''',
            (key,target_agent,canonical(sorted(set(event_types))),canonical(sorted(set(source_agents))),mode,int(wake),int(enabled)))

    @staticmethod
    def subscriptions(con, supervisor):
        rows=[{**dict(r),'event_types':json.loads(r['event_types']),'source_agents':json.loads(r['source_agents'])}
              for r in con.execute('SELECT * FROM record_subscriptions ORDER BY key')]
        if supervisor:
            rows.insert(0,dict(key='builtin-supervisor',target_agent=supervisor,event_types=sorted(EVENT_TYPES),
                source_agents=[],mode='both',wake='outcomes_and_incidents',enabled=True,builtin=True))
        return rows

    @staticmethod
    def emit(con, task, event_type, record_kind, record_id, supervisor, now, data=None):
        old=con.execute('SELECT id FROM record_events WHERE type=? AND record_id=?',(event_type,record_id)).fetchone()
        if old:return old['id']
        event_id=uuid4().hex
        payload=data or {}
        con.execute('INSERT INTO record_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (event_id,event_type,task['id'],task['trace_id'],task['listing_ref'],task['mode'],
                     task['agent'],record_kind,record_id,canonical(payload),now))
        recipients={}
        is_outcome=record_kind=='outcome'
        if supervisor and task['agent']!=supervisor:
            wake=(is_outcome and not payload.get('incident_id')) or event_type=='source.blocked'
            recipients[supervisor]=('supervisor',None,wake)
        for row in con.execute('SELECT * FROM record_subscriptions WHERE enabled=1'):
            if row['mode'] not in {'both',task['mode']} or event_type not in json.loads(row['event_types']):continue
            sources=json.loads(row['source_agents'])
            if sources and task['agent'] not in sources:continue
            # Never feed an agent's own output back into its own model, including custom subscriptions.
            if row['target_agent']==task['agent']:continue
            recipients.setdefault(row['target_agent'],('subscriber',None,bool(row['wake'])))
        if task['parent_id']:
            parent=con.execute('SELECT agent FROM inbox WHERE id=?',(task['parent_id'],)).fetchone()
            recipients[parent['agent']]=('parent',task['parent_id'],is_outcome)
        for target,(route,target_task,wake) in recipients.items():
            if is_outcome and task['source'].startswith('record:') and route!='parent':
                wake=False  # Processing an event must not recursively create more event evaluations.
            con.execute('''INSERT INTO record_deliveries(id,event_id,outcome_id,task_id,mode,route,
                target_agent,target_task_id,wake,created_at,available_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (uuid4().hex,event_id,record_id if is_outcome else None,task['id'],task['mode'],route,
                 target,target_task,int(wake),now,now))
        return event_id

    @staticmethod
    def progress(con, task_id, state, now):
        if state=='running':
            con.execute("""UPDATE record_deliveries SET processing_state='processing',started_at=coalesce(started_at,?)
                WHERE target_task_id=? AND state='delivered' AND processing_state!='processed'""",(now,task_id))
        elif state=='complete':
            con.execute("""UPDATE record_deliveries SET processing_state='processed',processed_at=?
                WHERE target_task_id=? AND state='delivered' AND started_at IS NOT NULL""",(now,task_id))
        elif state in {'blocked','failed','cancelled','uncertain','waiting_user'}:
            con.execute("""UPDATE record_deliveries SET processing_state='attention'
                WHERE target_task_id=? AND state='delivered' AND processing_state!='processed'""",(task_id,))

    @staticmethod
    def received(con, task):
        return [{**dict(r),'data':json.loads(r['data'])} for r in con.execute('''SELECT e.* FROM record_events e
            JOIN record_deliveries d ON d.event_id=e.id WHERE d.target_task_id=? AND d.state='delivered'
            AND e.mode=? ORDER BY e.created_at DESC LIMIT 30''',(task['id'],task['mode']))]
