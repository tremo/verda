"""SQLite side of the facts layer: append-only claims, versioned assessments."""
from __future__ import annotations

import json
from uuid import uuid4

from verda.facts.action import OFFICIALLY_CHECKABLE, UNVERIFIED_GRADES, next_action
from verda.facts.assemble import assemble
from verda.facts.extract import EXTRACTORS, extract
from verda.facts.model import FIELDS, LISTING, SEARCH, THREAD, TOOL_GRADE, Proposal, Verdict, accept, reject
from verda.facts.verify import VERIFIER, source_kind, verify
from verda.legacy import canonical, digest
from verda.policy import Decision, Policy, ScreeningFacts, evaluate

STATE_ORDER = ('excluded', 'outside_search', 'research_required', 'conditional', 'eligible')


def install(con):
    con.execute('''CREATE TABLE IF NOT EXISTS claims(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        listing_ref TEXT NOT NULL, mode TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
        scope TEXT NOT NULL, source_kind TEXT NOT NULL, key TEXT,
        observation_id TEXT REFERENCES observations(id), source_ref TEXT, quote TEXT,
        subject TEXT NOT NULL, proposer TEXT NOT NULL, status TEXT NOT NULL, grade TEXT,
        reason TEXT NOT NULL, resolves TEXT REFERENCES claims(id), verifier TEXT NOT NULL,
        created_at REAL NOT NULL, dedupe_key TEXT UNIQUE NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS claims_listing ON claims(listing_ref,mode,field,status)')
    con.execute('''CREATE TABLE IF NOT EXISTS assessments(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        listing_ref TEXT NOT NULL, mode TEXT NOT NULL, policy_version TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL, facts TEXT NOT NULL, basis TEXT NOT NULL, conflicts TEXT NOT NULL,
        state TEXT NOT NULL, reasons TEXT NOT NULL, conditions TEXT NOT NULL, created_at REAL NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS assessments_listing ON assessments(listing_ref,mode,seq)')
    for table in ('claims', 'assessments'):
        for action in ('UPDATE', 'DELETE'):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT,'{table}_are_append_only'); END")


def migrate_v8(con):
    install(con)
    con.execute('UPDATE agency_meta SET version=8')


def decode_claim(row) -> dict:
    return {**dict(row), 'value': json.loads(row['value'])}


PARCEL_BOUND = ('tkgm.lookup', 'sit.says', 'tkgm.corridor', 'tkgm.neighbors')


def with_parcels(con, rows, listing_ref, mode):
    """Marks each claim drawn from a parcel-bound record with the parcel it is about (`parcel`): the looked-up parcel for a TKGM record;
    for a sit overlay, corridor or neighbour check the parcel it names, else the main parcel looked up last before it. A record about
    another parcel (a wrong number in the listing, corrected by the user) then cannot speak for this sale (facts.assemble)."""
    marks = ','.join('?' * len(PARCEL_BOUND))
    obs = {r['id']: (r['tool'], r['captured_at'], json.loads(r['payload'])) for r in con.execute(
        f'SELECT id, tool, captured_at, payload FROM observations WHERE listing_ref=? AND mode=? AND tool IN ({marks})', (listing_ref, mode, *PARCEL_BOUND))}
    mains = sorted((at, p.get('parcel_key')) for tool, at, p in obs.values() if tool == 'tkgm.lookup' and p.get('found') and not p.get('part'))
    for c in rows:
        found = obs.get(c.get('observation_id'))
        if not found:
            continue
        tool, at, payload = found
        named = payload.get('parcel_key') if isinstance(payload, dict) else None
        if tool == 'tkgm.lookup' or named:
            c['parcel'] = named
        else:
            before = [parcel for t, parcel in mains if t <= at]
            c['parcel'] = before[-1] if before else None
    return rows


class FactService:
    # ---- claims ---------------------------------------------------------------------------
    @staticmethod
    def observation(con, observation_id):
        row = con.execute('SELECT id,mode,tool,listing_ref,payload FROM observations WHERE id=?', (observation_id,)).fetchone()
        return None if row is None else {**dict(row), 'payload': json.loads(row['payload'])}

    def _insert(self, con, p: Proposal, verdict: Verdict, observation: dict | None, now: float) -> dict:
        kind = source_kind(observation['tool'] if observation else None, p.proposer)
        identity = {'proposer': p.proposer, 'mode': p.mode, 'observation': p.observation_id, 'listing': p.listing_ref,
                    'field': p.field, 'value': p.value, 'source_ref': p.source_ref, 'quote': p.quote,
                    'key': p.key, 'subject': p.subject, 'resolves': p.resolves, 'basis': p.basis}
        if p.proposer == 'user' or p.resolves:
            identity['at'] = now            # every user decision is an event; A, B, A must all count
        if p.proposer.startswith('llm:'):
            identity['verifier'] = VERIFIER  # when the acceptance rules change, a model's proposal is checked again, not answered from the old verdict
        claim_id = uuid4().hex
        source_ref = p.source_ref if observation or not p.observation_id else f'missing_observation:{p.observation_id}'
        cursor = con.execute('''INSERT OR IGNORE INTO claims(id,listing_ref,mode,field,value,scope,source_kind,key,observation_id,
            source_ref,quote,subject,proposer,status,grade,reason,resolves,verifier,created_at,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (claim_id, p.listing_ref, p.mode, p.field, canonical(p.value), 'parcel' if kind in ('official', 'calc') else 'listing',
             kind, p.key, observation['id'] if observation else None, source_ref,
             p.quote[:500] if p.quote else None, p.subject, p.proposer, verdict.status, verdict.grade,
             verdict.reason, p.resolves, VERIFIER, now, digest(identity)))
        if cursor.rowcount == 0:
            old = con.execute('SELECT id,status,reason,grade FROM claims WHERE dedupe_key=?', (digest(identity),)).fetchone()
            if old is None:
                raise RuntimeError('claim_not_recorded')     # OR IGNORE hid a constraint failure
            return {'claim_id': old['id'], 'status': old['status'], 'reason': old['reason'], 'grade': old['grade'], 'duplicate': True}
        return {'claim_id': claim_id, 'status': verdict.status, 'reason': verdict.reason, 'grade': verdict.grade, 'duplicate': False}

    def submit(self, con, p: Proposal, *, now: float) -> dict:
        """Verify one proposal and log it, accepted or not. Re-submitting is harmless."""
        observation = self.observation(con, p.observation_id) if p.observation_id else None
        return self._insert(con, p, verify(p, observation), observation, now)

    def decide(self, con, listing_ref, mode, field, value, note, now, *, assess=True) -> dict:
        """A user's own fact (e.g. destination verified, sale scope resolved)."""
        result = self.submit(con, Proposal(listing_ref=listing_ref, field=field, value=value, proposer='user',
                                           mode=mode, quote=(note or None)), now=now)
        if assess and result['status'] == 'accepted':
            result['assessment'] = self.assess(con, listing_ref, mode, now)
        return result

    def resolve_review(self, con, claim_id, *, accepted: bool, note: str, now: float) -> dict:
        """Answer a needs_review item. Accepting keeps the source's own grade: the user
        confirmed what was said, which does not turn a statement into an official record."""
        row = con.execute("SELECT * FROM claims WHERE id=? AND status='needs_review'", (claim_id,)).fetchone()
        if row is None or con.execute('SELECT 1 FROM claims WHERE resolves=?', (claim_id,)).fetchone():
            raise ValueError('Only an open review item can be resolved')
        old = decode_claim(row)
        observation = self.observation(con, old['observation_id']) if old['observation_id'] else None
        p = Proposal(listing_ref=old['listing_ref'], field=old['field'], value=old['value'], proposer='user',
                     mode=old['mode'], observation_id=old['observation_id'], source_ref=old['source_ref'],
                     quote=old['quote'] or note, subject=old['subject'], key=old['key'], resolves=claim_id)
        verdict = (accept(TOOL_GRADE[observation['tool']] if observation else 'kullanici', 'user_confirmed')
                   if accepted else reject('user_rejected'))
        result = self._insert(con, p, verdict, observation, now)
        if accepted:
            result['assessment'] = self.assess(con, old['listing_ref'], old['mode'], now)
        return result

    # ---- extraction hook ------------------------------------------------------------------
    def on_observation(self, con, task, tool, observation_id, now, event) -> None:
        """Runs in the transaction that records the observation. A failure here never
        loses the observation: the savepoint is rolled back and an event says why."""
        if tool.key not in EXTRACTORS:
            return
        observation = self.observation(con, observation_id)
        con.execute('SAVEPOINT facts_extract')
        try:
            area = con.execute("""SELECT value FROM claims WHERE listing_ref=? AND mode=? AND field='sale_area_m2'
                AND status='accepted' AND grade!='resmi' ORDER BY seq DESC LIMIT 1""", (observation['listing_ref'], task['mode'])).fetchone()
            proposals = extract(tool.key, observation_id, task['mode'], observation['payload'], observation['listing_ref'],
                                json.loads(area['value']) if area else None)
            counts = {'accepted': 0, 'rejected': 0, 'needs_review': 0, 'duplicate': 0}
            touched = set()
            for p in proposals:
                result = self.submit(con, p, now=now)
                counts['duplicate' if result['duplicate'] else result['status']] += 1
                if result['status'] == 'accepted' and not result['duplicate']:
                    touched.add(p.listing_ref)
            assessed = []
            for listing in sorted(touched):
                outcome = self.assess(con, listing, task['mode'], now)
                if outcome['changed']:
                    assessed.append({'listing_id': listing, 'state': outcome['state'], 'previous': outcome['previous_state']})
            con.execute('RELEASE facts_extract')
        except Exception as error:
            con.execute('ROLLBACK TO facts_extract')
            con.execute('RELEASE facts_extract')
            event(con, task, 'facts_failed', {'observation_id': observation_id, 'error': type(error).__name__}, now)
            return
        if proposals:
            event(con, task, 'facts_recorded', {'observation_id': observation_id, **counts, 'assessments': assessed[:50]}, now)
        about = observation['payload'].get('listing_id') if isinstance(observation['payload'], dict) else None
        if tool.key == LISTING and task['listing_ref'] and about != task['listing_ref']:
            # Facts follow the content, never the label; the mismatch is made visible.
            event(con, task, 'facts_listing_mismatch', {'task_listing_ref': task['listing_ref'], 'payload_listing_id': about}, now)

    def backfill(self, con, mode, now) -> dict:
        """Run the extractors over observations recorded before the facts layer existed.
        Idempotent: a second run only reports duplicates."""
        counts = {'observations': 0, 'accepted': 0, 'rejected': 0, 'needs_review': 0, 'duplicate': 0}
        touched = set()
        marks = ','.join('?' for _ in EXTRACTORS)
        for row in con.execute(f'SELECT id,tool,listing_ref,payload FROM observations WHERE mode=? AND tool IN ({marks}) ORDER BY captured_at,id',
                               (mode, *sorted(EXTRACTORS))).fetchall():
            counts['observations'] += 1
            area = con.execute("""SELECT value FROM claims WHERE listing_ref=? AND mode=? AND field='sale_area_m2'
                AND status='accepted' AND grade!='resmi' ORDER BY seq DESC LIMIT 1""", (row['listing_ref'], mode)).fetchone()
            for p in extract(row['tool'], row['id'], mode, json.loads(row['payload']), row['listing_ref'], json.loads(area['value']) if area else None):
                result = self.submit(con, p, now=now)
                counts['duplicate' if result['duplicate'] else result['status']] += 1
                if result['status'] == 'accepted' and not result['duplicate']:
                    touched.add(p.listing_ref)
        counts['assessed'] = sum(self.assess(con, listing, mode, now)['changed'] for listing in sorted(touched))
        return counts

    # ---- assembly and assessment ----------------------------------------------------------
    @staticmethod
    def claims(con, listing_ref, mode, status='accepted') -> list[dict]:
        rows = [decode_claim(r) for r in con.execute(
            'SELECT * FROM claims WHERE listing_ref=? AND mode=? AND status=? ORDER BY seq', (listing_ref, mode, status))]
        return with_parcels(con, rows, listing_ref, mode)

    @staticmethod
    def screening(assembled) -> ScreeningFacts:
        v = assembled.values
        data = {name: float(v[name]) for name in ('price_tl', 'sale_area_m2') if name in v}
        data.update({name: v[name] for name in ('sale_scope_resolved', 'shared_title', 'archaeological_sit',
                                                'natural_sit', 'access', 'destination_verified', 'user_excluded') if name in v})
        if 'route_minutes' in v:
            data['route_minutes'] = [float(x) for x in v['route_minutes']]
        refs = {name: ['claim:' + c['id'] for c in rows] for name, rows in assembled.chosen.items() if name != 'parcel_key'}
        if 'sale_area_m2' in assembled.values and 'sale_area_m2' not in assembled.chosen:          # a sum of parcel areas cites its parts
            refs['sale_area_m2'] = ['claim:' + c['id'] for c in assembled.chosen.get('parcel_area_m2', [])]
        return ScreeningFacts(**data, conflicts=list(assembled.conflicts), evidence_refs=refs)

    def assess(self, con, listing_ref, mode, now, policy: Policy | None = None) -> dict:
        """Evaluate current facts and log the result if (and only if) it changed."""
        policy = policy or Policy()
        claims = self.claims(con, listing_ref, mode)
        if not claims:
            return {'changed': False, 'state': None, 'previous_state': None}
        assembled = assemble(claims, policy)
        facts = self.screening(assembled)
        decision = evaluate(facts, policy)
        basis = assembled.basis()
        # Claim ids are provenance, not input: a re-observed identical value is not a change.
        fingerprint = digest({'policy': policy.version, 'basis': basis,
                              'facts': facts.model_dump(mode='json', exclude={'evidence_refs'})})
        latest = con.execute('SELECT id,state,input_fingerprint FROM assessments WHERE listing_ref=? AND mode=? ORDER BY seq DESC LIMIT 1',
                             (listing_ref, mode)).fetchone()
        if latest and latest['input_fingerprint'] == fingerprint:
            return {'changed': False, 'id': latest['id'], 'state': latest['state'], 'previous_state': latest['state']}
        assessment_id = uuid4().hex
        con.execute('''INSERT INTO assessments(id,listing_ref,mode,policy_version,input_fingerprint,facts,basis,conflicts,
            state,reasons,conditions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (assessment_id, listing_ref, mode, policy.version, fingerprint, canonical(facts.model_dump(mode='json')),
             canonical(basis), canonical(assembled.conflicts), decision.state, canonical(decision.reasons),
             canonical(decision.conditions), now))
        return {'changed': True, 'id': assessment_id, 'state': decision.state,
                'previous_state': latest['state'] if latest else None}

    # ---- read model -----------------------------------------------------------------------
    @staticmethod
    def seen(con, listing_ref, mode) -> dict:
        tools = {r['tool'] for r in con.execute('SELECT tool FROM observations WHERE listing_ref=? AND mode=?', (listing_ref, mode))}
        tools |= {r['tool'] for r in con.execute('''SELECT DISTINCT o.tool FROM claims c JOIN observations o ON o.id=c.observation_id
            WHERE c.listing_ref=? AND c.mode=?''', (listing_ref, mode))}
        removed = con.execute("SELECT 1 FROM observations WHERE listing_ref=? AND mode=? AND tool=? AND payload LIKE '%\"unavailable\":true%' LIMIT 1",
                              (listing_ref, mode, LISTING)).fetchone() is not None
        enclosed = False
        if 'tkgm.corridor' in tools:
            row = con.execute("SELECT payload FROM observations WHERE listing_ref=? AND mode=? AND tool='tkgm.corridor' ORDER BY captured_at DESC, id DESC LIMIT 1", (listing_ref, mode)).fetchone()
            try:
                enclosed = json.loads(row['payload']).get('verdict') == 'all_edges_numbered_neighbors'
            except (TypeError, ValueError):
                enclosed = False
        return {'listing_read': LISTING in tools, 'thread_read': THREAD in tools, 'in_search': SEARCH in tools, 'listing_removed': removed,
                'tkgm_looked_up': 'tkgm.lookup' in tools, 'corridor_checked': 'tkgm.corridor' in tools, 'neighbors_checked': 'tkgm.neighbors' in tools, 'says_checked': 'sit.says' in tools,
                'corridor_enclosed': enclosed}      # every edge borders a numbered parcel: no cadastral road frontage (an easement may still exist)

    @staticmethod
    def review_items(con, listing_ref, mode) -> list[dict]:
        # An item ends when it is answered, or when the same field later got an accepted value (the same answer, now verified).
        rows = con.execute('''SELECT * FROM claims c WHERE listing_ref=? AND mode=? AND status='needs_review'
            AND NOT EXISTS (SELECT 1 FROM claims r WHERE r.resolves=c.id)
            AND NOT EXISTS (SELECT 1 FROM claims a WHERE a.listing_ref=c.listing_ref AND a.mode=c.mode AND a.field=c.field AND a.status='accepted' AND a.seq>c.seq)
            ORDER BY seq''', (listing_ref, mode)).fetchall()
        return [decode_claim(r) for r in rows]

    def status(self, con, listing_ref, mode, *, legacy_lifecycle=None, policy: Policy | None = None) -> dict:
        policy = policy or Policy()
        claims = self.claims(con, listing_ref, mode)
        assembled = assemble(claims, policy)
        decision = evaluate(self.screening(assembled), policy) if claims else None
        if legacy_lifecycle == 'excluded':
            # The old system's exclusion stands until someone reopens the listing: whatever the new records say, it is not a candidate.
            decision = Decision(state='excluded', policy_version=policy.version, reasons=['legacy_excluded'] + (decision.reasons if decision and decision.state == 'excluded' else []),
                                conditions=[], penalties={})
        seen = self.seen(con, listing_ref, mode)
        if len(assembled.parcels) > 1:
            seen['corridor_enclosed'] = False   # the main parcel may reach the road through another deed of the same sale: not conclusive
        review = self.review_items(con, listing_ref, mode)
        persisted = con.execute('SELECT id,state,created_at,policy_version FROM assessments WHERE listing_ref=? AND mode=? ORDER BY seq DESC LIMIT 1',
                                (listing_ref, mode)).fetchone()
        rejected = con.execute("SELECT id,field,reason,proposer FROM claims WHERE listing_ref=? AND mode=? AND status='rejected' ORDER BY seq DESC LIMIT 5",
                               (listing_ref, mode)).fetchall()

        def brief(c):
            return {'claim_id': c['id'], 'value': c['value'], 'grade': c['grade'], 'proposer': c['proposer'],
                    'source_kind': c['source_kind'], 'source_ref': c['source_ref'], 'quote': (c['quote'] or '')[:300] or None}
        basis = assembled.basis()
        official = con.execute("SELECT payload FROM observations WHERE tool='tkgm.lookup' AND mode=? AND listing_ref=? ORDER BY captured_at DESC LIMIT 1",
                               (mode, listing_ref)).fetchone()
        official = json.loads(official['payload']) if official else None
        return {
            'links': {'sahibinden': f'https://www.sahibinden.com/{listing_ref}', 'tkgm': (official or {}).get('parcel_url')},
            'official_parcel': None if official is None else (
                {'found': True, 'type': official.get('property_type'), 'area_m2': official.get('official_area_m2'),
                 'neighborhood': official.get('neighborhood'), 'status': official.get('raw_status')} if official.get('found') else {'found': False}),
            'listing_id': listing_ref, 'mode': mode, 'parcels': assembled.parcels,
            'decision': decision.model_dump() if decision else None,
            'facts': {f: (brief(rows[0]) if len(rows) == 1 else [brief(r) for r in rows]) for f, rows in assembled.chosen.items()}
                     | ({'sale_area_m2': {'claim_id': None, 'value': assembled.values['sale_area_m2'], 'grade': 'resmi', 'proposer': 'code:parcel_sum',
                                          'source_kind': 'official', 'source_ref': ' + '.join(assembled.parcels), 'quote': None,
                                          'parts': [brief(r) for r in assembled.chosen['sale_area_m2']]}}
                        if len(assembled.chosen.get('sale_area_m2') or []) > 1 else {}),     # a several-deed sale: one total, its parts beside it
            'leads': {f: [brief(c) for c in cs[:5]] for f, cs in assembled.leads.items()},
            'conflicts': assembled.conflicts,
            'contradictions': assembled.contradictions[:10],
            'listing_errors': assembled.errors[:10],
            'review': [{'claim_id': c['id'], 'field': c['field'], 'value': c['value'], 'reason': c['reason'],
                        'quote': (c['quote'] or '')[:300] or None, 'proposer': c['proposer']} for c in review[:10]],
            'rejected_recent': [dict(r) for r in rejected],
            'seen': seen,
            'legacy_lifecycle': legacy_lifecycle,
            'persisted_assessment': None if persisted is None else {**dict(persisted), 'current': (
                decision is not None and persisted['state'] == decision.state)},
            'next_action': next_action(decision, assembled if claims else None, seen, len(review), legacy_lifecycle),
            'to_verify_officially': [f for f in OFFICIALLY_CHECKABLE if basis.get(f) in UNVERIFIED_GRADES
                                    or (f == 'access' and assembled.values.get('access') == 'corridor_conditional')],   # a corridor still needs a person's look
            'note': 'Beyan/rapor/kullanici dereceli alanlar resmî doğrulama değildir; alma aşamasında doğrulanacaklar listesi.',
        }
