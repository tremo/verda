"""Local, versioned agent instructions; existing tasks keep their own snapshot."""
import json
import time
from verda.legacy import canonical, digest


def migrate_v6(con):
    con.execute('''CREATE TABLE IF NOT EXISTS agent_revisions(
        agent TEXT NOT NULL, revision TEXT NOT NULL, definition TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(agent,revision))''')
    con.execute('''CREATE TABLE IF NOT EXISTS browser_sessions(
        id TEXT PRIMARY KEY, label TEXT NOT NULL, seen_at REAL NOT NULL,
        expires_at REAL NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS browser_jobs(
        id TEXT PRIMARY KEY REFERENCES tool_calls(id), task_id TEXT NOT NULL REFERENCES inbox(id),
        tool TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL,
        worker TEXT, token TEXT, lease_until REAL, created_at REAL NOT NULL,
        finished_at REAL, result_hash TEXT)''')
    con.execute('UPDATE agency_meta SET version=6')


def effective_agent(con, base):
    row = con.execute('SELECT definition FROM agent_revisions WHERE agent=? ORDER BY rowid DESC LIMIT 1', (base.key,)).fetchone()
    if not row:
        return base
    value = json.loads(row['definition'])
    # Local edits only change prose, never tool or delegation permissions.
    return base.model_copy(update={k: value[k] for k in ('prompt', 'description', 'version')})


def agent_view(con, base):
    agent = effective_agent(con, base)
    return {**agent.model_dump(mode='json'), 'edit_revision': digest(agent.model_dump(mode='json'))}


def save_agent(store, key, *, prompt, description, expected_revision, now=None):
    if key not in store.agents:
        raise KeyError(key)
    if not prompt.strip() or len(prompt) > 12000 or not description.strip() or len(description) > 1000:
        raise ValueError('Yönerge ve görev açıklaması boş veya çok uzun.')
    with store.transaction() as con:
        current = agent_view(con, store.agents[key])
        if current['edit_revision'] != expected_revision:
            raise ValueError('Bu agent başka bir pencerede değişti. Yenileyip tekrar düzenle.')
        if current['prompt'] == prompt and current['description'] == description:
            return current
        count = con.execute('SELECT count(*) FROM agent_revisions WHERE agent=?', (key,)).fetchone()[0]
        updated = {**current, 'prompt': prompt, 'description': description,
                   'version': store.agents[key].version + '.yerel.' + str(count + 1)}
        updated.pop('edit_revision')
        revision = digest(updated)
        con.execute('INSERT INTO agent_revisions VALUES(?,?,?,?)',
                    (key, revision, canonical(updated), time.time() if now is None else now))
        return {**updated, 'edit_revision': revision}
