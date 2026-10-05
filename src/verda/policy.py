"""Pure screening decisions for NEW validated observations, never legacy prose."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = "datca-2026-10-05"
    max_price_tl: int = 15_000_000
    min_area_m2: int = 1_000
    max_route_minutes: int = 45


class ScreeningFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    price_tl: FiniteFloat | None = Field(default=None, gt=0)
    sale_area_m2: FiniteFloat | None = Field(default=None, gt=0)
    sale_scope_resolved: bool = False
    shared_title: bool | None = None
    archaeological_sit: bool | None = None
    natural_sit: Literal["none", "first_degree", "second_degree", "third_degree", "archival_exception", "unknown"] = "unknown"
    access: Literal["direct", "existing_easement", "corridor_conditional", "none", "unknown"] = "unknown"
    route_minutes: list[FiniteFloat] = Field(default_factory=list)
    destination_verified: bool = False
    conflicts: list[str] = Field(default_factory=list)
    # Each asserted field must cite evidence before this pure evaluator accepts it.
    evidence_refs: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_evidence(self):
        asserted = {
            "price_tl": self.price_tl is not None,
            "sale_area_m2": self.sale_area_m2 is not None,
            "shared_title": self.shared_title is not None,
            "archaeological_sit": self.archaeological_sit is not None,
            "natural_sit": self.natural_sit != "unknown",
            "access": self.access != "unknown",
            "route_minutes": bool(self.route_minutes),
            "destination_verified": self.destination_verified,
            "sale_scope_resolved": self.sale_scope_resolved,
        }
        for field, present in asserted.items():
            if present and not any(ref.strip() for ref in self.evidence_refs.get(field, [])):
                raise ValueError(f"{field} requires evidence references")
        if any(value <= 0 for value in self.route_minutes):
            raise ValueError("Route durations must be positive")
        return self


class Decision(BaseModel):
    state: Literal["excluded", "outside_search", "research_required", "conditional", "eligible"]
    policy_version: str
    reasons: list[str]
    conditions: list[str]


def evaluate(facts: ScreeningFacts, policy: Policy | None = None) -> Decision:
    policy = policy or Policy()
    hard, conditions, missing = [], [], []
    # Conflicted fields are not accepted as established facts by this evaluator.
    if facts.conflicts:
        return Decision(state="research_required", policy_version=policy.version,
                        reasons=["conflicting_evidence"], conditions=facts.conflicts)
    if facts.price_tl is not None and facts.price_tl > policy.max_price_tl:
        hard.append("price_over_limit")
    if facts.sale_scope_resolved and facts.sale_area_m2 is not None and facts.sale_area_m2 < policy.min_area_m2:
        hard.append("area_below_minimum")
    if facts.shared_title is True:
        hard.append("shared_title")
    if facts.archaeological_sit is True:
        hard.append("archaeological_sit")
    if facts.natural_sit == "first_degree":
        hard.append("first_degree_natural_sit")
    if facts.access == "none":
        hard.append("no_access")
    if hard:
        return Decision(state="excluded", policy_version=policy.version, reasons=hard, conditions=[])
    route_valid = facts.destination_verified and bool(facts.route_minutes)
    if route_valid and min(facts.route_minutes) > policy.max_route_minutes:
        return Decision(state="outside_search", policy_version=policy.version,
                        reasons=["route_over_limit"], conditions=[])
    if route_valid and min(facts.route_minutes) <= policy.max_route_minutes < max(facts.route_minutes):
        conditions.append("route_threshold_conflict")
    for field, absent in {
        "price": facts.price_tl is None,
        "sale_area": facts.sale_area_m2 is None or not facts.sale_scope_resolved,
        "archaeological_sit": facts.archaeological_sit is None,
        "natural_sit": facts.natural_sit == "unknown",
        "access": facts.access == "unknown",
        "route": not route_valid,
    }.items():
        if absent:
            missing.append(field)
    # No explicit shared-title disclosure may remain a screening assumption,
    # never an official ownership assertion or a routine seller question.
    if facts.shared_title is None:
        conditions.append("ownership_not_officially_verified")
    if facts.natural_sit in {"second_degree", "third_degree", "archival_exception"}:
        conditions.append(f"natural_sit_{facts.natural_sit}")
    if facts.access in {"existing_easement", "corridor_conditional"}:
        conditions.append(f"access_{facts.access}")
    return Decision(state="research_required" if missing else "conditional" if conditions else "eligible",
                    policy_version=policy.version, reasons=missing or ["screening_complete"], conditions=conditions)
