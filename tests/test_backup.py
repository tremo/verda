import os
import sqlite3

from verda.agency.backup import KEEP, maybe_backup
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore


def test_one_consistent_copy_a_day_and_only_the_last_seven_are_kept(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    store.submit(agent='manager', objective='x', inputs={}, request_key='k')
    day = 86400.0
    first = maybe_backup(store, now=1_700_000_000.0)
    assert first.exists() and maybe_backup(store, now=1_700_000_000.0 + 60) is None            # the same day: nothing more
    assert sqlite3.connect(first).execute('select count(*) from inbox').fetchone()[0] == 1       # a real copy of the data
    for n in range(1, 10):
        maybe_backup(store, now=1_700_000_000.0 + n * day)
    kept = sorted((tmp_path / 'backups').glob('agency-daily-*.sqlite'))
    assert len(kept) == KEEP and not list((tmp_path / 'backups').glob('*.partial'))


def test_the_agency_database_runs_in_write_ahead_log_mode(tmp_path):
    import sqlite3
    from verda.agency.registry import AgencyConfig
    from verda.agency.store import AgencyStore
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    assert sqlite3.connect(store.path).execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
