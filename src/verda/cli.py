from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets

from sqlalchemy.orm import Session

from verda.dashboard import store_projection
from verda.legacy import import_snapshot
from verda.storage import DashboardSnapshot, connect, initialize
from verda.workflow import WorkflowStore, research_plan


def main():
    parser = argparse.ArgumentParser(description="Verda v2 local shadow backend")
    parser.add_argument("--database", default="sqlite:///.local/verda.sqlite",
                        help="SQLAlchemy URL (default: local development database)")
    parser.add_argument("--workflow-db", type=Path, default=Path(".local/workflows.sqlite"))
    parser.add_argument("--runtime-config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("runtime-info")
    commands.add_parser("manager-demo")
    demo = commands.add_parser("demo-run")
    demo.add_argument("--request-key", default="synthetic-demo-v1")
    workflow_step = commands.add_parser("workflow-step")
    workflow_step.add_argument("--mode", choices=["demo", "shadow"], required=True)
    workflow_step.add_argument("--worker-id", default="local-worker")
    for name in ("workflow-status", "workflow-cancel"):
        commands.add_parser(name).add_argument("run_id")
    importer = commands.add_parser("import-legacy")
    importer.add_argument("source", type=Path)
    dashboard = commands.add_parser("import-dashboard")
    dashboard.add_argument("source", type=Path)
    exporter = commands.add_parser("export-dashboard")
    exporter.add_argument("projection_id")
    exporter.add_argument("output", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token-file", type=Path, default=Path(".local/api-token"))
    args = parser.parse_args()
    if args.command in {"runtime-info", "manager-demo"}:
        from verda.runtime import Runtime, RuntimeConfig, manager_preview
        runtime = Runtime(RuntimeConfig.load(args.runtime_config))
        if args.command == "runtime-info":
            result = runtime.describe()
        else:
            result = manager_preview(runtime,
                {"label": "Sentetik bağlantı testi", "listing_read": True,
                 "parcel_identity": "missing", "note": "Gerçek ilan veya yazışma içermez."},
                ["resolve_parcel_identity", "ask_user_for_missing_identity"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    workflow_store = WorkflowStore(args.workflow_db)
    if args.command in {"demo-run", "workflow-step", "workflow-status", "workflow-cancel"}:
        from verda.worker import step
        if args.command == "demo-run":
            workflow_store.initialize()
            run_id = workflow_store.create_run(research_plan("synthetic-demo", 1, "demo"), args.request_key)
            for _ in range(100):
                if not step(workflow_store, "demo-worker"):
                    break
            result = workflow_store.view(run_id)
        elif args.command == "workflow-step":
            result = {"worked": step(workflow_store, args.worker_id, mode=args.mode), "mode": args.mode}
        elif args.command == "workflow-cancel":
            result = {"run_id": args.run_id, "cancelled": workflow_store.cancel(args.run_id)}
        else:
            result = workflow_store.view(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    engine = connect(args.database)
    try:
        if args.command == "init":
            initialize(engine)
            workflow_store.initialize()
            result = {"initialized": True, "schema_version": 1, "workflow_schema_version": 1, "mode": "shadow"}
        elif args.command == "import-legacy":
            result = import_snapshot(args.source, engine)
        elif args.command == "import-dashboard":
            result = store_projection(args.source, engine)
        elif args.command == "export-dashboard":
            with Session(engine) as session:
                row = session.get(DashboardSnapshot, args.projection_id)
                if not row:
                    raise ValueError("Dashboard projection not found")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                # O_EXCL prevents accidental replacement of a current publication.
                fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(row.payload, output, ensure_ascii=False, indent=2, allow_nan=False)
                result = {"exported": True, "projection_id": row.id, "records": len(row.payload["records"])}
        else:
            from verda.api import create_app
            import uvicorn
            args.token_file.parent.mkdir(parents=True, exist_ok=True)
            if not args.token_file.exists():
                fd = os.open(args.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as token_file:
                    token_file.write(secrets.token_urlsafe(48))
            token = args.token_file.read_text().strip()
            args.token_file.chmod(0o600)
            if not workflow_store.path.exists():
                raise ValueError("Run verda init before serving workflows")
            from verda.runtime import Runtime, RuntimeConfig
            runtime = Runtime(RuntimeConfig.load(args.runtime_config))
            uvicorn.run(create_app(engine, token, workflow_store, runtime), host="127.0.0.1", port=args.port,
                        access_log=False, log_level="warning")
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
