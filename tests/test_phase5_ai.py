"""Run: pytest tests/test_phase5_ai.py -v   (from Backend/)"""
import asyncio
import json

from intelligence.explanation import (build_grounding, deterministic_explanation,
                                      explain, numbers_are_grounded)
from intelligence.preference_extractor import (extract_preferences, extract_with_rules,
                                               next_followup, normalize, to_renter_preferences)

MSG = "I earn 70k, work in CBD and need a one bedroom under 25k. I don't have a car."


def run(c):
    return asyncio.run(c)


def test_rules_roadmap_example():
    p = normalize(extract_with_rules(MSG), MSG)
    assert p["rent_budget"] == 25000
    assert p["income_range"] == "60000-80000"
    assert p["bedrooms"] == 1 and p["preferred_property_type"] == "1_bedroom"
    assert p["transport_mode"] == "matatu"
    assert p["workplace_name"] == "Nairobi CBD" and "workplace_latitude" in p


def test_income_not_mistaken_for_rent():
    m = "I earn 80k and want a 1 bedroom under 30k in Upper Hill, I work in Upper Hill"
    p = normalize(extract_with_rules(m), m)
    assert p["rent_budget"] == 30000


def test_bedsitter_and_commas():
    m = "bedsitter max KSh 12,000"
    p = normalize(extract_with_rules(m), m)
    assert p["bedrooms"] == 0 and p["preferred_property_type"] == "bedsitter"
    assert p["rent_budget"] == 12000


def test_llm_hallucinated_number_dropped():
    raw = {"rent_budget": 99999, "bedrooms": 2}
    p = normalize(raw, MSG)
    assert "rent_budget" not in p and p["bedrooms"] == 2


def test_invalid_values_dropped():
    p = normalize({"transport_mode": "helicopter", "preferred_property_type": "castle",
                   "safety_importance": 9, "commute_limit_minutes": 999}, "x")
    assert p == {}


def test_rent_over_total_resolved():
    p = normalize({"rent_budget": 30000, "total_housing_budget": 20000}, "30000 and 20000")
    assert "total_housing_budget" not in p


def test_followup_only_when_necessary():
    assert "rent" in next_followup({}).lower()
    assert "work" in next_followup({"rent_budget": 20000}).lower()
    assert next_followup({"rent_budget": 20000, "workplace_name": "Nairobi CBD"}) is None


def test_extract_falls_back_when_llm_fails():
    async def boom(s, u):
        raise RuntimeError("down")
    r = run(extract_preferences(MSG, llm_call=boom))
    assert r["source"] == "rules" and r["ready_to_search"] is True


def test_extract_bad_json_falls_back():
    async def junk(s, u):
        return "not json"
    assert run(extract_preferences(MSG, llm_call=junk))["preferences"]["rent_budget"] == 25000


def test_rules_win_over_llm():
    async def llm(s, u):
        return json.dumps({"rent_budget": 70000, "water_importance": 5})
    r = run(extract_preferences(MSG, llm_call=llm))
    assert r["preferences"]["rent_budget"] == 25000
    assert r["preferences"]["water_importance"] == 5


def test_upsert_compatible():
    r = run(extract_preferences(MSG, llm_call=None))
    try:
        from models import UserPreferencesUpsert
        UserPreferencesUpsert(**{k: v for k, v in r["preferences"].items()})
    except ImportError:
        pass  # models not on path in isolation


def test_to_renter_preferences():
    rp = to_renter_preferences({"rent_budget": 25000, "furnished_preference": "unfurnished"})
    assert rp.rent_budget == 25000 and rp.furnished_preference is False


SCORE = {"overall_score": 88, "budget_score": 92, "location_score": 85, "property_score": 95,
         "commute_score": 40, "availability_score": 96, "amenities_score": 60, "trust_score": 30,
         "notes": ["Rent KSh 25,000 is within budget.", "In Kilimani.", "1 bedroom matches.",
                   "Commute about 55 minutes.", "Confirmed today.", "Some amenities.",
                   "Host verification status: unverified."]}
FACTS = {"asking_price": 25000, "estimated_monthly_cost": 33000, "commute_minutes": 55}


def test_deterministic_explanation_shape():
    e = deterministic_explanation(build_grounding(SCORE, FACTS))
    assert 3 <= len(e["reasons"]) <= 5 and 1 <= len(e["drawbacks"]) <= 3
    assert any("Commute" in d for d in e["drawbacks"])
    assert any("Trust" in d for d in e["drawbacks"])


def test_no_commute_drawback_without_data():
    s = {**SCORE, "commute_score": 40}
    e = deterministic_explanation(build_grounding(s, {}))
    assert not any(d.startswith("Commute") for d in e["drawbacks"])


def test_llm_with_invented_number_rejected():
    async def llm(s, u):
        return json.dumps({"summary": "Great", "reasons": ["Rent is only KSh 18,000"], "drawbacks": []})
    e = run(explain(SCORE, FACTS, llm_call=llm))
    assert e["source"] == "deterministic"


def test_llm_grounded_accepted():
    async def llm(s, u):
        return json.dumps({"summary": "88% match", "reasons": ["Rent KSh 25,000 fits", "Confirmed today", "1 bedroom"],
                           "drawbacks": ["Commute about 55 minutes"]})
    e = run(explain(SCORE, FACTS, llm_call=llm))
    assert e["source"] == "llm"


def test_llm_extra_drawbacks_rejected():
    async def llm(s, u):
        return json.dumps({"summary": "ok", "reasons": ["a"], "drawbacks": ["x", "y", "z"]})
    assert run(explain(SCORE, FACTS, llm_call=llm))["source"] == "deterministic"


def test_explain_survives_llm_failure():
    async def boom(s, u):
        raise TimeoutError()
    assert run(explain(SCORE, FACTS, llm_call=boom))["source"] == "deterministic"
