"""An executable synthetic scenario; real connectors are deliberately absent."""
from verda.workflow import WorkflowStore


def step(store: WorkflowStore, worker_id: str, *, mode: str = "demo", now: float | None = None) -> bool:
    claim = store.claim(worker_id, mode, now=now)
    if claim is None:
        return False
    if claim.mode != "demo":
        store.fail(claim, "connector_not_implemented", blocked=True, now=now)
        return True
    fixtures = {
        "read_listing": {"title": "Sentetik test ilanı", "price_tl": 8_000_000, "sale_area_m2": 2000},
        "resolve_parcel": {"parcel_key": "DEMO:1/1"},
        "read_official_parcel": {"identity_matched": True, "geometry_validated": True},
        "natural_sit": {"status": "synthetic_clear"},
        "archaeological_sit": {"status": "synthetic_clear"},
        "research_access": {"status": "synthetic_direct_access"},
        "route": {"minutes": [42, 48], "destination": "synthetic"},
        "elevation": {"metres": 80, "source": "synthetic"},
        "assess": {"summary": "Örnek akış tamamlandı; bu bir gerçek ilan değerlendirmesi değildir."},
    }
    if claim.action not in fixtures:
        store.fail(claim, "demo_handler_not_implemented", blocked=True, now=now)
    else:
        store.complete_demo(claim, fixtures[claim.action], now=now)
    return True
