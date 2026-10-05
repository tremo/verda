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


def main():
    parser = argparse.ArgumentParser(description="Verda v2 local shadow backend")
    parser.add_argument("--database", default="sqlite:///.local/verda.sqlite",
                        help="SQLAlchemy URL (default: local development database)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
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
    engine = connect(args.database)
    try:
        if args.command == "init":
            initialize(engine)
            result = {"initialized": True, "schema_version": 1, "mode": "shadow"}
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
            uvicorn.run(create_app(engine, token), host="127.0.0.1", port=args.port,
                        access_log=False, log_level="warning")
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
