import json

import pytest

from verda.agency.operators import operator_status
from verda.agency.registry import AgencyConfig
from verda.agency.store import AgencyStore
from verda.agency.tuning import _SIMPLE, apply, describe, number, run_now, tool_wait


def _store(tmp_path):
    store = AgencyStore(tmp_path / 'a.sqlite', AgencyConfig.load(None), supervisor_agent=None)
    store.initialize()
    return store


def test_values_are_checked_against_their_bounds_and_a_bad_batch_changes_nothing(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        apply(store, [{'key': 'queue.batch', 'value': 99}])
    with pytest.raises(ValueError):                                             # one good and one bad: all or nothing
        apply(store, [{'key': 'queue.stuck_after', 'value': 4}, {'key': 'queue.batch', 'value': 0}])
    assert number(store, 'queue.stuck_after') == 3
    with pytest.raises(ValueError):
        apply(store, [{'key': 'queue.batch', 'value': 2.5}])                    # whole numbers only
    with pytest.raises(ValueError):
        apply(store, [{'key': 'no.such.setting', 'value': 1}])
    apply(store, [{'key': 'queue.stuck_after', 'value': 5}])
    assert number(store, 'queue.stuck_after') == 5
    apply(store, [{'key': 'queue.stuck_after', 'value': None}])                 # back to the default
    assert number(store, 'queue.stuck_after') == 3
    assert [l['key'] for l in describe(store)['log']][:2] == ['queue.stuck_after', 'queue.stuck_after']


def test_a_tool_wait_override_is_used_and_the_minimum_may_not_pass_the_maximum(tmp_path):
    store = _store(tmp_path)
    tool = next(t for t in store.config.tools if t.key == 'sahibinden.read_thread')
    with store.transaction() as con:
        assert tool_wait(con, tool) == (0.5, 6)
    apply(store, [{'key': 'tool.sahibinden.read_thread.min_interval_seconds', 'value': 2}, {'key': 'tool.sahibinden.read_thread.max_interval_seconds', 'value': 3}])
    with store.transaction() as con:
        low, high = tool_wait(con, tool)
    assert (low, high) == (2, 3) and all(2 <= AgencyStore._pause(tool, (low, high)) <= 3 for _ in range(50))
    with pytest.raises(ValueError):
        apply(store, [{'key': 'tool.sahibinden.read_thread.min_interval_seconds', 'value': 9}])      # 9 > current max 3


def test_agent_limits_apply_to_tasks_created_afterwards_only(tmp_path):
    store = _store(tmp_path)
    old = store.submit(agent='manager', objective='a', inputs={}, request_key='before')
    apply(store, [{'key': 'agent.manager.max_turns', 'value': 5}, {'key': 'agent.manager.timeout_seconds', 'value': 60}])
    new = store.submit(agent='manager', objective='b', inputs={}, request_key='after')
    with store.transaction() as con:
        defs = {r['id']: json.loads(r['definition']) for r in con.execute('SELECT id,definition FROM inbox')}
    assert defs[old]['max_turns'] == 16 and defs[new]['max_turns'] == 5 and defs[new]['model']['timeout_seconds'] == 60


def test_timers_can_be_retimed_switched_off_and_run_by_hand_once(tmp_path):
    store = _store(tmp_path)
    store.register_trigger('daily', 'timer', {'agent': 'sahibinden', 'objective': 'tara', 'interval_seconds': 86400, 'inputs': {'routine': 'daily_scan'}}, next_at=10_000_000.0)
    apply(store, [{'key': 'trigger.daily.interval_minutes', 'value': 30}], now=1000.0)
    with store.transaction() as con:
        row = con.execute("SELECT spec,next_at FROM triggers WHERE key='daily'").fetchone()
    assert json.loads(row['spec'])['interval_seconds'] == 1800 and row['next_at'] == 1000.0 + 1800     # the next run is not later than one new interval
    with pytest.raises(ValueError):
        apply(store, [{'key': 'trigger.daily.interval_minutes', 'value': 0}])
    apply(store, [{'key': 'trigger.daily.enabled', 'value': False}])
    assert describe(store)['triggers'][0]['enabled'] is False
    task = run_now(store, 'daily', now=2000.0)
    with store.transaction() as con:
        row = con.execute('SELECT agent,source,inputs FROM inbox WHERE id=?', (task,)).fetchone()
    assert (row['agent'], row['source'], json.loads(row['inputs'])) == ('sahibinden', 'manual:daily', {'routine': 'daily_scan'})
    with pytest.raises(ValueError):
        run_now(store, 'daily', now=2001.0)                                      # still running: not started twice
    with pytest.raises(KeyError):
        run_now(store, 'nope')


def test_operator_status_shows_working_waiting_idle_and_what_needs_the_user(tmp_path):
    store = _store(tmp_path)
    now = 5000.0
    idle = operator_status(store, now=now)
    assert {a['key']: a['state'] for a in idle['agents']}['manager'] == 'idle'
    store.submit(agent='sahibinden', objective='bir', inputs={}, request_key='q1', now=now)
    store.submit(agent='sahibinden', objective='iki', inputs={}, request_key='q2', now=now)
    waiting = {a['key']: a for a in operator_status(store, now=now)['agents']}['sahibinden']
    assert waiting['state'] == 'waiting' and waiting['queue'] == 2 and [t['objective'] for t in waiting['tasks']] == ['bir', 'iki']
    claimed = store.claim(mode='local', now=now)
    working = {a['key']: a for a in operator_status(store, now=now + 1)['agents']}['sahibinden']
    assert working['state'] == 'working' and working['queue'] == 2
    store.transition(claimed, 'blocked', reason='read_failed', now=now + 2)
    stopped = {a['key']: a for a in operator_status(store, now=now + 3)['agents']}['sahibinden']
    assert stopped['needs_you'] == 1 and stopped['queue'] == 1


def test_the_default_turn_limit_of_every_batch_operator_covers_the_largest_batch_with_room_to_spare():
    # one turn per listing, one to summarize, and a few for answers the engine sends back for correction
    largest = _SIMPLE['queue.batch'][4]
    config = AgencyConfig.load(None)
    operators = [a for a in config.agents if 'routine=batch' in a.prompt]
    assert {a.key for a in operators} == {'sahibinden', 'parcel', 'research'}
    for agent in operators:
        assert agent.max_turns >= largest + 1 + 2, agent.key
