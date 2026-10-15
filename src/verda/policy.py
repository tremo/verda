"""Pure screening decisions for NEW validated observations, never legacy prose."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


# Strongest first. `kullanici` is the user's explicit decision: it is the working
# assumption, but contradicting official evidence stays visible (never hidden).
GRADES = ("kullanici", "resmi", "bagimsiz", "beyan", "rapor")
_ANY = GRADES
_SIT = ("kullanici", "resmi", "bagimsiz", "beyan")


def _field_grades() -> dict[str, tuple[str, ...]]:
    """Which evidence grades may establish a field for screening.

    Mirrors the legacy fallback_evidence_policy: sit and cadastral frontage may rest
    on an explicit statement (`beyan`); area scope, registered type, geometry and
    route need official or independent evidence. A weaker claim is kept as a lead.
    """
    return {
        "price_tl": _ANY,
        "sale_area_m2": _ANY,
        "sale_scope_resolved": ("kullanici", "resmi", "bagimsiz"),
        "parcel_key": ("kullanici", "resmi", "beyan"),
        "property_type": ("kullanici", "resmi", "beyan"),
        "shared_title": ("kullanici", "resmi", "beyan", "rapor"),   # `rapor` = "the listing does not say hisseli"
        "archaeological_sit": _SIT,
        "natural_sit": _SIT,
        "access": _SIT,
        "route_minutes": ("resmi", "bagimsiz"),
        "destination_verified": ("kullanici", "resmi"),
        "sale_parcels": ("kullanici", "resmi", "beyan"),
        "parcel_area_m2": ("resmi",),
        "user_excluded": ("kullanici",),
        "scope_question": ("kullanici",),
    }


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = "datca-2026-10-15.1"
    max_price_tl: int = 15_000_000
    min_area_m2: int = 1_000
    max_route_minutes: int = 45
    # Purchase-score deductions (the legacy business rules, 03-BUSINESS-RULES §7.1 and §8.4). Never a hard gate.
    penalties: dict[str, int] = Field(default_factory=lambda: {
        "natural_sit_second_degree": 30, "natural_sit_third_degree": 30, "access_existing_easement": 10})
    field_grades: dict[str, tuple[str, ...]] = Field(default_factory=_field_grades)
    # The route target, given by the user on 2026-10-07. Verified by the user: it needs no per-listing decision.
    destination: dict = Field(default_factory=lambda: {
        "name": "Mertur Tatil Sitesi (Palamutbükü)", "lat": 36.676853, "lon": 27.512604,
        "verified_by": "user", "given_at": "2026-10-07"})


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
    user_excluded: bool = False
    conflicts: list[str] = Field(default_factory=list)
    # Each asserted field must cite evidence before this pure evaluator accepts it.
    evidence_refs: dict[str, list[str]] = Field(default_factory=dict)

    def asserted_fields(self) -> dict[str, bool]:
        return {
            "price_tl": self.price_tl is not None,
            "sale_area_m2": self.sale_area_m2 is not None,
            "shared_title": self.shared_title is not None,
            "archaeological_sit": self.archaeological_sit is not None,
            "natural_sit": self.natural_sit != "unknown",
            "access": self.access != "unknown",
            "route_minutes": bool(self.route_minutes),
            "destination_verified": self.destination_verified,
            "sale_scope_resolved": self.sale_scope_resolved,
            "user_excluded": self.user_excluded,
        }

    @model_validator(mode="after")
    def require_evidence(self):
        asserted = self.asserted_fields()
        for field, present in asserted.items():
            if present and not any(ref.strip() for ref in self.evidence_refs.get(field, [])):
                raise ValueError(f"{field} requires evidence references")
        # A conflict names what is withheld; a conflicted field must not also be asserted.
        for subject in self.conflicts:
            if asserted.get(subject):
                raise ValueError(f"{subject} is in conflict and must not be asserted")
        if any(value <= 0 for value in self.route_minutes):
            raise ValueError("Route durations must be positive")
        return self


class Decision(BaseModel):
    state: Literal["excluded", "outside_search", "research_required", "conditional", "eligible"]
    policy_version: str
    reasons: list[str]
    conditions: list[str]
    penalties: dict[str, int] = Field(default_factory=dict)     # purchase-score points removed, by cause


def evaluate(facts: ScreeningFacts, policy: Policy | None = None) -> Decision:
    policy = policy or Policy()
    hard, conditions, missing = [], [], []
    # A conflict never stops the evaluation: whatever it names is simply not asserted
    # (the assembler withholds it), and everything else keeps its force. A verified
    # price over the limit still excludes while an unrelated field is in dispute.
    conflicts = [f"conflict:{subject}" for subject in facts.conflicts]
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
    if facts.user_excluded:
        hard.append("user_excluded")          # the user looked and said no: nothing more is done for the listing
    if hard:
        return Decision(state="excluded", policy_version=policy.version, reasons=hard, conditions=conflicts)
    route_valid = facts.destination_verified and bool(facts.route_minutes)
    if route_valid and min(facts.route_minutes) > policy.max_route_minutes:
        return Decision(state="outside_search", policy_version=policy.version,
                        reasons=["route_over_limit"], conditions=conflicts)
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
    penalties = {c: policy.penalties[c] for c in conditions if c in policy.penalties}
    missing.extend(conflicts)
    return Decision(state="research_required" if missing else "conditional" if conditions else "eligible",
                    policy_version=policy.version, reasons=missing or ["screening_complete"], conditions=conditions,
                    penalties=penalties)
