import pytest
from pydantic import ValidationError

from verda.policy import ScreeningFacts, evaluate


def facts(**overrides):
    values = dict(price_tl=15_000_000.0, sale_area_m2=1000.0, sale_scope_resolved=True,
                  shared_title=False, archaeological_sit=False, natural_sit="none", access="direct",
                  route_minutes=[45.0], destination_verified=True)
    values.update(overrides)
    values["evidence_refs"] = {key: [f"evidence-demo:{key}"] for key in values}
    return ScreeningFacts(**values)


def test_exact_boundaries_are_included():
    assert evaluate(facts()).state == "eligible"


@pytest.mark.parametrize("changes,reason", [
    ({"price_tl": 15_000_001.0}, "price_over_limit"),
    ({"sale_area_m2": 999.9}, "area_below_minimum"),
    ({"shared_title": True}, "shared_title"),
    ({"archaeological_sit": True}, "archaeological_sit"),
    ({"natural_sit": "first_degree"}, "first_degree_natural_sit"),
    ({"access": "none"}, "no_access"),
])
def test_hard_exclusions(changes, reason):
    result = evaluate(facts(**changes))
    assert result.state == "excluded"
    assert reason in result.reasons


def test_over_45_is_separate_and_threshold_disagreement_conditional():
    assert evaluate(facts(route_minutes=[46.0, 48.0])).state == "outside_search"
    result = evaluate(facts(route_minutes=[41.0, 48.0]))
    assert result.state == "conditional"
    assert "route_threshold_conflict" in result.conditions
    assert evaluate(facts(route_minutes=[46.0], shared_title=True)).state == "excluded"


def test_unverified_destination_never_gives_final_route_result():
    result = evaluate(facts(route_minutes=[70.0], destination_verified=False))
    assert result.state == "research_required"
    assert "route" in result.reasons


def test_unknowns_and_conflicts_are_not_invented():
    assert evaluate(ScreeningFacts()).state == "research_required"
    assert evaluate(facts(conflicts=["parcel_identity"])).state == "research_required"
    assert evaluate(facts(sale_scope_resolved=False, sale_area_m2=800.0)).state == "research_required"


def test_future_right_of_way_cannot_be_passed_as_existing():
    with pytest.raises(ValidationError):
        facts(access="can_obtain_easement")
    assert evaluate(facts(access="existing_easement")).state == "conditional"


def test_facts_without_evidence_rejected():
    with pytest.raises(ValidationError, match="evidence references"):
        ScreeningFacts(price_tl=1_000_000.0)


@pytest.mark.parametrize("duration", [-1.0, 0.0, float("nan"), float("inf")])
def test_invalid_route_measurement_rejected(duration):
    with pytest.raises(ValidationError):
        facts(route_minutes=[duration])


def test_boolean_is_not_a_price():
    with pytest.raises(ValidationError):
        facts(price_tl=True)
