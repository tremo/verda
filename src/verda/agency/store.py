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

TERMINAL = {'complete', 'cancelled', 'failed'}


class AgencyStore:
    def __init__(self, path: Path, config: AgencyConfig):
        self.path = path.resolve()
        self.config = config
        self.agents = {a.key: a for a in config.agents}

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
                if 'agency_meta' not in tables or con.execute('SELECT version FROM agency_meta').fetchone()[0] not in {1, 2, 3}:
                    raise ValueError('Foreign agency database')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 1:
                    con.execute('ALTER TABLE inbox ADD COLUMN executor_tool TEXT')
                    con.execute('UPDATE agency_meta SET version=2')
                if con.execute('SELECT version FROM agency_meta').fetchone()[0] == 2:
                    con.execute('CREATE TABLE workers(id TEXT PRIMARY KEY, mode TEXT NOT NULL, state TEXT NOT NULL, seen_at REAL NOT NULL, expires_at REAL NOT NULL)')
                    con.execute('UPDATE agency_meta SET version=3')
                return
            for sql in [
                'CREATE TABLE agency_meta(version INTEGER PRIMARY KEY)', 'INSERT INTO agency_meta VALUES(3)',
                '''CREATE TABLE inbox(id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                agent TEXT NOT NULL, objective TEXT NOT NULL, inputs TEXT NOT NULL, listing_ref TEXT, trace_id TEXT NOT NULL,
                parent_id TEXT REFERENCES inbox(id), source TEXT NOT NULL, mode TEXT NOT NULL, priority INTEGER NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, available_at REAL NOT NULL, turns INTEGER NOT NULL DEFAULT 0,
                config_revision TEXT NOT NULL, definition TEXT NOT NULL, token TEXT, lease_until REAL, reason TEXT,
                pending TEXT, result TEXT, executor_tool TEXT)''',
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
                listing_ref=None, parent=None, executor_tool=None, now):
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
        old = con.execute('SELECT * FROM inbox WHERE request_key=?', (request_key,)).fetchone()
        if old:
            if old['request_hash'] != digest(identity):
                raise ValueError('Request key conflict')
            return old['id']
        task_id = uuid4().hex
        trace = parent['trace_id'] if parent else uuid4().hex
        if parent and con.execute('SELECT count(*) FROM inbox WHERE trace_id=?', (trace,)).fetchone()[0] >= 100:
            raise ValueError('Trace task budget exceeded')
        definition = self.agents[agent].model_dump(mode='json')
        con.execute('''INSERT INTO inbox(id,request_key,request_hash,agent,objective,inputs,listing_ref,trace_id,
            parent_id,source,mode,priority,state,created_at,available_at,config_revision,definition,executor_tool)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (task_id, request_key, digest(identity), agent, objective, canonical(inputs), listing_ref, trace,
             parent['id'] if parent else None, source, mode, priority, 'queued', now, now,
             self.config.revision, canonical(definition), executor_tool))
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
            for call in calls:
                con.execute("UPDATE tool_calls SET state='uncertain' WHERE id=?", (call['id'],))
                if call['resource']:
                    con.execute("UPDATE resources SET halted='uncertain_execution',owner=NULL WHERE key=?", (call['resource'],))
            con.execute("UPDATE inbox SET state=?,reason='worker_lost',token=NULL,lease_until=NULL WHERE id=?",
                        ('uncertain' if calls else 'blocked', task['id']))
            self._event(con, task, 'worker_lost', {'automatic_retry': False}, now)
            self._wake_parent(con, task, 'uncertain' if calls else 'blocked', None, now)

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
            con.execute('UPDATE inbox SET state=?,reason=?,result=?,pending=NULL,token=NULL,lease_until=NULL WHERE id=?',
                        (state, reason, canonical(result) if result is not None else None, task['id']))
            self._event(con, task, 'task_' + state, {'reason': reason, 'result': result}, now)
            self._wake_parent(con, task, state, result, now)

    def _wake_parent(self, con, task, state, result, now):
        if not task['parent_id']:
            return
        parent = con.execute('SELECT * FROM inbox WHERE id=?', (task['parent_id'],)).fetchone()
        if state == 'complete':
            self._event(con, parent, 'child_result_received', {'child_id': task['id'], 'agent': task['agent'], 'result': result}, now)
            unfinished = con.execute("SELECT 1 FROM inbox WHERE parent_id=? AND state!='complete' LIMIT 1", (parent['id'],)).fetchone()
            if parent['state'] == 'waiting_children' and not unfinished:
                con.execute("UPDATE inbox SET state='queued',available_at=? WHERE id=?", (now, parent['id']))
        elif state in {'blocked', 'uncertain', 'failed', 'waiting_user'}:
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

    def begin_tool(self, task, tool, arguments, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._owned(con, task, now)
            if tool.resource:
                con.execute('INSERT OR IGNORE INTO resources(key) VALUES(?)', (tool.resource,))
                resource = con.execute('SELECT * FROM resources WHERE key=?', (tool.resource,)).fetchone()
                if resource['halted'] or resource['owner'] or resource['next_at'] > now:
                    state = 'blocked' if resource['halted'] else 'queued'
                    why = resource['halted'] or ('resource_busy' if resource['owner'] else 'rate_limit_wait')
                    con.execute('UPDATE inbox SET state=?,reason=?,available_at=?,token=NULL,lease_until=NULL WHERE id=?',
                                (state, why, max(now + 1, resource['next_at']), task['id']))
                    self._event(con, task, 'tool_deferred', {'tool': tool.key, 'reason': why}, now)
                    return None
            call_id = uuid4().hex
            con.execute('INSERT INTO tool_calls(id,task_id,tool,resource,effect,state,arguments,started_at) VALUES(?,?,?,?,?,?,?,?)',
                        (call_id, task['id'], tool.key, tool.resource, tool.effect, 'started', canonical(arguments), now))
            if tool.resource:
                con.execute('UPDATE resources SET owner=? WHERE key=?', (call_id, tool.resource))
            self._event(con, task, 'tool_started', {'call_id': call_id, 'tool': tool.key, 'transport': tool.transport,
                                                  'arguments': arguments, 'effect': tool.effect}, now)
            return call_id

    def finish_tool(self, task, tool, call_id, *, result=None, error=None, halt=False, now=None):
        now = time.time() if now is None else now
        with self.transaction() as con:
            self._owned(con, task, now)
            call = con.execute("SELECT * FROM tool_calls WHERE id=? AND task_id=? AND state='started'", (call_id, task['id'])).fetchone()
            if call is None:
                raise RuntimeError('tool_call_not_owned')
            uncertain = error is not None and tool.effect == 'write'
            state = 'uncertain' if uncertain else 'failed' if error else 'complete'
            con.execute('UPDATE tool_calls SET state=?,result=?,finished_at=? WHERE id=?',
                        (state, canonical({'output': result, 'error': error}), now, call_id))
            if tool.resource:
                con.execute('UPDATE resources SET owner=NULL,next_at=?,halted=? WHERE key=? AND owner=?',
                            (now + tool.min_interval_seconds, error if halt or uncertain else None, tool.resource, call_id))
            con.execute('UPDATE inbox SET state=?,reason=?,pending=NULL,token=NULL,lease_until=NULL WHERE id=?',
                        ('uncertain' if uncertain else 'blocked' if error else 'queued', error, task['id']))
            self._event(con, task, 'tool_finished', {'call_id': call_id, 'tool': tool.key, 'state': state,
                                                   'output': result, 'error': error}, now)
            if error:
                self._wake_parent(con, task, 'uncertain' if uncertain else 'blocked', None, now)

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
                for k in ('inputs', 'definition', 'pending', 'result'):
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
