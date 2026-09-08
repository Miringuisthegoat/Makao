"""
Backend/tests/test_scoring.py

Unit tests for Phase 3 deterministic scoring engine.
Run with: pytest Backend/tests/test_scoring.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from intelligence.scoring import (
    WEIGHTS,
    ListingSnapshot,
    RenterPreferences,
    compute_match_score,
    rank_listings,
    score_availability,
    score_budget,
    score_commute,
    score_location,
    score_property,
    score_trust,
)


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def make_listing(**overrides) -> ListingSnapshot:
    defaults = dict(
        listing_id=1,
        property_id=1,
        asking_price=25000,
        service_charge=2000,
        property_type="1 bedroom",
        bedrooms=1,
        bathrooms=1,
        furnished=False,
        parking_spaces=0,
        neighbourhood="Kilimani",
        has_water=True,
        has_internet_ready=True,
        has_security=True,
        last_confirmed_at=NOW,
        availability_status="available",
        host_verification_status="verified",
        estimated_monthly_cost=36000,
        commute_minutes=35,
    )
    defaults.update(overrides)
    return ListingSnapshot(**defaults)


def make_prefs(**overrides) -> RenterPreferences:
    defaults = dict(
        rent_budget=25000,
        total_housing_budget=36000,
        bedrooms=1,
        preferred_property_type="1 bedroom",
        commute_limit_minutes=45,
        preferred_neighbourhoods=["Kilimani", "Upper Hill"],
        parking_required=False,
        furnished_preference=None,
        water_importance=3,
        internet_importance=3,
        safety_importance=3,
    )
    defaults.update(overrides)
    return RenterPreferences(**defaults)


class TestWeights:
    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

    def test_weights_match_roadmap(self):
        assert WEIGHTS["budget"] == 0.30
        assert WEIGHTS["location"] == 0.20
        assert WEIGHTS["property"] == 0.15
        assert WEIGHTS["commute"] == 0.15
        assert WEIGHTS["availability"] == 0.10
        assert WEIGHTS["amenities"] == 0.05
        assert WEIGHTS["trust"] == 0.05


class TestBudgetScore:
    def test_within_budget_scores_high(self):
        listing = make_listing(estimated_monthly_cost=35000)
        prefs = make_prefs(total_housing_budget=36000)
        score, note = score_budget(listing, prefs)
        assert score >= 90

    def test_over_budget_penalized(self):
        listing = make_listing(estimated_monthly_cost=50000)
        prefs = make_prefs(total_housing_budget=36000)
        score, _ = score_budget(listing, prefs)
        assert score < 60

    def test_no_budget_pref_is_neutral(self):
        listing = make_listing()
        prefs = make_prefs(total_housing_budget=None, rent_budget=None)
        score, _ = score_budget(listing, prefs)
        assert score == 50

    def test_falls_back_to_asking_price_if_no_estimate(self):
        listing = make_listing(estimated_monthly_cost=None, asking_price=25000, service_charge=2000)
        prefs = make_prefs(total_housing_budget=27000)
        score, _ = score_budget(listing, prefs)
        assert score >= 90

    def test_score_bounded_0_100(self):
        listing = make_listing(estimated_monthly_cost=200000)
        prefs = make_prefs(total_housing_budget=10000)
        score, _ = score_budget(listing, prefs)
        assert 0 <= score <= 100


class TestLocationScore:
    def test_exact_match(self):
        listing = make_listing(neighbourhood="Kilimani")
        prefs = make_prefs(preferred_neighbourhoods=["Kilimani"])
        score, _ = score_location(listing, prefs)
        assert score == 100

    def test_no_match(self):
        listing = make_listing(neighbourhood="Ruaka")
        prefs = make_prefs(preferred_neighbourhoods=["Kilimani", "Upper Hill"])
        score, _ = score_location(listing, prefs)
        assert score == 35

    def test_no_preference_is_neutral(self):
        listing = make_listing()
        prefs = make_prefs(preferred_neighbourhoods=[])
        score, _ = score_location(listing, prefs)
        assert score == 70

    def test_missing_listing_neighbourhood(self):
        listing = make_listing(neighbourhood=None)
        prefs = make_prefs(preferred_neighbourhoods=["Kilimani"])
        score, _ = score_location(listing, prefs)
        assert score == 40


class TestPropertyScore:
    def test_perfect_match(self):
        listing = make_listing(bedrooms=1, property_type="1 bedroom")
        prefs = make_prefs(bedrooms=1, preferred_property_type="1 bedroom", parking_required=False)
        score, _ = score_property(listing, prefs)
        assert score == 100

    def test_bedroom_off_by_one(self):
        listing = make_listing(bedrooms=2)
        prefs = make_prefs(bedrooms=1, preferred_property_type=None, parking_required=False)
        score, _ = score_property(listing, prefs)
        assert 0 < score < 100

    def test_parking_required_but_missing(self):
        listing = make_listing(parking_spaces=0, bedrooms=1, property_type="1 bedroom")
        prefs = make_prefs(bedrooms=1, preferred_property_type="1 bedroom", parking_required=True)
        score, _ = score_property(listing, prefs)
        assert score < 100

    def test_no_preferences_is_neutral(self):
        listing = make_listing()
        prefs = make_prefs(bedrooms=None, preferred_property_type=None, parking_required=False,
                            furnished_preference=None)
        score, _ = score_property(listing, prefs)
        assert score == 70


class TestCommuteScore:
    def test_within_limit(self):
        listing = make_listing(commute_minutes=30)
        prefs = make_prefs(commute_limit_minutes=45)
        score, _ = score_commute(listing, prefs)
        assert score >= 80

    def test_over_limit(self):
        listing = make_listing(commute_minutes=90)
        prefs = make_prefs(commute_limit_minutes=45)
        score, _ = score_commute(listing, prefs)
        assert score < 50

    def test_missing_commute_data_is_neutral(self):
        listing = make_listing(commute_minutes=None)
        prefs = make_prefs()
        score, _ = score_commute(listing, prefs)
        assert score == 50


class TestAvailabilityScore:
    def test_confirmed_today(self):
        listing = make_listing(availability_status="available", last_confirmed_at=NOW)
        score, note = score_availability(listing, now=NOW)
        assert score == 100
        assert "today" in note

    def test_confirmed_a_week_ago(self):
        listing = make_listing(availability_status="available", last_confirmed_at=NOW - timedelta(days=7))
        score, _ = score_availability(listing, now=NOW)
        assert score == 70

    def test_stale_confirmation(self):
        listing = make_listing(availability_status="available", last_confirmed_at=NOW - timedelta(days=30))
        score, note = score_availability(listing, now=NOW)
        assert score == 20
        assert "stale" in note

    def test_unavailable_scores_zero(self):
        listing = make_listing(availability_status="unavailable")
        score, _ = score_availability(listing, now=NOW)
        assert score == 0

    def test_never_confirmed_is_not_treated_as_available(self):
        listing = make_listing(availability_status="uncertain", last_confirmed_at=None)
        score, _ = score_availability(listing, now=NOW)
        assert score == 20


class TestTrustScore:
    def test_verified_scores_highest(self):
        listing = make_listing(host_verification_status="verified")
        score, _ = score_trust(listing)
        assert score == 100

    def test_unverified_scores_lowest(self):
        listing = make_listing(host_verification_status="unverified")
        score, _ = score_trust(listing)
        assert score == 20

    def test_unknown_status_defaults_to_unverified_score(self):
        listing = make_listing(host_verification_status="garbage")
        score, _ = score_trust(listing)
        assert score == 20


class TestComputeMatchScore:
    def test_returns_all_subscores(self):
        listing = make_listing()
        prefs = make_prefs()
        result = compute_match_score(listing, prefs, now=NOW)
        d = result.to_dict()
        for key in ["overall_score", "budget_score", "location_score", "property_score",
                    "commute_score", "availability_score", "amenities_score", "trust_score"]:
            assert key in d
            assert 0 <= d[key] <= 100

    def test_overall_score_is_weighted_average(self):
        listing = make_listing()
        prefs = make_prefs()
        result = compute_match_score(listing, prefs, now=NOW)
        expected = (
            result.budget_score * WEIGHTS["budget"]
            + result.location_score * WEIGHTS["location"]
            + result.property_score * WEIGHTS["property"]
            + result.commute_score * WEIGHTS["commute"]
            + result.availability_score * WEIGHTS["availability"]
            + result.amenities_score * WEIGHTS["amenities"]
            + result.trust_score * WEIGHTS["trust"]
        )
        assert result.overall_score == round(expected)

    def test_strong_match_scores_high(self):
        """A listing matching every stated preference should score highly."""
        listing = make_listing(
            estimated_monthly_cost=35000,
            neighbourhood="Kilimani",
            bedrooms=1,
            property_type="1 bedroom",
            commute_minutes=30,
            availability_status="available",
            last_confirmed_at=NOW,
            host_verification_status="verified",
        )
        prefs = make_prefs()
        result = compute_match_score(listing, prefs, now=NOW)
        assert result.overall_score >= 85

    def test_poor_match_scores_low(self):
        listing = make_listing(
            estimated_monthly_cost=90000,
            neighbourhood="Ruaka",
            bedrooms=4,
            property_type="house",
            commute_minutes=120,
            availability_status="uncertain",
            last_confirmed_at=None,
            host_verification_status="unverified",
        )
        prefs = make_prefs()
        result = compute_match_score(listing, prefs, now=NOW)
        assert result.overall_score <= 35

    def test_deterministic_same_inputs_same_output(self):
        listing = make_listing()
        prefs = make_prefs()
        r1 = compute_match_score(listing, prefs, now=NOW)
        r2 = compute_match_score(listing, prefs, now=NOW)
        assert r1.to_dict() == r2.to_dict()


class TestRankListings:
    def test_sorts_best_first(self):
        strong = make_listing(listing_id=1, estimated_monthly_cost=35000, commute_minutes=30)
        weak = make_listing(listing_id=2, estimated_monthly_cost=90000, commute_minutes=120,
                             neighbourhood="Ruaka", availability_status="uncertain",
                             last_confirmed_at=None, host_verification_status="unverified")
        prefs = make_prefs()
        ranked = rank_listings([weak, strong], prefs, now=NOW)
        assert ranked[0][0].listing_id == 1
        assert ranked[0][1].overall_score >= ranked[1][1].overall_score

    def test_empty_list(self):
        assert rank_listings([], make_prefs()) == []
