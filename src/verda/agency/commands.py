"""Local entry points. Starting the API does not silently start a model worker."""
import json
import time
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
    command = args.command
    if command == 'agency-init':
        return {'initialized': True, 'agents': len(config.agents), 'tools': len(config.tools)}
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
    engine = AgentEngine(store, default_registry(config))
    steps = 0
    try:
        while args.continuous or steps < args.max_steps:
            store.tick()
            worked = engine.step(mode='local')
            steps += 1
            if not worked:
                if not args.continuous:
                    break
                time.sleep(1)
    finally:
        engine.close()
    return {'worker_iterations': steps}
