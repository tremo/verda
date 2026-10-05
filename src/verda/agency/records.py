"""Append-only evidence and outcomes. The runtime writes; models do not own SQL.

All methods receiving a connection participate in the caller's transaction.
The outbox only carries record references; delivery never calls a model.
"""
from __future__ import annotations

import json
import math
from uuid import uuid4

from verda.legacy import canonical, digest
from verda.agency.events import EventBus


OUTCOME_STATES = {'complete', 'blocked', 'uncertain', 'failed', 'cancelled', 'waiting_user'}


class RecordService:
    @staticmethod
    def install(con):
        for sql in [
            '''CREATE TABLE observations(id TEXT PRIMARY KEY, call_id TEXT UNIQUE NOT NULL REFERENCES tool_calls(id),
            task_id TEXT NOT NULL REFERENCES inbox(id), agent TEXT NOT NULL, trace_id TEXT NOT NULL,
            listing_ref TEXT, mode TEXT NOT NULL, tool TEXT NOT NULL, resource TEXT, kind TEXT NOT NULL,
            captured_at REAL NOT NULL, arguments TEXT NOT NULL, payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL, fields TEXT NOT NULL)''',
            'CREATE INDEX observation_subject ON observations(mode,listing_ref,captured_at)',
            '''CREATE TABLE record_outcomes(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES inbox(id),
            version INTEGER NOT NULL, agent TEXT NOT NULL, trace_id TEXT NOT NULL, listing_ref TEXT, mode TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT, result TEXT, kind TEXT NOT NULL, recorded_at REAL NOT NULL,
            observation_ids TEXT NOT NULL, UNIQUE(task_id,version))''',
            '''CREATE TABLE record_deliveries(id TEXT PRIMARY KEY, outcome_id TEXT UNIQUE NOT NULL REFERENCES record_outcomes(id),
            task_id TEXT NOT NULL REFERENCES inbox(id), mode TEXT NOT NULL, route TEXT NOT NULL,
            target_agent TEXT NOT NULL, target_task_id TEXT, state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, delivered_at REAL)''',
            'CREATE INDEX record_delivery_queue ON record_deliveries(mode,state,created_at)',
        ]:
            con.execute(sql)
        for table in ('observations', 'record_outcomes'):
            for action in ('UPDATE', 'DELETE'):
                con.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                            "BEGIN SELECT RAISE(ABORT,'records_are_append_only'); END")

    @staticmethod
    def normalized_fields(payload):
        """Typed reported values, not verified facts or an overwrite of older values."""
        if not isinstance(payload, dict):
            return {}
        fields = {}
        for key, unit in [('price_tl', 'TRY'), ('area_m2', 'm²'), ('elevation_m', 'm')]:
            value = payload.get(key)
            if type(value) in (int, float) and math.isfinite(value) and (key == 'elevation_m' or value >= 0):
                fields[key] = {'value': value, 'unit': unit}
        if isinstance(payload.get('parcel_key'), str) and len(payload['parcel_key']) <= 256:
            fields['parcel_key'] = {'value': payload['parcel_key']}
        return fields

    def observe(self, con, task, tool, call, payload, now, supervisor=None):
        encoded = canonical(payload)
        if len(encoded.encode()) > 32768:
            raise ValueError('record_payload_too_large')
        old = con.execute('SELECT id,payload_hash FROM observations WHERE call_id=?', (call['id'],)).fetchone()
        if old:
            if old['payload_hash'] != digest(payload):
                raise ValueError('observation_identity_conflict')
            return old['id']
        args = json.loads(call['arguments'])
        listing_ref = task['listing_ref'] or (args.get('listing_id') if isinstance(args.get('listing_id'), str) else None)
        kind = 'calculation' if tool.key == 'policy.screen' else 'external_receipt' if tool.effect == 'write' else 'tool_observation'
        record_id = uuid4().hex
        con.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (record_id, call['id'], task['id'], task['agent'], task['trace_id'], listing_ref, task['mode'],
                     tool.key, tool.resource, kind, now, call['arguments'], encoded, digest(payload),
                     canonical(self.normalized_fields(payload))))
        EventBus.emit(con, task, 'observation.recorded', 'observation', record_id, supervisor, now)
        return record_id

    @staticmethod
    def outcome(con, task, state, result, reason, supervisor, now, incident_id=None):
        if state not in OUTCOME_STATES:
            return None
        current = con.execute('SELECT record_version FROM inbox WHERE id=?', (task['id'],)).fetchone()
        version = current['record_version'] + 1
        observation_ids = [r['id'] for r in con.execute('SELECT id FROM observations WHERE task_id=? ORDER BY captured_at,id', (task['id'],))]
        # A supervisor decision retains the evidence it received, even if it called no tools.
        for row in con.execute('''SELECT r.observation_ids FROM record_outcomes r
                JOIN record_deliveries d ON d.outcome_id=r.id
                WHERE d.target_task_id=? AND d.state='delivered' AND r.mode=? ORDER BY r.recorded_at,r.id''',
                (task['id'], task['mode'])):
            observation_ids.extend(json.loads(row['observation_ids']))
        observation_ids = list(dict.fromkeys(observation_ids))
        record_id = uuid4().hex
        kind = 'manager_decision' if task['agent'] == supervisor else 'task_outcome'
        con.execute('INSERT INTO record_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (record_id, task['id'], version, task['agent'], task['trace_id'], task['listing_ref'], task['mode'],
                     state, reason, canonical(result) if result is not None else None, kind, now, canonical(observation_ids)))
        con.execute('UPDATE inbox SET record_version=? WHERE id=?', (version, task['id']))
        EventBus.emit(con, task, 'task.'+state, 'outcome', record_id, supervisor, now,
                      {'state':state,'reason':reason,'incident_id':incident_id})
        return record_id

    @staticmethod
    def decode(row, *, full=False):
        if row is None:
            raise KeyError('Record not found')
        result = dict(row)
        for key in ('payload', 'arguments', 'fields', 'result', 'observation_ids'):
            if key in result:
                result[key] = json.loads(result[key]) if result[key] is not None else None
        if not full:
            for key in ('payload', 'arguments', 'result'):
                result.pop(key, None)
        if 'call_id' in result:
            result['verification'] = 'unverified'
        return result

    def task_context(self, con, task):
        outcomes = con.execute('''SELECT o.* FROM record_outcomes o JOIN record_deliveries d ON d.outcome_id=o.id
            WHERE d.target_task_id=? AND d.state='delivered' AND o.mode=? ORDER BY o.recorded_at DESC LIMIT 12''',
                               (task['id'], task['mode'])).fetchall()
        delivered = []
        for row in outcomes:
            item = self.decode(row)
            ids = item.pop('observation_ids')
            item['observation_count'] = len(ids)
            item['observation_ids'] = ids[:32]
            if len(ids) > 32:
                item['observation_ids_omitted'] = True
            raw_result = json.loads(row['result']) if row['result'] else None
            encoded = canonical(raw_result)
            if len(canonical(delivered + [item]).encode()) + len(encoded.encode()) < 18000:
                item['result'] = raw_result
            else:
                item['result_omitted'] = True
            item['observations'] = []
            for record_id in item['observation_ids']:
                observation = self.decode(con.execute('SELECT * FROM observations WHERE id=? AND mode=?', (record_id, task['mode'])).fetchone(), full=True)
                item['observations'].append(observation)
                if len(canonical(delivered + [item]).encode()) > 18000:
                    item['observations'][-1] = {'id': record_id, 'tool': observation['tool'], 'payload_omitted': True}
            if len(canonical(delivered + [item]).encode()) <= 20000:
                delivered.append(item)
            else:
                delivered.append({'id': row['id'], 'state': row['state'], 'record_omitted': True})
        return delivered

    def summary(self, con, task_ids):
        where = ' WHERE task_id IN (' + ','.join('?' for _ in task_ids) + ')' if task_ids else ' WHERE 0'
        observations = [self.decode(r) for r in con.execute('SELECT * FROM observations' + where + ' ORDER BY captured_at DESC LIMIT 200', task_ids)]
        outcomes = [self.decode(r) for r in con.execute('SELECT * FROM record_outcomes' + where + ' ORDER BY recorded_at DESC LIMIT 200', task_ids)]
        deliveries = [dict(r) for r in con.execute('SELECT * FROM record_deliveries' + where + ' ORDER BY created_at DESC LIMIT 200', task_ids)]
        return {'observations': observations, 'outcomes': outcomes, 'deliveries': deliveries,
                'observation_count': con.execute('SELECT count(*) FROM observations' + where, task_ids).fetchone()[0],
                'outcome_count': con.execute('SELECT count(*) FROM record_outcomes' + where, task_ids).fetchone()[0],
                'pending_count': con.execute("SELECT count(*) FROM record_deliveries WHERE state='pending'").fetchone()[0],
                'blocked_count': con.execute("SELECT count(*) FROM record_deliveries WHERE state='blocked'").fetchone()[0],
                'events': [{**dict(r),'data':json.loads(r['data'])} for r in con.execute('SELECT * FROM record_events'+where+' ORDER BY created_at DESC LIMIT 200',task_ids)],
                'limit': 200}
