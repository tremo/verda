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
    browser = commands.add_parser('agency-browser')
    browser.add_argument('action', choices=['attach','detach','claim','finish','status'])
    browser.add_argument('--worker', default='codex-chrome')
    browser.add_argument('--file', type=Path)
    subscription=commands.add_parser('agency-subscription')
    subscription.add_argument('file',type=Path)
    resolve=commands.add_parser('agency-source-resume')
    resolve.add_argument('incident_id')
    resolve.add_argument('--note',required=True)
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
    trigger = commands.add_parser('agency-trigger')
    trigger.add_argument('file', type=Path)
    event = commands.add_parser('agency-event')
    event.add_argument('event_type')
    event.add_argument('file', type=Path)
    event.add_argument('--request-key', required=True)


def run(args):
    config = AgencyConfig.load(args.agency_config)
    store = AgencyStore(args.agency_db, config, supervisor_agent=args.supervisor_agent or None)
    store.initialize()
    if args.database.startswith('sqlite:///'):
        store.archive_path = Path(args.database[len('sqlite:///'):])
    command = args.command
    if command == 'agency-browser':
        from verda.agency.browser import BrowserBridge
        bridge = BrowserBridge(store)
        if args.action == 'attach':
            bridge.attach(args.worker)
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
    if command == 'agency-source-resume':
        store.resolve_source(args.incident_id,args.note)
        return {'resolved':args.incident_id}
    if command == 'agency-status':
        return store.overview()
    if command == 'agency-records-retry':
        store.retry_record_delivery(args.delivery_id)
        return {'delivery_id': args.delivery_id, 'state': 'pending'}
    if command == 'agency-submit':
        return {'task_id': store.submit(agent=args.agent, objective=args.objective,
                inputs=json.loads(args.inputs.read_text()) if args.inputs else {}, request_key=args.request_key,
                priority=args.priority, listing_ref=args.listing_ref, executor_tool=args.tool)}
    if command == 'agency-trigger':
        data = json.loads(args.file.read_text())
        store.register_trigger(**data)
        return {'registered': data['key']}
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
            try:store.tick()
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
