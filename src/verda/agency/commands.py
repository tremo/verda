"""Local entry points. Starting the API does not silently start a model worker."""
import json
import time
import sqlite3
from pathlib import Path
from uuid import uuid4

from verda.agency.engine import AgentEngine, default_registry
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore


def add_commands(parser, commands):
    parser.add_argument('--agency-db', type=Path, default=Path('.local/agency.sqlite'))
    parser.add_argument('--agency-config', type=Path)
    parser.add_argument('--supervisor-agent', default='manager', help='Supervisor agent key; empty string disables root result routing')
    commands.add_parser('agency-init')
    commands.add_parser('agency-status')
    status = commands.add_parser('agency-facts-status')
    status.add_argument('listing_id')
    status.add_argument('--synthetic', action='store_true')
    backfill = commands.add_parser('agency-facts-backfill')
    backfill.add_argument('--synthetic', action='store_true')
    propose = commands.add_parser('agency-facts-propose')
    propose.add_argument('listing_id'); propose.add_argument('observation_id'); propose.add_argument('field')
    propose.add_argument('value', help='JSON value')
    propose.add_argument('--source-ref'); propose.add_argument('--quote')
    propose.add_argument('--subject', default='this_parcel', choices=['this_parcel', 'neighbor', 'unclear'])
    propose.add_argument('--proposer', default='llm:manual', help='must start with llm:')
    decide = commands.add_parser('agency-facts-decide')
    decide.add_argument('listing_id')
    decide.add_argument('field')
    decide.add_argument('value', help='JSON value, e.g. true, 8000000, "none"')
    decide.add_argument('--note', required=True)
    resolve_review = commands.add_parser('agency-facts-resolve')
    resolve_review.add_argument('claim_id')
    resolve_review.add_argument('--note', required=True)
    verdict = resolve_review.add_mutually_exclusive_group(required=True)
    verdict.add_argument('--accept', action='store_true')
    verdict.add_argument('--reject', action='store_true')
    browser = commands.add_parser('agency-browser')
    browser.add_argument('action', choices=['attach','detach','claim','finish','status','select'])
    browser.add_argument('--driver', choices=['codex','jev'], default='codex')
    browser.add_argument('--worker', default='codex-chrome')
    browser.add_argument('--file', type=Path)
    jev = commands.add_parser('agency-jev-worker')
    jev.add_argument('--env-file', type=Path)
    jev.add_argument('--continuous', action='store_true')
    jev.add_argument('--no-launch', action='store_true', help='Do not open Chrome or attach; only use an already connected browser')
    jev.add_argument('--visible', action='store_true', help='Plain foreground tab in your own Chrome; waits for a human to pass a security check')
    subscription=commands.add_parser('agency-subscription')
    subscription.add_argument('file',type=Path)
    resolve=commands.add_parser('agency-source-resume')
    resolve.add_argument('incident_id')
    resolve.add_argument('--note',required=True)
    publish = commands.add_parser('agency-publish-firestore', help="Publish v2's listing overlay to the private Firestore the dashboard reads")
    publish.add_argument('--dry-run', action='store_true', help='Only count what would be written')
    publish.add_argument('--every', type=int, default=0, help='Repeat every N seconds (a service); 0 runs once')
    retry = commands.add_parser('agency-records-retry')
    retry.add_argument('delivery_id')
    demo = commands.add_parser('agency-demo')
    demo.add_argument('--request-key', default=None)
    demo.add_argument('--max-steps', type=int, default=12)
    submit = commands.add_parser('agency-submit')
    submit.add_argument('agent')
    submit.add_argument('objective')
    submit.add_argument('--inputs', type=Path)
    submit.add_argument('--listing-ref')
    submit.add_argument('--tool', help='Execute this granted tool directly, without model calls')
    submit.add_argument('--priority', type=int, default=50)
    submit.add_argument('--request-key', required=True)
    worker = commands.add_parser('agency-worker')
    worker.add_argument('--mode', choices=['local'], default='local')
    worker.add_argument('--max-steps', type=int, default=1)
    worker.add_argument('--continuous', action='store_true')
    scan = commands.add_parser('agency-scan')
    scan.add_argument('--mode', choices=['daily', 'full'], default='daily')
    scan.add_argument('--env-file', type=Path)
    scan.add_argument('--no-details', action='store_true', help='Walk the result pages only; read no listing detail')
    sync = commands.add_parser('agency-inbox-sync')
    sync.add_argument('--env-file', type=Path)
    trigger = commands.add_parser('agency-trigger')
    trigger.add_argument('file', type=Path)
    event = commands.add_parser('agency-event')
    event.add_argument('event_type')
    event.add_argument('file', type=Path)
    event.add_argument('--request-key', required=True)


FEEDBACK_EVERY_ROUNDS = 3          # the dashboard's corrections are read every third round (15 min): one read per feedback document each time


def run(args):
    config = AgencyConfig.load(args.agency_config)
    store = AgencyStore(args.agency_db, config, supervisor_agent=args.supervisor_agent or None)
    store.initialize()
    if args.database.startswith('sqlite:///'):
        store.archive_path = Path(args.database[len('sqlite:///'):])
    command = args.command
    if command == 'agency-inbox-sync':
        from verda.agency.inbox import run_sync
        from verda.agency.jev import run_worker
        sync_engine = AgentEngine(store, default_registry(config, store))
        def sync_step():
            store.tick()
            sync_engine.step(mode='local')
            run_worker(store, env_file=args.env_file, visible=True, launch=False)
        try:
            return run_sync(store, step=sync_step)
        finally:
            sync_engine.close()
    if command == 'agency-scan':
        from verda.agency.discovery import run_scan
        from verda.agency.jev import run_worker
        scan_engine = AgentEngine(store, default_registry(config, store))
        def step():
            store.tick()
            scan_engine.step(mode='local')
            run_worker(store, env_file=args.env_file, visible=True, launch=False)
        try:
            return run_scan(store, mode=args.mode, step=step, read_details=not args.no_details)
        finally:
            scan_engine.close()
    if command == 'agency-jev-worker':
        from verda.agency.jev import run_worker
        return run_worker(store, env_file=args.env_file, continuous=args.continuous, visible=args.visible, launch=not args.no_launch)
    if command == 'agency-browser':
        from verda.agency.browser import BrowserBridge
        bridge = BrowserBridge(store)
        if args.action == 'attach':
            if args.driver != 'codex':
                raise ValueError('Jev bağlantısını agency-jev-worker doğrular; elle bağlı işaretlenemez.')
            bridge.attach(args.worker)
        elif args.action == 'select':
            bridge.select(args.driver)
        elif args.action == 'detach':
            bridge.detach(args.worker)
        elif args.action == 'claim':
            return bridge.claim(args.worker)
        elif args.action == 'finish':
            if not args.file:
                raise ValueError('Receipt file required')
            bridge.finish(worker=args.worker, **json.loads(args.file.read_text()))
        return bridge.overview()
    if command == 'agency-init':
        return {'initialized': True, 'agents': len(config.agents), 'tools': len(config.tools)}
    if command == 'agency-subscription':
        store.register_subscription(**json.loads(args.file.read_text()))
        return {'registered':True}
    if command == 'agency-publish-firestore':
        import time as _time
        from verda.publish.firestore import Firestore
        from verda.publish.sync import sync
        from verda.publish.sync import record_status
        firestore = Firestore()
        rounds, backoff = 0, 0
        while True:
            try:
                result = sync(store, firestore, dry_run=args.dry_run)
                if not args.dry_run and rounds % FEEDBACK_EVERY_ROUNDS == 0:
                    from verda.publish.feedback import import_feedback
                    result.update(import_feedback(store, firestore))               # the owner's corrections on the dashboard flow back as user facts
                backoff = 0
            except Exception as error:                      # one failed round never stops the service; it is retried later
                result = {'error': type(error).__name__ + ':' + str(error)[:80]}
                # 429 is Firestore saying "too much" (the daily free quota): slow down, doubling the wait up to an hour, never hammer it.
                backoff = min(3600, max(args.every, backoff * 2)) if 'firestore_http_429' in result['error'] else 0
            rounds += 1
            if not args.every:
                return result
            if not args.dry_run:
                record_status(store, result, wait=backoff or args.every)
            print(json.dumps({**result, **({'backoff_seconds': backoff} if backoff else {})}, ensure_ascii=False), flush=True)
            _time.sleep(backoff or args.every)
    if command == 'agency-source-resume':
        store.resolve_source(args.incident_id,args.note)
        return {'resolved':args.incident_id}
    if command == 'agency-status':
        return store.overview()
    if command == 'agency-facts-status':
        from verda.agency.research import fact_status
        return fact_status(store, args.listing_id, mode='synthetic' if args.synthetic else 'local')
    if command == 'agency-facts-backfill':
        with store.transaction() as con:
            return store.facts.backfill(con, 'synthetic' if args.synthetic else 'local', time.time())
    if command == 'agency-facts-propose':
        from verda.agency.research import fact_propose
        if not args.proposer.startswith('llm:'):
            raise ValueError('Proposer must start with llm:')
        return fact_propose(store, {'listing_id': args.listing_id, 'observation_id': args.observation_id, 'field': args.field,
                                    'value': json.loads(args.value), 'source_ref': args.source_ref, 'quote': args.quote,
                                    'subject': args.subject}, mode='local', task_id='', proposer=args.proposer)
    if command == 'agency-facts-decide':
        with store.transaction() as con:
            return store.facts.decide(con, args.listing_id, 'local', args.field, json.loads(args.value), args.note, time.time())
    if command == 'agency-facts-resolve':
        with store.transaction() as con:
            return store.facts.resolve_review(con, args.claim_id, accepted=args.accept, note=args.note, now=time.time())
    if command == 'agency-records-retry':
        store.retry_record_delivery(args.delivery_id)
        return {'delivery_id': args.delivery_id, 'state': 'pending'}
    if command == 'agency-submit':
        return {'task_id': store.submit(agent=args.agent, objective=args.objective,
                inputs=json.loads(args.inputs.read_text()) if args.inputs else {}, request_key=args.request_key,
                priority=args.priority, listing_ref=args.listing_ref, executor_tool=args.tool)}
    if command == 'agency-trigger':
        from datetime import datetime
        registered = []
        loaded = json.loads(args.file.read_text())
        for data in (loaded if isinstance(loaded, list) else [loaded]):
            if isinstance(data.get('next_at'), str):      # an ISO time with its offset, e.g. 2026-10-09T09:00:00+03:00
                data = {**data, 'next_at': datetime.fromisoformat(data['next_at']).timestamp()}
            store.register_trigger(**data)
            registered.append(data['key'])
        return {'registered': registered}
    if command == 'agency-event':
        return {'tasks': store.publish(args.request_key, args.event_type, json.loads(args.file.read_text()))}
    if command == 'agency-demo':
        from verda.agency.demo import synthetic_registry
        registry = synthetic_registry(config)
        store = AgencyStore(args.agency_db, registry.config, supervisor_agent=args.supervisor_agent or None)
        registry.context_handlers.update(default_registry(registry.config, store).context_handlers)
        task = store.submit(agent='manager', objective='Sahibinden operatörüne DEMO-101 ilanının ayrıntısını okuma görevi ver. Dönen fiyat ve alanı özetle ve tamamla. Başka araştırma yapma.',
            inputs={'synthetic': True, 'listing_id': 'DEMO-101'}, listing_ref='DEMO-101',
            request_key=args.request_key or 'agency-demo:' + uuid4().hex, mode='synthetic')
        engine = AgentEngine(store, registry)
        try:
            for _ in range(args.max_steps):
                if not engine.step(mode='synthetic'):
                    break
        finally:
            engine.close()
        result = store.overview()
        root = next(t for t in result['tasks'] if t['id'] == task)
        return {'task_id': task, 'state': root['state'], 'trace_id': root['trace_id'],
                'tasks': sum(t['trace_id'] == root['trace_id'] for t in result['tasks']), 'mode': 'synthetic',
                'provider': registry.agents['manager'].model.provider}
    engine = AgentEngine(store, default_registry(config, store))
    steps = 0
    try:
        while args.continuous or steps < args.max_steps:
            try:
                store.tick()
                from verda.agency.backup import maybe_backup
                maybe_backup(store)
            except sqlite3.Error:
                from verda.agency.health import worker_health
                engine.storage_error='SQLiteError'
                worker_health(store,engine.worker_id,'local','storage_error','SQLiteError')
                worked=False
            else:worked = engine.step(mode='local')
            steps += 1
            if not worked:
                if not args.continuous:
                    break
                time.sleep(5)
    finally:
        engine.close()
    return {'worker_iterations': steps, 'storage_error':engine.storage_error}
