from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest
from pydantic import ValidationError

from verda.workflow import LeaseLost, Plan, Step, WorkflowStore, research_plan
from verda.worker import step


@pytest.fixture
def store(tmp_path):
    result = WorkflowStore(tmp_path / "workflows.sqlite")
    result.initialize()
    return result


def listing_plan(case="demo-a", *, mode="demo", attempts=3):
    return Plan(case_ref=case, source_revision=1, mode=mode,
                steps=(Step(key="listing", agent="sahibinden", action="read_listing", max_attempts=attempts),))


def test_demo_runs_in_order_and_survives_reopen(store):
    run = store.create_run(research_plan("demo", 1, "demo"), "demo-request", now=0)
    assert step(store, "worker-a", now=1)
    reopened = WorkflowStore(store.path)
    while step(reopened, "worker-b", now=2):
        pass
    view = reopened.view(run)
    assert view["status"] == "complete"
    assert len(view["tasks"]) == 9
    assert all(task["attempts"] == 1 and task["result"]["demo"] for task in view["tasks"])
    assert all("lease_token" not in task for task in view["tasks"])
    assert [event["details"]["step"] for event in view["events"] if event["kind"] == "task_succeeded"][:3] == ["listing", "identity", "parcel"]


def test_request_key_deduplicates_but_cannot_change_plan(store):
    first = store.create_run(listing_plan(), "same", now=0)
    assert store.create_run(listing_plan(), "same", now=1) == first
    with pytest.raises(ValueError, match="different plan"):
        store.create_run(listing_plan("demo-b"), "same")
    assert len(store.view(first)["events"]) == 1


@pytest.mark.parametrize("steps", [
    (Step(key="send", agent="sahibinden", action="send_message"),),
    (Step(key="route", agent="geography", action="route"),),
    (Step(key="listing", agent="sahibinden", action="read_listing", depends_on=("listing",)),),
    (Step(key="listing", agent="manager", action="propose_plan"),),
    (Step(key="listing", agent="sahibinden", action="read_listing"),) * 2,
])
def test_manager_plan_rejects_unauthorized_or_invalid_dependencies(steps):
    with pytest.raises(ValidationError):
        Plan(case_ref="demo", source_revision=1, mode="demo", steps=steps)


def test_concurrent_workers_share_one_resource_across_runs(store):
    store.create_run(listing_plan("a"), "a", now=0)
    store.create_run(listing_plan("b"), "b", now=0)
    def claim(worker):
        return WorkflowStore(store.path).claim(worker, "demo", now=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ["one", "two"]))
    assert sum(claim is not None for claim in claims) == 1
    owner = next(claim for claim in claims if claim)
    store.complete_demo(owner, {}, now=2)
    assert store.claim("third", "demo", now=3) is not None


def test_independent_resources_can_run_together(store):
    store.create_run(Plan(case_ref="demo", source_revision=1, mode="demo", steps=(
        Step(key="listing", agent="sahibinden", action="read_listing"),
        Step(key="identity", agent="parcel", action="resolve_parcel"),
    )), "independent", now=0)
    claims = [store.claim(worker, "demo", now=1) for worker in ["one", "two"]]
    assert all(claims)
    assert {claim.agent for claim in claims} == {"sahibinden", "parcel"}


def test_lease_expiry_recovers_with_backoff_and_rejects_old_result(store):
    run = store.create_run(listing_plan(), "crash", now=0)
    old = store.claim("crashed", "demo", lease_seconds=10, now=0)
    assert store.claim("new", "demo", now=10) is None
    new = store.claim("new", "demo", now=12)
    assert new.attempt == 2
    with pytest.raises(LeaseLost):
        store.complete_demo(old, {}, now=13)
    store.complete_demo(new, {}, now=13)
    view = store.view(run)
    assert view["status"] == "complete"
    assert any(event["kind"] == "lease_expired" for event in view["events"])


def test_heartbeat_extends_only_current_lease(store):
    store.create_run(listing_plan(), "heartbeat", now=0)
    claim = store.claim("one", "demo", lease_seconds=10, now=0)
    store.heartbeat(claim, lease_seconds=20, now=9)
    assert store.claim("two", "demo", now=15) is None
    store.complete_demo(claim, {}, now=20)
    with pytest.raises(LeaseLost):
        store.heartbeat(claim, now=21)


def test_retry_budget_is_bounded_and_downstream_waits(store):
    run = store.create_run(research_plan("demo", 1, "demo"), "retry", now=0)
    for attempt, now in enumerate([0, 2, 6], start=1):
        claim = store.claim("worker", "demo", now=now)
        assert claim.step_key == "listing" and claim.attempt == attempt
        store.fail(claim, "temporary_unavailable", retryable=True, now=now)
    assert store.claim("worker", "demo", now=1000) is None
    view = store.view(run)
    assert view["status"] == "failed"
    assert view["tasks"][1]["waiting_on"] == ["listing"]
    assert all(task["attempts"] == 0 for task in view["tasks"][1:])


def test_expired_final_attempt_becomes_failed(store):
    run = store.create_run(listing_plan(attempts=1), "exhausted", now=0)
    store.claim("crash", "demo", lease_seconds=1, now=0)
    assert store.claim("retry", "demo", now=2) is None
    assert store.view(run)["status"] == "failed"


def test_cancel_is_durable_idempotent_and_fences_running_result(store):
    run = store.create_run(research_plan("demo", 1, "demo"), "cancel", now=0)
    claim = store.claim("one", "demo", now=1)
    store.cancel(run, now=2)
    store.cancel(run, now=3)
    with pytest.raises(LeaseLost):
        store.complete_demo(claim, {}, now=4)
    assert store.claim("two", "demo", now=5) is None
    view = store.view(run)
    assert view["status"] == "cancelled"
    assert all(task["state"] == "cancelled" for task in view["tasks"])
    assert sum(event["kind"] == "run_cancelled" for event in view["events"]) == 1


def test_cancel_does_not_relabel_completed_research(store):
    run = store.create_run(listing_plan(), "completed", now=0)
    step(store, "one", now=1)
    assert store.cancel(run, now=2) is False
    assert store.view(run)["status"] == "complete"


def test_shadow_cannot_be_completed_by_demo_even_with_forged_claim_mode(store):
    run = store.create_run(listing_plan(mode="shadow"), "shadow", now=0)
    assert store.claim("demo", "demo", now=1) is None
    claim = store.claim("shadow", "shadow", now=1)
    with pytest.raises(ValueError, match="cannot complete"):
        store.complete_demo(replace(claim, mode="demo"), {}, now=2)
    store.fail(claim, "connector_not_implemented", blocked=True, now=2)
    assert store.view(run)["status"] == "blocked"


def test_missing_real_connector_becomes_visible_blocker(store):
    run = store.create_run(research_plan("shadow-case", 2, "shadow"), "no-connector", now=0)
    assert step(store, "shadow-worker", mode="shadow", now=1)
    assert not step(store, "shadow-worker", mode="shadow", now=2)
    task = store.view(run)["tasks"][0]
    assert task["reason"] == "connector_not_implemented"
    assert task["result"] is None


def test_invalid_parcel_result_does_not_unlock_spatial_work(store):
    run = store.create_run(research_plan("demo", 1, "demo"), "parcel", now=0)
    step(store, "a", now=1)
    step(store, "a", now=2)
    claim = store.claim("a", "demo", now=3)
    assert claim.step_key == "parcel"
    with pytest.raises(ValueError, match="identity and geometry"):
        store.complete_demo(claim, {"identity_matched": True}, now=4)
    assert store.claim("b", "demo", now=5) is None
    assert all(task["attempts"] == 0 for task in store.view(run)["tasks"][3:])


def test_event_cursor_does_not_duplicate_or_skip(store):
    run = store.create_run(listing_plan(), "events", now=0)
    step(store, "one", now=1)
    first = store.view(run, limit=2)
    second = store.view(run, after_event_id=first["next_after_event_id"], limit=2)
    assert first["has_more_events"] and not second["has_more_events"]
    ids = [event["id"] for event in first["events"] + second["events"]]
    assert len(ids) == len(set(ids)) == 3


def test_init_rejects_legacy_or_foreign_database_without_modifying_it(tmp_path):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE items(id TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="foreign"):
        WorkflowStore(path).initialize()
    assert path.read_bytes() == before
