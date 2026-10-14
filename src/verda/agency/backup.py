"""One consistent copy of the agency database per day, the last seven kept. Cheap to call on every loop."""
import sqlite3
import time
from pathlib import Path

KEEP = 7


def maybe_backup(store, now=None, directory=None):
    now = time.time() if now is None else now
    directory = Path(directory or store.path.parent / 'backups')
    target = directory / f"agency-daily-{time.strftime('%Y%m%d', time.localtime(now))}.sqlite"
    if target.exists():
        return None
    directory.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix('.partial')
    source = sqlite3.connect(store.path)
    try:
        copy = sqlite3.connect(partial)
        with copy:
            source.backup(copy)                       # SQLite's online backup: consistent even while workers write
        copy.close()
    finally:
        source.close()
    partial.rename(target)
    for old in sorted(directory.glob('agency-daily-*.sqlite'))[:-KEEP]:
        old.unlink()
    return target
