"""Numbers the user may change from the panel, with bounds, defaults and a change log.

Defaults come from the configuration files; a local value overrides one default and can be removed to go back to it. Values are
read at the moment they are used, so a change needs no restart. A change never touches a task that already started with its
own snapshot (agent limits apply to new tasks); a call's wait applies from its next finish.
"""
import json
import time

_SIMPLE = {
    'queue.batch': ('Süpürmede bir operatöre verilecek ilan sayısı', 'Yönetici her toplu işe en fazla bu kadar ilan koyar.', 'ilan', 1, 10, 10, int),
    'queue.stuck_after': ('Kaç başarısızlıktan sonra adım "takıldı" sayılsın', 'Takılan adım otomatik denenmez; "Bekleyen konular"da sana gösterilir.', 'deneme', 2, 10, 3, int),
    'queue.rereads_per_day': ('Mahalle okunamayan ilan günde en fazla kaç kez yeniden okunsun', 'Sayfa yer bilgisini vermiyorsa sonsuz denememek için.', 'kez', 1, 6, 2, int),
    'scan.quiet_pages': ('Günlük taramada kaç boş sayfa üst üste görünce dursun', 'Boş sayfa: yeni ya da değişmiş ilan olmayan sayfa.', 'sayfa', 1, 5, 2, int),
    'scan.daily_max_pages': ('Günlük taramada en fazla kaç sayfa okunsun', 'Sayfa başına 20 ilan.', 'sayfa', 1, 15, 3, int),
    'scan.full_max_pages': ('Haftalık tam taramada en fazla kaç sayfa okunsun', 'Sayfa başına 20 ilan.', 'sayfa', 1, 30, 15, int),
    'sender.dry_run_limit': ('Deneme modunda en fazla kaç mesaj taslağı hazırlansın', 'Deneme: form doldurulur ve doğrulanır, mesaj gönderilmez. Sınıra gelince önce taslakları incelemen beklenir.', 'taslak', 1, 20, 3, int),
    'sender.gap_minutes': ('İki gerçek mesaj arasında en az kaç dakika geçsin', 'Mesajlar tek tek ve görünür Chrome\'dan gönderilir; arka arkaya yağdırılmaz.', 'dk', 1, 240, 3, int),
}
# Choices (text values): key -> (label, help, group, [(value, label)], default)
_CHOICES = {
    'sender.mode': ('Satıcıya mesaj gönderme', 'Kapalı: hiçbir şey yazılmaz. Deneme: Sahibinden formu açılır, metin yazılıp doğrulanır, silinir; GÖNDERİLMEZ. Canlı: mesaj gerçekten gönderilir (08:30–21:00, her çalışmada en çok bir).',
                    'Mesaj gönderme', [('off', 'Kapalı'), ('dry_run', 'Deneme (yazar, göndermez)'), ('live', 'Canlı (gerçekten gönderir)')], 'dry_run'),
}


def _ensure(con):
    con.execute('CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS settings_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, key TEXT NOT NULL, old TEXT, new TEXT)')


def stored(con, key):
    _ensure(con)
    row = con.execute('SELECT value FROM runtime_settings WHERE key=?', (key,)).fetchone()
    return None if row is None else json.loads(row['value'])


def number(store, key, fallback=None):
    """The local value if there is one, else the default of a simple setting, else `fallback`."""
    with store.transaction() as con:
        value = stored(con, key)
    if value is not None:
        return value
    return _SIMPLE[key][5] if key in _SIMPLE else fallback


def choice(store, key):
    """The local text value of a choice setting, else its default."""
    with store.transaction() as con:
        value = stored(con, key)
    options = [v for v, _ in _CHOICES[key][3]]
    return value if value in options else _CHOICES[key][4]


def tool_wait(con, tool):
    """(min, max) seconds to wait after a call of this tool, local values first."""
    low, high = stored(con, f'tool.{tool.key}.min_interval_seconds'), stored(con, f'tool.{tool.key}.max_interval_seconds')
    return (tool.min_interval_seconds if low is None else low), (tool.max_interval_seconds if high is None else high)


def apply_agent_limits(con, agent):
    """The agent with local limits applied (new tasks snapshot this)."""
    turns, timeout = stored(con, f'agent.{agent.key}.max_turns'), stored(con, f'agent.{agent.key}.timeout_seconds')
    update = {}
    if turns is not None:
        update['max_turns'] = int(turns)
    if timeout is not None:
        update['model'] = agent.model.model_copy(update={'timeout_seconds': int(timeout)})
    return agent.model_copy(update=update) if update else agent


def _entries(store):
    out = []
    for tool in store.config.tools:
        if tool.resource and tool.transport in ('browser', 'python') and tool.effect == 'read' and (tool.min_interval_seconds or tool.max_interval_seconds is not None or tool.resource in ('sahibinden', 'tkgm', 'geography')):
            out.append({'key': f'tool.{tool.key}.min_interval_seconds', 'group': 'Kaynak bekleme süreleri', 'label': f'{tool.label}: en az bekleme', 'unit': 'sn',
                        'default': tool.min_interval_seconds, 'min': 0, 'max': 3600, 'type': 'float',
                        'help': 'İki çağrı arasında en az bu kadar beklenir. Üst sınır da varsa bekleme ikisi arasında rastgele seçilir.'})
            out.append({'key': f'tool.{tool.key}.max_interval_seconds', 'group': 'Kaynak bekleme süreleri', 'label': f'{tool.label}: en çok bekleme', 'unit': 'sn',
                        'default': tool.max_interval_seconds if tool.max_interval_seconds is not None else tool.min_interval_seconds, 'min': 0, 'max': 3600, 'type': 'float',
                        'help': 'Alttan küçük olamaz. Aynıysa bekleme sabittir.'})
    for agent in store.agents.values():
        out.append({'key': f'agent.{agent.key}.max_turns', 'group': 'Agent sınırları', 'label': f'{agent.label}: görev başına tur sınırı', 'unit': 'tur',
                    'default': agent.max_turns, 'min': 1, 'max': 100, 'type': 'int', 'help': 'Bir görevde en fazla kaç model kararı. Aşılırsa görev senden bir şey bekler. Yeni görevlere uygulanır.'})
        out.append({'key': f'agent.{agent.key}.timeout_seconds', 'group': 'Agent sınırları', 'label': f'{agent.label}: tek model çağrısı süresi', 'unit': 'sn',
                    'default': agent.model.timeout_seconds, 'min': 10, 'max': 300, 'type': 'int', 'help': 'Bir model çağrısı bu sürede dönmezse hata sayılır. Yeni görevlere uygulanır.'})
    for key, (label, help_, group, options, default) in _CHOICES.items():
        out.append({'key': key, 'group': group, 'label': label, 'unit': '', 'default': default, 'type': 'choice', 'choices': [list(o) for o in options], 'help': help_})
    for key, (label, help_, unit, low, high, default, kind) in _SIMPLE.items():
        out.append({'key': key, 'group': 'Mesaj gönderme' if key.startswith('sender.') else 'Kuyruk ve tarama', 'label': label, 'unit': unit, 'default': default, 'min': low, 'max': high,
                    'type': 'int' if kind is int else 'float', 'help': help_})
    return out


def describe(store):
    """Everything the panel shows: current value, default, bounds, plus the timers and the recent changes."""
    with store.transaction() as con:
        _ensure(con)
        values = {r['key']: json.loads(r['value']) for r in con.execute('SELECT key,value FROM runtime_settings')}
        triggers = [{'key': t['key'], 'enabled': bool(t['enabled']), 'interval_minutes': round(json.loads(t['spec'])['interval_seconds'] / 60, 2),
                     'agent': json.loads(t['spec'])['agent'], 'objective': json.loads(t['spec'])['objective'][:120], 'next_at': t['next_at'],
                     'sample': 'ornek' in t['key']} for t in con.execute("SELECT * FROM triggers WHERE kind='timer' ORDER BY key")]
        log = [dict(r) for r in con.execute('SELECT at,key,old,new FROM settings_log ORDER BY id DESC LIMIT 30')]
    entries = _entries(store)
    for e in entries:
        e['value'] = values.get(e['key'], e['default'])
        e['changed'] = e['key'] in values
    return {'entries': entries, 'triggers': triggers, 'log': log}


def _valid(entry, value):
    if entry['type'] == 'choice':
        if value not in [v for v, _ in entry['choices']]:
            raise ValueError(f"{entry['label']}: geçersiz seçim")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{entry['label']}: sayı gerekli")
    if entry['type'] == 'int' and int(value) != value:
        raise ValueError(f"{entry['label']}: tam sayı olmalı")
    if not entry['min'] <= value <= entry['max']:
        raise ValueError(f"{entry['label']}: {entry['min']} ile {entry['max']} arasında olmalı")
    return int(value) if entry['type'] == 'int' else float(value)


def apply(store, changes, now=None):
    """All or nothing. `changes`: [{key, value}] where value None removes the local value; triggers use trigger.<key>.interval_minutes / .enabled."""
    now = time.time() if now is None else now
    entries = {e['key']: e for e in _entries(store)}
    with store.transaction() as con:
        _ensure(con)
        planned = []
        for change in changes:
            key, value = change['key'], change.get('value')
            if key.startswith('trigger.'):
                _, name, field = key.split('.', 2)
                row = con.execute('SELECT * FROM triggers WHERE key=?', (name,)).fetchone()
                if row is None or field not in ('interval_minutes', 'enabled'):
                    raise ValueError('Bilinmeyen zamanlayıcı ayarı')
                spec = json.loads(row['spec'])
                if field == 'enabled':
                    if not isinstance(value, bool):
                        raise ValueError('Etkin/pasif için evet-hayır gerekli')
                    planned.append(('trigger_enabled', name, value, str(bool(row['enabled'])), str(value)))
                else:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 10080:
                        raise ValueError('Zamanlayıcı aralığı 1 ile 10080 dakika arasında olmalı')
                    planned.append(('trigger_interval', name, float(value), f"{spec['interval_seconds'] / 60:g}", f'{value:g}'))
                continue
            if key not in entries:
                raise ValueError('Bilinmeyen ayar: ' + key)
            if value is None:
                planned.append(('remove', key, None, str(stored(con, key)), None))
            else:
                planned.append(('set', key, _valid(entries[key], value), str(stored(con, key)), str(value)))
        # a minimum may not exceed its maximum, judged on the values after the change
        after = {e['key']: (stored(con, e['key']) if stored(con, e['key']) is not None else e['default']) for e in entries.values()}
        for kind, key, value, _, _ in planned:
            if kind == 'set': after[key] = value
            if kind == 'remove': after[key] = entries[key]['default']
        for key in list(after):
            if key.endswith('.min_interval_seconds') and after[key] > after.get(key.replace('min_', 'max_'), after[key]):
                raise ValueError('En az bekleme, en çok beklemeden büyük olamaz: ' + key.split('.')[1])
        for kind, key, value, old, new in planned:
            if kind == 'set':
                con.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?,?)', (key, json.dumps(value), now))
            elif kind == 'remove':
                con.execute('DELETE FROM runtime_settings WHERE key=?', (key,))
            elif kind == 'trigger_enabled':
                con.execute('UPDATE triggers SET enabled=? WHERE key=?', (int(value), key))
            else:
                row = con.execute('SELECT * FROM triggers WHERE key=?', (key,)).fetchone()
                spec = json.loads(row['spec']); spec['interval_seconds'] = int(value * 60)
                # the next run may not be later than one new interval from now
                con.execute('UPDATE triggers SET spec=?, next_at=? WHERE key=?',
                            (json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(',', ':')), min(row['next_at'], now + spec['interval_seconds']), key))
            con.execute('INSERT INTO settings_log(at,key,old,new) VALUES(?,?,?,?)', (now, key if kind in ('set', 'remove') else f'trigger.{key}.{"enabled" if kind == "trigger_enabled" else "interval_minutes"}', old, new))
    return {'applied': len(planned)}


def run_now(store, key, now=None):
    """Start a timer's task right away by hand. A trigger that is still running is not started a second time."""
    now = time.time() if now is None else now
    with store.transaction() as con:
        row = con.execute("SELECT * FROM triggers WHERE key=? AND kind='timer'", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        spec = json.loads(row['spec'])
        if con.execute("SELECT 1 FROM inbox WHERE source IN (?,?) AND state NOT IN ('complete','failed','done') LIMIT 1", ('timer:' + key, 'manual:' + key)).fetchone():
            raise ValueError('Bu zamanlayıcının işi zaten sürüyor ya da sırada.')
        task = store._submit(con, agent=spec['agent'], objective=spec['objective'], inputs=spec.get('inputs', {}), request_key=f'manual:{key}:{int(now * 1000)}',
                             source='manual:' + key, mode=spec.get('mode', 'local'), priority=spec.get('priority', 20), executor_tool=spec.get('executor_tool'), now=now)
        con.execute('INSERT INTO settings_log(at,key,old,new) VALUES(?,?,?,?)', (now, f'manual_run.{key}', None, task))
    return task
