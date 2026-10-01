"""
Backend/tests/test_affordability.py

Run with: pytest Backend/tests/test_affordability.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from intelligence.affordability import calculate_affordability


def test_basic_estimate_matches_spec_example():
    # From the product spec: rent 25000, service charge 2000, utilities 3000,
    # transport 6000 -> total 36000. We don't hard-code their utilities/transport
    # numbers (ours are derived), so we override to reproduce the example exactly.
    result = calculate_affordability(
        advertised_rent=25000,
        service_charge=2000,
        utilities_override=3000,
        transport_cost_override=6000,
    )
    assert result.estimated_monthly_cost == 36000
    assert result.is_estimate is True
    assert result.confidence == "high"


def test_no_budget_supplied_skips_score():
    result = calculate_affordability(advertised_rent=20000, service_charge=1000)
    assert result.affordability_score is None
    assert result.budget_difference is None
    assert any("budget" in n for n in result.notes)


def test_within_budget_scores_high():
    result = calculate_affordability(
        advertised_rent=20000,
        service_charge=1000,
        utilities_override=2000,
        transport_cost_override=3000,
        monthly_budget=30000,
    )
    # total = 26000, budget = 30000 -> comfortably under
    assert result.estimated_monthly_cost == 26000
    assert result.affordability_score is not None
    assert result.affordability_score >= 85


def test_over_budget_scores_low():
    result = calculate_affordability(
        advertised_rent=30000,
        service_charge=3000,
        utilities_override=4000,
        transport_cost_override=8000,
        monthly_budget=25000,
    )
    # total = 45000, budget 25000 -> ratio 1.8 -> should floor at 0
    assert result.affordability_score == 0


def test_utilities_estimated_from_property_category_when_not_provided():
    result = calculate_affordability(
        advertised_rent=15000,
        property_category="bedsitter",
    )
    assert result.estimated_utilities == 1500
    assert "bedsitter" in result.notes[0]


def test_unknown_category_falls_back_to_default_utilities():
    result = calculate_affordability(advertised_rent=15000, property_category="yurt")
    assert result.estimated_utilities == 3000  # DEFAULT_UTILITIES_FALLBACK


def test_real_property_type_keys_match_models_py_underscore_convention():
    # Regression test: DEFAULT_UTILITIES_BY_BEDROOMS previously used "1 bedroom"
    # (space) while models.py's real PROPERTY_TYPES use "1_bedroom" (underscore),
    # which silently fell through to the fallback for every real property.
    result = calculate_affordability(advertised_rent=15000, property_category="1_bedroom")
    assert result.estimated_utilities == 2500
    result2 = calculate_affordability(advertised_rent=15000, property_category="4plus_bedroom")
    assert result2.estimated_utilities == 6000


def test_preference_mode_names_map_to_engine_mode_names():
    from intelligence.affordability import map_preference_transport_mode
    assert map_preference_transport_mode("boda") == "boda_boda"
    assert map_preference_transport_mode("car") == "own_car"
    assert map_preference_transport_mode("mixed") == "matatu"
    assert map_preference_transport_mode("matatu") == "matatu"
    assert map_preference_transport_mode(None) is None


def test_transport_defaults_to_matatu_mode():
    result = calculate_affordability(advertised_rent=15000)
    # matatu: 200/day * 22 days = 4400
    assert result.estimated_transport == 4400


def test_walking_within_45_minutes_is_free():
    result = calculate_affordability(
        advertised_rent=15000,
        transport_mode="walking",
        commute_minutes=20,
    )
    assert result.estimated_transport == 0


def test_walking_over_45_minutes_still_estimated_as_if_no_walk_assumption():
    # Walking > 45 min is unrealistic as a daily commute; falls through to the
    # walking mode profile itself (0 daily cost) rather than assuming a fare —
    # this documents current behaviour so future changes are intentional.
    result = calculate_affordability(
        advertised_rent=15000,
        transport_mode="walking",
        commute_minutes=90,
    )
    assert result.estimated_transport == 0


def test_negative_rent_raises():
    with pytest.raises(ValueError):
        calculate_affordability(advertised_rent=-100)


def test_confidence_reflects_provided_data():
    low = calculate_affordability(advertised_rent=15000)
    medium = calculate_affordability(advertised_rent=15000, utilities_override=2000)
    high = calculate_affordability(
        advertised_rent=15000, utilities_override=2000, transport_cost_override=4000
    )
    assert low.confidence == "low"
    assert medium.confidence == "medium"
    assert high.confidence == "high"


def test_budget_difference_sign():
    over = calculate_affordability(advertised_rent=40000, monthly_budget=30000)
    assert over.budget_difference < 0
    under = calculate_affordability(advertised_rent=10000, monthly_budget=30000)
    assert under.budget_difference > 0
