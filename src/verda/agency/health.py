"""Storage health is inspected outside the work queue it supervises."""
import json
from pathlib import Path
import sqlite3
import time


def worker_health(store, worker_id, mode, state, error=None, now=None):
    folder=store.path.parent/(store.path.name+'.health')
    try:
        folder.mkdir(mode=0o700,exist_ok=True)
        path=folder/(worker_id+'.json');tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'id':worker_id,'mode':mode,'state':state,'error':error,
                                  'seen_at':time.time() if now is None else now}))
        tmp.chmod(0o600);tmp.replace(path)
    except OSError:
        # Failure reporting must never make an external operation run again.
        pass


def inspect_health(store):
    now=time.time();error=None
    try:
        con=sqlite3.connect(store.path.as_uri()+'?mode=rw',uri=True,timeout=.2)
        try:
            con.execute('BEGIN IMMEDIATE')
            version=con.execute('SELECT version FROM agency_meta').fetchone()[0]
            if version!=7:raise sqlite3.DatabaseError('schema unavailable')
            con.rollback()
        finally:con.close()
    except (sqlite3.Error,OSError) as exc:error=type(exc).__name__
    reports=[]
    folder=store.path.parent/(store.path.name+'.health')
    try:
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]:
            try:reports.append(json.loads(path.read_text()))
            except (OSError,ValueError):continue
    except OSError:pass
    return {'checked_at':now,'storage_accessible':error is None,'storage_error':error,'workers':reports,
            'note':'Erişim ve yazma kilidi kontrolü; her kalıcı yazmanın ayrıca başarılı olması gerekir.'}
