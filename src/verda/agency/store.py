"""SQLite inboxes, task lineage, triggers and tool receipts. No model calls in transactions."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from verda.legacy import canonical, digest
from verda.agency.registry import AgencyConfig
from verda.agency.records import RecordService, OUTCOME_STATES
from verda.agency.events import EventBus, migrate_v5, DELIVERY_DELAYS, READ_DELAYS
from verda.agency.settings import migrate_v6, effective_agent

TERMINAL = {'complete', 'cancelled', 'failed'}


class AgencyStore:
    def __init__(self, path: Path, config: AgencyConfig, *, supervisor_agent='manager'):
        self.path = path.resolve()
        self.config = config
        self.agents = {a.key: a for a in config.agents}
        self.supervisor_agent = supervisor_agent
        self.records = RecordService()

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self.transaction() as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if 'agency_meta' not in tables or con.execute('SELECT version FROM agency_meta').fetchone()[0] not in {1, 2, 3, 4, 5, 6}:
                    raise ValueError('Foreign agency database')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 1:
                    con.execute('ALTER TABLE inbox ADD COLUMN executor_tool TEXT')
                    con.execute('UPDATE agency_meta SET version=2')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 2:
                    con.execute('CREATE TABLE workers(id TEXT PRIMARY KEY, mode TEXT NOT NULL, state TEXT NOT NULL, seen_at REAL NOT NULL, expires_at REAL NOT NULL)')
                    con.execute('UPDATE agency_meta SET version=3')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 3:
                    con.execute('ALTER TABLE inbox ADD COLUMN caused_by_task_id TEXT REFERENCES inbox(id)')
                    con.execute('ALTER TABLE inbox ADD COLUMN record_version INTEGER NOT NULL DEFAULT 0')
                    self.records.install(con)
                    con.execute('UPDATE agency_meta SET version=4')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 4:
                    migrate_v5(con)
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 5:
                    migrate_v6(con)
                return
            for sql in [
                'CREATE TABLE agency_meta(version INTEGER PRIMARY KEY)', 'INSERT INTO agency_meta VALUES(4)',
                '''CREATE TABLE inbox(id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                agent TEXT NOT NULL, objective TEXT NOT NULL, inputs TEXT NOT NULL, listing_ref TEXT, trace_id TEXT NOT NULL,
                parent_id TEXT REFERENCES inbox(id), source TEXT NOT NULL, mode TEXT NOT NULL, priority INTEGER NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, available_at REAL NOT NULL, turns INTEGER NOT NULL DEFAULT 0,
                config_revision TEXT NOT NULL, definition TEXT NOT NULL, token TEXT, lease_until REAL, reason TEXT,
                pending TEXT, result TEXT, executor_tool TEXT, caused_by_task_id TEXT REFERENCES inbox(id),
                record_version INTEGER NOT NULL DEFAULT 0)''',
                'CREATE INDEX inbox_queue ON inbox(state,available_at,priority,agent)',
                'CREATE INDEX inbox_trace ON inbox(trace_id,created_at)',
                '''CREATE TABLE agency_events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, trace_id TEXT,
                at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)''',
                'CREATE INDEX agency_timeline ON agency_events(trace_id,id)',
                '''CREATE TABLE tool_calls(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES inbox(id),
                tool TEXT NOT NULL, resource TEXT, effect TEXT NOT NULL, state TEXT NOT NULL,
                arguments TEXT NOT NULL, result TEXT, started_at REAL NOT NULL, finished_at REAL)''',
                '''CREATE TABLE resources(key TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0,
                owner TEXT, halted TEXT)''',
                '''CREATE TABLE triggers(key TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL,
                next_at REAL, enabled INTEGER NOT NULL DEFAULT 1)''',
                'CREATE TABLE incoming_events(key TEXT PRIMARY KEY, body_hash TEXT NOT NULL)',
                'CREATE TABLE workers(id TEXT PRIMARY KEY, mode TEXT NOT NULL, state TEXT NOT NULL, seen_at REAL NOT NULL, expires_at REAL NOT NULL)',
            ]:
                con.execute(sql)
            self.records.install(con)
            migrate_v5(con)
            migrate_v6(con)

    def worker_status(self, worker_id, mode, state, *, now=None):
        if mode not in {'local', 'synthetic'} or state not in {'processing', 'idle', 'stopped'}:
            raise ValueError('Invalid worker status')
        now = time.time() if now is None else now
        ttl = 600 if state == 'processing' else 15 if state == 'idle' else 0
        with self.transaction() as con:
            con.execute("""INSERT INTO workers VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                mode=excluded.mode,state=excluded.state,seen_at=excluded.seen_at,expires_at=excluded.expires_at""",
                        (worker_id, mode, state, now, now + ttl))

    @staticmethod
    def _event(con, task, kind, data, now):
        con.execute('INSERT INTO agency_events(task_id,trace_id,at,kind,data) VALUES(?,?,?,?,?)',
                    (task['id'], task['trace_id'], now, kind, canonical(data)))

    def _submit(self, con, *, agent, objective, inputs, request_key, source, mode, priority,
                listing_ref=None, parent=None, caused_by=None, executor_tool=None, now):
        if agent not in self.agents or not objective.strip() or len(objective) > 4000:
            raise ValueError('Invalid agent or objective')
        if executor_tool and executor_tool not in self.agents[agent].tools:
            raise ValueError('Direct tool not granted to agent')
        if mode not in {'synthetic', 'local'} or not 0 <= priority <= 100:
            raise ValueError('Invalid mode or priority')
        if not request_key or len(request_key) > 256 or len(canonical(inputs).encode()) > 16384:
            raise ValueError('Invalid request key or oversized input')
        identity = dict(agent=agent, objective=objective, inputs=inputs, source=source, mode=mode,
                        priority=priority, listing_ref=listing_ref, parent=parent['id'] if parent else None)
        if executor_tool:
            identity['executor_tool'] = executor_tool
        if caused_by:
            identity['caused_by_task_id'] = caused_by['id']
        old = con.execute('SELECT * FROM inbox WHERE request_key=?', (request_key,)).fetchone()
        if old:
            if old['request_hash'] != digest(identity):
                raise ValueError('Request key conflict')
            return old['id']
        task_id = uuid4().hex
        origin = parent or caused_by
        trace = origin['trace_id'] if origin else uuid4().hex
        if origin and con.execute('SELECT count(*) FROM inbox WHERE trace_id=?', (trace,)).fetchone()[0] >= 100:
            raise ValueError('Trace task budget exceeded')
        if parent:
            root = con.execute('SELECT inputs FROM inbox WHERE trace_id=? AND parent_id IS NULL AND caused_by_task_id IS NULL ORDER BY created_at,id LIMIT 1', (trace,)).fetchone()
            budget = json.loads(root['inputs']).get('max_followups') if root else None
            if type(budget) is int and 0 <= budget <= 100:
                used = con.execute('SELECT count(*) FROM inbox WHERE trace_id=? AND parent_id IS NOT NULL', (trace,)).fetchone()[0]
                if used >= budget:
                    raise ValueError('Delegation budget exceeded')
        definition = effective_agent(con, self.agents[agent]).model_dump(mode='json')
        con.execute('''INSERT INTO inbox(id,request_key,request_hash,agent,objective,inputs,listing_ref,trace_id,
            parent_id,source,mode,priority,state,created_at,available_at,config_revision,definition,executor_tool,caused_by_task_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task_id, request_key, digest(identity), agent, objective, canonical(inputs), listing_ref, trace,
             parent['id'] if parent else None, source, mode, priority, 'queued', now, now,
             self.config.revision, canonical(definition), executor_tool, caused_by['id'] if caused_by else None))
        task = con.execute('SELECT * FROM inbox WHERE id=?', (task_id,)).fetchone()
        self._event(con, task, 'task_submitted', identity, now)
        return task_id

    def submit(self, *, agent, objective, inputs, request_key, source='user', mode='local',
               priority=50, listing_ref=None, executor_tool=None, now=None):
        with self.transaction() as con:
            return self._submit(con, agent=agent, objective=objective, inputs=inputs, request_key=request_key,
                                source=source, mode=mode, priority=priority, listing_ref=listing_ref, executor_tool=executor_tool,
                                now=time.time() if now is None else now)

    def _expire(self, con, now):
        for task in con.execute("SELECT * FROM inbox WHERE state='running' AND lease_until<=?", (now,)).fetchall():
            calls = con.execute("SELECT * FROM tool_calls WHERE task_id=? AND state='started'", (task['id'],)).fetchall()
            incident=None
            for call in calls:
                con.execute("UPDATE tool_calls SET state='uncertain' WHERE id=?", (call['id'],))
                if call['resource']:
                    con.execute("UPDATE resources SET halted='uncertain_execution',owner=NULL WHERE key=?", (call['resource'],))
                    incident=self._source_incident(con,task,call['resource'],'uncertain_execution',now)
            con.execute("UPDATE inbox SET state=?,reason='worker_lost',token=NULL,lease_until=NULL WHERE id=?",
                        ('uncertain' if calls else 'blocked', task['id']))
            self._event(con, task, 'worker_lost', {'automatic_retry': False}, now)
            self._record_outcome(con, task, 'uncertain' if calls else 'blocked', None, 'worker_lost', now, incident)

    def claim(self, *, mode, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._expire(con, now)
            task = con.execute('''SELECT * FROM inbox t WHERE mode=? AND state='queued' AND available_at<=?
                AND NOT EXISTS (SELECT 1 FROM inbox busy WHERE busy.agent=t.agent AND busy.state='running')
                ORDER BY priority DESC,created_at,id LIMIT 1''', (mode, now)).fetchone()
            if task is None:
                return None
            token = uuid4().hex
            con.execute("UPDATE inbox SET state='running',token=?,lease_until=? WHERE id=?", (token, now + 600, task['id']))
            EventBus.progress(con, task['id'], 'running', now)
            self._event(con, task, 'turn_started', {'agent': task['agent'], 'inputs': json.loads(task['inputs'])}, now)
            return {**dict(task), 'token': token, 'state': 'running'}

    @staticmethod
    def _owned(con, task, now):
        row = con.execute('SELECT * FROM inbox WHERE id=?', (task['id'],)).fetchone()
        if not row or row['state'] != 'running' or row['token'] != task['token'] or row['lease_until'] <= now:
            raise RuntimeError('task_lease_lost')
        return row

    def context(self, task):
        with self.transaction() as con:
            events = con.execute('SELECT kind,data FROM agency_events WHERE task_id=? ORDER BY id', (task['id'],)).fetchall()
            children = con.execute('SELECT id,agent,state,result,reason FROM inbox WHERE parent_id=? ORDER BY created_at,id', (task['id'],)).fetchall()
            return {'task_id': task['id'], 'objective': task['objective'], 'inputs': json.loads(task['inputs']),
                    'listing_ref': task['listing_ref'], 'mode': task['mode'],
                    'received_records': self.records.task_context(con, task),
                    'received_events': EventBus.received(con, task),
                    'source_incidents': [dict(r) for r in con.execute('''SELECT DISTINCT i.* FROM source_incidents i
                        JOIN record_events e ON e.record_id=i.id AND e.record_kind='incident'
                        JOIN record_deliveries d ON d.event_id=e.id WHERE d.target_task_id=? AND d.state='delivered'
                        AND i.mode=?''',(task['id'],task['mode']))],
                    'record_reads': [json.loads(e['data']) for e in events if e['kind'] == 'record_read'][-1:],
                    'history': [{'kind': e['kind'], 'data': json.loads(e['data'])} for e in events
                                if e['kind'] in {'decision', 'tool_finished', 'delegated', 'resumed'}][-24:],
                    'delegated_results': [{**dict(r), 'result': json.loads(r['result']) if r['result'] else None} for r in children]}

    def decision(self, task, decision, generation, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._owned(con, task, now)
            con.execute('UPDATE inbox SET pending=?,turns=turns+1 WHERE id=?', (canonical(decision), task['id']))
            self._event(con, task, 'decision', {'decision': decision, 'generation': generation,
                                             'prompt_version': json.loads(task['definition'])['version'],
                                             'config_revision': task['config_revision']}, now)

    def transition(self, task, state, *, reason=None, result=None, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._owned(con, task, now)
            if state == 'complete' and con.execute("SELECT 1 FROM inbox WHERE parent_id=? AND state!='complete' LIMIT 1", (task['id'],)).fetchone():
                state, reason, result = 'waiting_children', 'awaiting_delegated_work', None
            if state == 'complete' and con.execute("SELECT 1 FROM record_deliveries WHERE target_task_id=? AND state='delivered' AND wake=1 AND started_at IS NULL LIMIT 1",(task['id'],)).fetchone():
                state,reason,result='queued','new_notifications_arrived',None
            con.execute('UPDATE inbox SET state=?,reason=?,result=?,pending=NULL,token=NULL,lease_until=NULL WHERE id=?',
                        (state, reason, canonical(result) if result is not None else None, task['id']))
            self._event(con, task, 'task_' + state, {'reason': reason, 'result': result}, now)
            self._record_outcome(con, task, state, result, reason, now)

    def _record_outcome(self, con, task, state, result, reason, now, incident_id=None):
        if state not in OUTCOME_STATES:
            return
        outcome = self.records.outcome(con, task, state, result, reason, self.supervisor_agent, now, incident_id)
        EventBus.progress(con, task['id'], state, now)
        self._event(con, task, 'outcome_recorded', {'outcome_id': outcome, 'state': state}, now)
        if task['parent_id']:
            self._wake_parent(con, task, state, result, now)
            con.execute("UPDATE record_deliveries SET state='delivered',attempts=1,delivered_at=? WHERE outcome_id=? AND route='parent'", (now, outcome))
            self._event(con, task, 'record_delivered', {'outcome_id': outcome, 'target_task_id': task['parent_id'], 'route': 'parent'}, now)

    def register_subscription(self, **spec):
        with self.transaction() as con:
            EventBus.register(con,self.agents,**spec)

    def dispatch_record_events(self, *, mode, now=None):
        """Each recipient commits independently; source tools never run on redelivery."""
        now = time.time() if now is None else now
        created=[]
        with self.transaction() as con:
            rows=con.execute("SELECT d.* FROM record_deliveries d JOIN record_events e ON e.id=d.event_id WHERE d.state='pending' AND d.mode=? AND d.available_at<=? ORDER BY e.rowid,d.id LIMIT 100",(mode,now)).fetchall()
            for delivery in rows:
                con.execute('SAVEPOINT deliver_record')
                target=None
                try:
                    if delivery['target_agent'] not in self.agents:raise ValueError('Unknown subscriber')
                    task=con.execute('SELECT * FROM inbox WHERE id=?',(delivery['task_id'],)).fetchone()
                    target=delivery['target_task_id']
                    if delivery['wake'] and target is None:
                        target=self._submit(con, agent=delivery['target_agent'],
                            objective='Ortak kayıt servisinden gelen olayı ve kanıtlarını değerlendir. Gerekliyse sonraki işi belirle; gerekmiyorsa sonucu özetleyip tamamla. Engel veya belirsizlik giderilmediyse giderilmiş sayma. Sentetik olayları örnek olarak belirt.',
                            inputs={'event_id':delivery['event_id'],'outcome_id':delivery['outcome_id'],'origin_task_id':task['id']},
                            request_key='delivery:'+delivery['id'],source='record:'+delivery['event_id'],
                            mode=task['mode'],priority=task['priority'],listing_ref=task['listing_ref'],caused_by=task,now=now)
                        created.append(target)
                    con.execute("""UPDATE record_deliveries SET state='delivered',target_task_id=?,attempts=attempts+1,
                        error=NULL,delivered_at=?,processing_state=? WHERE id=?""",
                        (target,now,'waiting' if delivery['wake'] else 'observed',delivery['id']))
                    if target and delivery['wake']:
                        # Routine evidence is bound to the evaluation that consumes its task outcome.
                        con.execute("""UPDATE record_deliveries SET target_task_id=?,processing_state='waiting'
                            WHERE task_id=? AND target_agent=? AND state='delivered' AND wake=0
                            AND target_task_id IS NULL AND created_at<=?""",(target,task['id'],delivery['target_agent'],delivery['created_at']))
                    self._event(con,task,'record_delivered',{'event_id':delivery['event_id'],'outcome_id':delivery['outcome_id'],
                        'target_agent':delivery['target_agent'],'target_task_id':target,'route':delivery['route'],'wake':bool(delivery['wake'])},now)
                    con.execute('RELEASE deliver_record')
                except Exception as error:
                    con.execute('ROLLBACK TO deliver_record');con.execute('RELEASE deliver_record')
                    if target in created:created.remove(target)
                    attempt=delivery['attempts']+1
                    transient=isinstance(error,(TimeoutError,ConnectionError)) or (
                        isinstance(error,sqlite3.OperationalError) and getattr(error,'sqlite_errorcode',None) in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED})
                    retry=transient and attempt<=len(DELIVERY_DELAYS)
                    con.execute("UPDATE record_deliveries SET state=?,attempts=?,error=?,available_at=? WHERE id=?",
                        ('pending' if retry else 'blocked',attempt,type(error).__name__,
                         now+DELIVERY_DELAYS[attempt-1] if retry else now,delivery['id']))
        return created

    def retry_record_delivery(self, delivery_id):
        with self.transaction() as con:
            row=con.execute('SELECT * FROM record_deliveries WHERE id=?',(delivery_id,)).fetchone()
            if not row or row['state']!='blocked':raise ValueError('Only blocked record deliveries can retry')
            con.execute("UPDATE record_deliveries SET state='pending',error=NULL,available_at=0 WHERE id=?",(delivery_id,))

    def record_detail(self, kind, record_id):
        table = {'observation': 'observations', 'outcome': 'record_outcomes'}.get(kind)
        if not table:
            raise KeyError('Unknown record type')
        with self.transaction() as con:
            result = self.records.decode(con.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone(), full=True)
            if kind == 'outcome':
                result['observations'] = [self.records.decode(con.execute('SELECT * FROM observations WHERE id=?', (key,)).fetchone(), full=True) for key in result['observation_ids']]
            result['deliveries']=[dict(r) for r in con.execute('SELECT d.* FROM record_deliveries d JOIN record_events e ON e.id=d.event_id WHERE e.record_kind=? AND e.record_id=?',(kind,record_id))]
            return result

    def read_record(self, task, record_id, offset=0, now=None):
        """A bounded read of own/delivered evidence, never arbitrary cross-task data."""
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid record offset')
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._owned(con, task, now)
            row = con.execute('''SELECT o.* FROM observations o WHERE o.id=? AND o.mode=? AND
                (o.task_id=? OR EXISTS (SELECT 1 FROM record_deliveries d
                 JOIN record_outcomes r ON r.id=d.outcome_id
                 WHERE d.target_task_id=? AND d.state='delivered'
                 AND EXISTS (SELECT 1 FROM json_each(r.observation_ids) WHERE value=o.id)))''',
                (record_id, task['mode'], task['id'], task['id'])).fetchone()
            kind = 'observation'
            if row is None:
                kind = 'outcome'
                row = con.execute('''SELECT r.* FROM record_outcomes r WHERE r.id=? AND r.mode=? AND
                    (r.task_id=? OR EXISTS (SELECT 1 FROM record_deliveries d WHERE d.outcome_id=r.id
                     AND d.target_task_id=? AND d.state='delivered'))''',
                    (record_id, task['mode'], task['id'], task['id'])).fetchone()
            if row is None:
                raise ValueError('Record not delivered to this task')
            encoded = canonical(self.records.decode(row, full=True))
            self._event(con, task, 'record_read', {'record_id': record_id, 'record_kind': kind, 'offset': offset,
                'text': encoded[offset:offset + 8000], 'next_offset': offset + 8000 if offset + 8000 < len(encoded) else None,
                'total_characters': len(encoded)}, now)
            con.execute("UPDATE inbox SET state='queued',pending=NULL,token=NULL,lease_until=NULL WHERE id=?", (task['id'],))

    def _wake_parent(self, con, task, state, result, now):
        if not task['parent_id']:
            return
        parent = con.execute('SELECT * FROM inbox WHERE id=?', (task['parent_id'],)).fetchone()
        if state == 'complete':
            self._event(con, parent, 'child_result_received', {'child_id': task['id'], 'agent': task['agent'], 'result': result}, now)
            unfinished = con.execute("SELECT 1 FROM inbox WHERE parent_id=? AND state!='complete' LIMIT 1", (parent['id'],)).fetchone()
            if parent['state'] == 'waiting_children' and not unfinished:
                con.execute("UPDATE inbox SET state='queued',available_at=? WHERE id=?", (now, parent['id']))
        elif state in {'blocked', 'uncertain', 'failed', 'waiting_user', 'cancelled'}:
            self._event(con, parent, 'child_needs_attention', {'child_id': task['id'], 'agent': task['agent'], 'state': state}, now)
            if parent['state'] == 'waiting_children':
                con.execute("UPDATE inbox SET state='queued',available_at=? WHERE id=?", (now, parent['id']))

    def delegate(self, task, target, objective, inputs, listing_ref, now=None, *, wait=True):
        now = time.time() if now is None else now
        if target not in json.loads(task['definition'])['delegates']:
            raise ValueError('delegation_not_granted')
        with self.transaction() as con:
            row = self._owned(con, task, now)
            child = self._submit(con, agent=target, objective=objective, inputs=inputs,
                                 request_key=f"delegate:{task['id']}:{row['turns']}", source='agent:' + task['agent'],
                                 mode=task['mode'], priority=task['priority'], parent=task,
                                 listing_ref=listing_ref or task['listing_ref'], now=now)
            con.execute("UPDATE inbox SET state=?,pending=NULL,token=NULL,lease_until=NULL WHERE id=?", ('waiting_children' if wait else 'queued', task['id']))
            self._event(con, task, 'delegated', {'child_id': child, 'to_agent': target, 'objective': objective, 'inputs': inputs, 'await_result': wait}, now)
            return child

    def _source_incident(self, con, task, resource, reason, now):
        incident=con.execute("SELECT * FROM source_incidents WHERE resource=? AND mode=? AND state='open'",(resource,task['mode'])).fetchone()
        if incident is None:
            key=uuid4().hex
            con.execute("INSERT INTO source_incidents(id,resource,mode,reason,state,created_at) VALUES(?,?,?,?,'open',?)",(key,resource,task['mode'],reason,now))
            EventBus.emit(con,task,'source.blocked','incident',key,self.supervisor_agent,now,
                          {'incident_id':key,'resource':resource,'reason':reason})
        else:key=incident['id']
        con.execute('INSERT OR IGNORE INTO incident_tasks VALUES(?,?)',(key,task['id']))
        return key

    def resolve_source(self, incident_id, note, now=None):
        """Explicit local operator action after fixing access. Uncertain effects stay quarantined."""
        if not isinstance(note,str) or not note.strip() or len(note)>2000:raise ValueError('Resolution evidence required')
        now=time.time() if now is None else now
        with self.transaction() as con:
            incident=con.execute("SELECT * FROM source_incidents WHERE id=? AND state='open'",(incident_id,)).fetchone()
            if not incident:raise ValueError('Open source incident required')
            resource=con.execute('SELECT * FROM resources WHERE key=?',(incident['resource'],)).fetchone()
            if resource['owner'] or con.execute("SELECT 1 FROM tool_calls WHERE resource=? AND state IN ('uncertain','started') LIMIT 1",(incident['resource'],)).fetchone():
                raise ValueError('Uncertain or active operations require reconciliation first')
            con.execute("UPDATE source_incidents SET state='resolved',resolved_at=?,resolution=? WHERE id=?",(now,note,incident_id))
            con.execute('UPDATE resources SET halted=NULL WHERE key=?',(incident['resource'],))
            tasks=con.execute('SELECT t.* FROM inbox t JOIN incident_tasks i ON i.task_id=t.id WHERE i.incident_id=?',(incident_id,)).fetchall()
            for task in tasks:
                pending=json.loads(task['pending']) if task['pending'] else None
                tool=next((t for t in self.config.tools if pending and t.key==pending.get('target')),None)
                if task['state']=='blocked' and task['config_revision']==self.config.revision and tool and tool.effect=='read':
                    con.execute("UPDATE inbox SET state='queued',reason=NULL,available_at=?,retry=NULL WHERE id=?",(now,task['id']))
                    self._event(con,task,'source_resumed',{'incident_id':incident_id},now)
            if tasks:EventBus.emit(con,tasks[0],'source.recovered','incident',incident_id,self.supervisor_agent,now,{'incident_id':incident_id,'resource':incident['resource']})

    def begin_tool(self, task, tool, arguments, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            owned=self._owned(con, task, now)
            if tool.resource:
                con.execute('INSERT OR IGNORE INTO resources(key) VALUES(?)', (tool.resource,))
                resource = con.execute('SELECT * FROM resources WHERE key=?', (tool.resource,)).fetchone()
                if resource['halted'] or resource['owner'] or resource['next_at'] > now:
                    state = 'blocked' if resource['halted'] else 'queued'
                    why = resource['halted'] or ('resource_busy' if resource['owner'] else 'rate_limit_wait')
                    con.execute('UPDATE inbox SET state=?,reason=?,available_at=?,token=NULL,lease_until=NULL WHERE id=?',
                                (state, why, max(now + 1, resource['next_at']), task['id']))
                    self._event(con, task, 'tool_deferred', {'tool': tool.key, 'reason': why}, now)
                    if state == 'blocked':
                        incident=self._source_incident(con,task,tool.resource,why,now)
                        self._record_outcome(con, task, state, None, why, now, incident)
                    return None
            operation_key=task['id']+':'+str(owned['turns'])
            attempt=con.execute('SELECT count(*) FROM tool_calls WHERE operation_key=?',(operation_key,)).fetchone()[0]+1
            call_id = uuid4().hex
            con.execute('INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,started_at,operation_key,attempt) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (call_id, task['id'], tool.key, tool.resource, tool.effect, 'started', canonical(arguments), now,operation_key,attempt))
            if tool.resource:
                con.execute('UPDATE resources SET owner=? WHERE key=?', (call_id, tool.resource))
            self._event(con, task, 'tool_started', {'call_id': call_id, 'operation_key':operation_key,'attempt':attempt,
                'tool': tool.key, 'transport': tool.transport,'arguments': arguments, 'effect': tool.effect}, now)
            return call_id

    def finish_tool(self, task, tool, call_id, *, result=None, error=None, halt=False,
                    transient=False, retry_after=0, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            return self._finish_tool(con, task, tool, call_id, result=result, error=error, halt=halt,
                                     transient=transient, retry_after=retry_after, now=now)

    def _finish_tool(self, con, task, tool, call_id, *, result=None, error=None, halt=False,
                     transient=False, retry_after=0, now):
        self._owned(con, task, now)
        call = con.execute("SELECT * FROM tool_calls WHERE id=? AND task_id=? AND state='started'", (call_id, task['id'])).fetchone()
        if call is None:raise RuntimeError('tool_call_not_owned')
        uncertain = error is not None and tool.effect == 'write'
        retry=error is not None and transient and not halt and not uncertain and call['attempt']<=len(READ_DELAYS)
        next_at=now+max(tool.min_interval_seconds,READ_DELAYS[call['attempt']-1],retry_after) if retry else now+tool.min_interval_seconds
        state = 'uncertain' if uncertain else 'failed' if error else 'complete'
        con.execute('UPDATE tool_calls SET state=?,result=?,finished_at=? WHERE id=?',
                    (state, canonical({'output': result, 'error': error}), now, call_id))
        if tool.resource:
            con.execute('UPDATE resources SET owner=NULL,next_at=?,halted=? WHERE key=? AND owner=?',
                        (next_at, error if halt or uncertain else None, tool.resource, call_id))
        final_reason='read_retries_exhausted:'+error if error and transient and not retry and not uncertain else error
        info={'tool':tool.key,'category':'transient_read','attempt':call['attempt'],
              'max_attempts':len(READ_DELAYS)+1,'next_at':next_at} if retry else None
        con.execute("""UPDATE inbox SET state=?,reason=?,pending=CASE WHEN ? THEN pending ELSE NULL END,
            retry=?,available_at=?,token=NULL,lease_until=NULL WHERE id=?""",
            ('queued' if retry or not error else 'uncertain' if uncertain else 'blocked',final_reason,
             retry or halt,canonical(info) if info else None,next_at if retry else now,task['id']))
        self._event(con, task, 'tool_finished', {'call_id': call_id, 'tool': tool.key, 'state': state,
                                               'output': result, 'error': error}, now)
        if retry:
            self._event(con,task,'task_retry_scheduled',info,now)
            EventBus.emit(con,task,'task.retry_scheduled','attempt',call_id,self.supervisor_agent,now,info)
        elif error:
            incident=self._source_incident(con,task,tool.resource,error,now) if tool.resource and (halt or uncertain) else None
            self._record_outcome(con,task,'uncertain' if uncertain else 'blocked',None,
                                 final_reason,now,incident)
        else:
            record = self.records.observe(con, task, tool, call, result, now, self.supervisor_agent)
            self._event(con, task, 'observation_recorded', {'observation_id': record, 'call_id': call_id}, now)

    def resume(self, task_id, inputs, now=None):
        now = time.time() if now is None else now
        if len(canonical(inputs).encode()) > 16384:
            raise ValueError('Input too large')
        with self.transaction() as con:
            task = con.execute('SELECT * FROM inbox WHERE id=?', (task_id,)).fetchone()
            if not task or task['state'] != 'waiting_user':
                raise ValueError('Only tasks awaiting input can resume')
            con.execute("UPDATE inbox SET state='queued',reason=NULL,available_at=?,inputs=? WHERE id=?",
                        (now, canonical({**json.loads(task['inputs']), 'user_reply': inputs}), task_id))
            self._event(con, task, 'resumed', {'reply': inputs}, now)

    def overview(self, *, listing_ref=None, trace_id=None):
        con = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN')
            where, params = [], []
            for name, value in [('listing_ref', listing_ref), ('trace_id', trace_id)]:
                if value is not None:
                    where.append(name + '=?'); params.append(value)
            query = 'SELECT * FROM inbox' + (' WHERE ' + ' AND '.join(where) if where else '')
            total = con.execute('SELECT count(*) FROM (' + query + ')', params).fetchone()[0]
            counts = [dict(r) for r in con.execute('SELECT agent,state,mode,count(*) AS count FROM (' + query + ') GROUP BY agent,state,mode', params)]
            tasks = []
            for row in con.execute(query + ' ORDER BY created_at DESC,id DESC LIMIT 200', params):
                t = dict(row)
                for k in ('inputs', 'definition', 'pending', 'result', 'retry'):
                    t[k] = json.loads(t[k]) if t[k] else None
                t.pop('token'); t.pop('request_hash')
                tasks.append(t)
            ids = [t['id'] for t in tasks]
            events, more_events = [], False
            if ids:
                rows = con.execute('SELECT * FROM agency_events WHERE task_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id DESC LIMIT 1001', ids).fetchall()
                more_events = len(rows) > 1000
                events = [{**dict(r), 'data': json.loads(r['data'])} for r in rows[:1000]][::-1]
            return {'tasks': tasks, 'events': events, 'resources': [dict(r) for r in con.execute('SELECT * FROM resources')],
                    'records': self.records.summary(con, ids), 'supervisor_agent': self.supervisor_agent,
                    'subscriptions': EventBus.subscriptions(con,self.supervisor_agent),
                    'incidents': [{**dict(r),'tasks':[t[0] for t in con.execute('SELECT task_id FROM incident_tasks WHERE incident_id=?',(r['id'],))]} for r in con.execute('SELECT * FROM source_incidents ORDER BY created_at DESC LIMIT 100')],
                    'error_policy': {'read_attempts':len(READ_DELAYS)+1,'read_delays':READ_DELAYS,'delivery_attempts':len(DELIVERY_DELAYS)+1,'delivery_delays':DELIVERY_DELAYS,'uncertain_write_retry':False},
                    'triggers': [{**dict(r), 'spec': json.loads(r['spec'])} for r in con.execute('SELECT * FROM triggers')],
                    'limits': {'tasks': 200, 'events': 1000}, 'total_tasks': total, 'task_counts': counts,
                    'tasks_truncated': total > len(tasks), 'events_truncated': more_events,
                    'workers': [{**dict(r), 'active': r['state'] != 'stopped' and r['expires_at'] > time.time()}
                                for r in con.execute('SELECT * FROM workers ORDER BY seen_at DESC LIMIT 20')],
                    'config_revision': self.config.revision}
        finally:
            con.close()

    def register_trigger(self, key, kind, spec, next_at=None):
        if kind not in {'timer', 'event'} or spec['agent'] not in self.agents:
            raise ValueError('Invalid trigger')
        if kind == 'timer':
            if next_at is None or spec.get('interval_seconds', 0) < 1:
                raise ValueError('Timer requires next_at and positive interval_seconds')
        elif not spec.get('event_type'):
            raise ValueError('Event trigger requires event_type')
        with self.transaction() as con:
            previous = con.execute('SELECT * FROM triggers WHERE key=?', (key,)).fetchone()
            if previous:
                if previous['kind'] != kind or previous['spec'] != canonical(spec):
                    raise ValueError('Trigger already registered with different configuration')
                return
            con.execute('INSERT INTO triggers(key,kind,spec,next_at) VALUES(?,?,?,?)', (key, kind, canonical(spec), next_at))

    def set_trigger_enabled(self, key, enabled):
        with self.transaction() as con:
            if not con.execute('SELECT 1 FROM triggers WHERE key=?', (key,)).fetchone():
                raise KeyError(key)
            con.execute('UPDATE triggers SET enabled=? WHERE key=?', (int(enabled), key))

    def tick(self, now=None):
        now = time.time() if now is None else now
        created = []
        with self.transaction() as con:
            for row in con.execute("SELECT * FROM triggers WHERE kind='timer' AND enabled=1 AND next_at<=?", (now,)).fetchall():
                spec = json.loads(row['spec'])
                # Missed intervals coalesce to one delivery. No morning backlog burst.
                created.append(self._submit(con, agent=spec['agent'], objective=spec['objective'],
                    inputs=spec.get('inputs', {}), request_key=f"timer:{row['key']}:{row['next_at']}",
                    source='timer:' + row['key'], mode=spec.get('mode', 'local'), priority=spec.get('priority', 20), executor_tool=spec.get('executor_tool'), now=now))
                periods = int((now - row['next_at']) // spec['interval_seconds']) + 1
                con.execute('UPDATE triggers SET next_at=? WHERE key=?',
                            (row['next_at'] + periods * spec['interval_seconds'], row['key']))
        return created

    def publish(self, event_key, event_type, payload, now=None):
        now = time.time() if now is None else now
        if not event_key or len(event_key) > 128 or len(event_type) > 128 or len(canonical(payload).encode()) > 16384:
            raise ValueError('Invalid or oversized event')
        identity = digest({'type': event_type, 'payload': payload})
        with self.transaction() as con:
            old = con.execute('SELECT body_hash FROM incoming_events WHERE key=?', (event_key,)).fetchone()
            if old:
                if old[0] != identity:
                    raise ValueError('Event identity conflict')
                return []
            created = []
            for row in con.execute("SELECT * FROM triggers WHERE kind='event' AND enabled=1"):
                spec = json.loads(row['spec'])
                if spec['event_type'] == event_type:
                    created.append(self._submit(con, agent=spec['agent'], objective=spec['objective'], inputs=payload,
                        request_key=f"event:{row['key']}:{event_key}", source='event:' + event_type,
                        mode=spec.get('mode', 'local'), priority=spec.get('priority', 50), executor_tool=spec.get('executor_tool'), now=now))
            con.execute('INSERT INTO incoming_events VALUES(?,?)', (event_key, identity))
            return created
