"""
Backend/intelligence/scoring.py

Phase 3 — Deterministic Match Scoring Engine.

Architectural rule (non-negotiable, see roadmap section 11 & 41.10):
    The LLM NEVER ranks properties. It only extracts preferences (Phase 5
    preference_extractor) and explains scores after the fact (Phase 5
    explanation.py). This module is the sole authority for numeric ranking.
    It is pure, synchronous, side-effect-free, and fully unit-testable with
    no DB/network/AI calls.

Weights (roadmap section 12):
    budget       30%
    location     20%
    property     15%
    commute      15%
    availability 10%
    amenities     5%
    trust         5%

Callers (Phase 2 search.service / search.routes) are responsible for
fetching the raw data (listing, property, user_preferences, commute
estimate, affordability estimate) and passing it in as plain dicts/dataclasses.
This module does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Weights — single source of truth. Keep in sync with roadmap section 12.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "budget": 0.30,
    "location": 0.20,
    "property": 0.15,
    "commute": 0.15,
    "availability": 0.10,
    "amenities": 0.05,
    "trust": 0.05,
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Scoring weights must sum to 1.0"


# ---------------------------------------------------------------------------
# Input contracts
# ---------------------------------------------------------------------------

@dataclass
class ListingSnapshot:
    """Minimal listing+property data needed to score a candidate.

    Populate this from a joined listings/properties/locations query in
    Backend/search/service.py — this module does not touch the DB.
    """
    listing_id: int
    property_id: int
    asking_price: float
    service_charge: float = 0.0
    deposit: float = 0.0

    property_type: str = ""          # e.g. "1 bedroom", "studio", "bedsitter"
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    furnished: Optional[bool] = None
    parking_spaces: int = 0

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    neighbourhood: Optional[str] = None

    # Amenity flags — extend as real data becomes available. Missing/unknown
    # amenities must never silently count as "present"; treat None as unknown.
    has_water: Optional[bool] = None
    has_backup_power: Optional[bool] = None
    has_internet_ready: Optional[bool] = None
    has_security: Optional[bool] = None

    # Freshness
    last_confirmed_at: Optional[datetime] = None
    availability_status: str = "uncertain"   # "available" | "unavailable" | "uncertain"

    # Trust
    host_verification_status: str = "unverified"  # "unverified" | "partially_verified" | "verified"

    # Optional pre-computed estimates from other Phase 3/4 modules. If not
    # supplied, this module degrades gracefully (see docstrings below).
    estimated_monthly_cost: Optional[float] = None   # from affordability.py
    commute_minutes: Optional[float] = None          # from commute.py


@dataclass
class RenterPreferences:
    """Mirrors the relevant subset of the user_preferences table."""
    rent_budget: Optional[float] = None
    total_housing_budget: Optional[float] = None
    bedrooms: Optional[int] = None
    preferred_property_type: Optional[str] = None
    commute_limit_minutes: Optional[float] = None
    preferred_neighbourhoods: list = field(default_factory=list)
    parking_required: bool = False
    furnished_preference: Optional[bool] = None  # None = no preference
    water_importance: int = 0        # 0-5
    internet_importance: int = 0     # 0-5
    safety_importance: int = 0       # 0-5


@dataclass
class ScoreBreakdown:
    overall_score: int
    budget_score: int
    location_score: int
    property_score: int
    commute_score: int
    availability_score: int
    amenities_score: int
    trust_score: int
    # Human-readable, non-AI-generated notes. Phase 5's explanation.py may
    # use these as grounding facts, but the LLM must not invent numbers.
    notes: list

    def to_dict(self) -> dict:
        return {
            "overall_score": self.overall_score,
            "budget_score": self.budget_score,
            "location_score": self.location_score,
            "property_score": self.property_score,
            "commute_score": self.commute_score,
            "availability_score": self.availability_score,
            "amenities_score": self.amenities_score,
            "trust_score": self.trust_score,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Sub-score functions — each returns 0-100. Kept independently testable.
# ---------------------------------------------------------------------------

def score_budget(listing: ListingSnapshot, prefs: RenterPreferences) -> tuple[int, str]:
    """Budget compatibility, weighted 30%.

    Prefers estimated_monthly_cost (advertised rent + service charge +
    utilities, from affordability.py) when available, since that is the
    real-world cost the roadmap insists we surface. Falls back to
    asking_price + service_charge if no affordability estimate exists yet.
    """
    budget = prefs.total_housing_budget or prefs.rent_budget
    if budget is None or budget <= 0:
        return 50, "No budget preference supplied; neutral budget score."

    cost = listing.estimated_monthly_cost
    if cost is None:
        cost = listing.asking_price + listing.service_charge

    if cost <= 0:
        return 50, "Listing has no price data; neutral budget score."

    ratio = cost / budget

    if ratio <= 1.0:
        # At or under budget: reward being close to budget (better use of
        # money) without penalizing being cheaper.
        headroom = 1.0 - ratio
        score = 100 - (headroom * 15)  # small penalty for being far under
        note = f"Estimated cost KSh {cost:,.0f} is within budget of KSh {budget:,.0f}."
    else:
        overage = ratio - 1.0
        # Linear falloff: 20% over budget -> score ~40; 50%+ over -> floor.
        score = max(0, 100 - (overage * 200))
        note = f"Estimated cost KSh {cost:,.0f} exceeds budget of KSh {budget:,.0f} by {overage*100:.0f}%."

    return round(max(0, min(100, score))), note


def score_location(listing: ListingSnapshot, prefs: RenterPreferences) -> tuple[int, str]:
    """Location fit, weighted 20%.

    MVP-level: exact/partial neighbourhood-name match against the renter's
    preferred list. This is intentionally simple — geo-radius/geometry-based
    scoring belongs to Phase 2's geo.py and V1 neighbourhood intelligence,
    not this module.
    """
    if not prefs.preferred_neighbourhoods:
        return 70, "No neighbourhood preference supplied; default location score."

    if not listing.neighbourhood:
        return 40, "Listing has no neighbourhood data; cannot confirm location fit."

    listing_n = listing.neighbourhood.strip().lower()
    preferred = [n.strip().lower() for n in prefs.preferred_neighbourhoods]

    if listing_n in preferred:
        return 100, f"{listing.neighbourhood} is one of the renter's preferred areas."

    if any(p in listing_n or listing_n in p for p in preferred):
        return 75, f"{listing.neighbourhood} is close to a preferred area."

    return 35, f"{listing.neighbourhood} is not in the renter's preferred areas."


def score_property(listing: ListingSnapshot, prefs: RenterPreferences) -> tuple[int, str]:
    """Property fit, weighted 15%. Bedrooms, type, furnishing, parking."""
    points = 0
    max_points = 0
    reasons = []

    # Bedrooms (weight 2)
    if prefs.bedrooms is not None:
        max_points += 2
        if listing.bedrooms is not None:
            diff = abs(listing.bedrooms - prefs.bedrooms)
            if diff == 0:
                points += 2
                reasons.append(f"{listing.bedrooms}-bedroom matches exactly.")
            elif diff == 1:
                points += 1
                reasons.append(f"{listing.bedrooms}-bedroom is close to the requested {prefs.bedrooms}.")
            else:
                reasons.append(f"{listing.bedrooms}-bedroom differs from the requested {prefs.bedrooms}.")

    # Property type (weight 2)
    if prefs.preferred_property_type:
        max_points += 2
        if listing.property_type and listing.property_type.lower() == prefs.preferred_property_type.lower():
            points += 2
            reasons.append(f"Property type '{listing.property_type}' matches preference.")
        else:
            reasons.append("Property type does not match preference.")

    # Furnishing (weight 1)
    if prefs.furnished_preference is not None:
        max_points += 1
        if listing.furnished is not None and listing.furnished == prefs.furnished_preference:
            points += 1
            reasons.append("Furnishing matches preference.")

    # Parking (weight 1)
    if prefs.parking_required:
        max_points += 1
        if listing.parking_spaces > 0:
            points += 1
            reasons.append("Parking available as required.")
        else:
            reasons.append("No parking, but renter requires it.")

    if max_points == 0:
        return 70, "No specific property preferences supplied; default property score."

    score = round((points / max_points) * 100)
    return score, " ".join(reasons)


def score_commute(listing: ListingSnapshot, prefs: RenterPreferences) -> tuple[int, str]:
    """Commute fit, weighted 15%.

    Requires listing.commute_minutes to be populated by commute.py (Phase 4).
    Degrades to a neutral score if commute data isn't available yet, rather
    than fabricating a number.
    """
    if listing.commute_minutes is None:
        return 50, "No commute estimate available; neutral commute score."

    limit = prefs.commute_limit_minutes
    if limit is None or limit <= 0:
        # No stated limit — score inversely to absolute commute time as a
        # reasonable default (shorter is generically better).
        score = max(0, 100 - listing.commute_minutes)
        return round(max(0, min(100, score))), f"Estimated commute: {listing.commute_minutes:.0f} min."

    if listing.commute_minutes <= limit:
        headroom = (limit - listing.commute_minutes) / limit
        score = 80 + (headroom * 20)
        note = f"Estimated commute {listing.commute_minutes:.0f} min is within the {limit:.0f} min limit."
    else:
        overage = (listing.commute_minutes - limit) / limit
        score = max(0, 80 - (overage * 160))
        note = f"Estimated commute {listing.commute_minutes:.0f} min exceeds the {limit:.0f} min limit."

    return round(max(0, min(100, score))), note


def score_availability(listing: ListingSnapshot, now: Optional[datetime] = None) -> tuple[int, str]:
    """Availability freshness, weighted 10%.

    Never treat missing/unconfirmed data as "available" — this directly
    supports the roadmap's freshness/trust principle (section 8, 18).
    """
    now = now or datetime.now(timezone.utc)

    if listing.availability_status == "unavailable":
        return 0, "Listing is marked unavailable."

    if listing.availability_status != "available" or listing.last_confirmed_at is None:
        return 20, "Availability has not been confirmed recently."

    confirmed_at = listing.last_confirmed_at
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)

    age_days = (now - confirmed_at).total_seconds() / 86400.0

    if age_days <= 1:
        return 100, "Availability confirmed today."
    if age_days <= 3:
        return 90, f"Availability confirmed {age_days:.0f} days ago."
    if age_days <= 7:
        return 70, f"Availability confirmed {age_days:.0f} days ago."
    if age_days <= 14:
        return 45, f"Availability confirmed {age_days:.0f} days ago; may be stale."
    return 20, f"Availability last confirmed {age_days:.0f} days ago; likely stale."


def score_amenities(listing: ListingSnapshot, prefs: RenterPreferences) -> tuple[int, str]:
    """Basic amenities, weighted 5%. Only counts amenities the renter said matter."""
    weighted_points = 0
    weighted_max = 0
    reasons = []

    checks = [
        (prefs.water_importance, listing.has_water, "water supply"),
        (prefs.internet_importance, listing.has_internet_ready, "internet readiness"),
        (prefs.safety_importance, listing.has_security, "security"),
    ]

    for importance, present, label in checks:
        if importance and importance > 0:
            weighted_max += importance
            if present is True:
                weighted_points += importance
                reasons.append(f"Has {label}.")
            elif present is False:
                reasons.append(f"Missing {label}, which the renter values.")
            else:
                reasons.append(f"{label.capitalize()} status unknown.")

    if weighted_max == 0:
        return 70, "No specific amenity priorities supplied; default amenities score."

    score = round((weighted_points / weighted_max) * 100)
    return score, " ".join(reasons)


def score_trust(listing: ListingSnapshot) -> tuple[int, str]:
    """Trust/verification, weighted 5%."""
    mapping = {
        "verified": 100,
        "partially_verified": 60,
        "unverified": 20,
    }
    status = listing.host_verification_status or "unverified"
    score = mapping.get(status, 20)
    return score, f"Host verification status: {status.replace('_', ' ')}."


# ---------------------------------------------------------------------------
# Aggregate scoring entrypoint
# ---------------------------------------------------------------------------

def compute_match_score(
    listing: ListingSnapshot,
    prefs: RenterPreferences,
    now: Optional[datetime] = None,
) -> ScoreBreakdown:
    """Compute the full weighted match score for one listing against one
    renter's preferences. Pure function — safe to call from search.service
    in a loop or via a comprehension over candidate listings.
    """
    budget_score, budget_note = score_budget(listing, prefs)
    location_score, location_note = score_location(listing, prefs)
    property_score, property_note = score_property(listing, prefs)
    commute_score, commute_note = score_commute(listing, prefs)
    availability_score, availability_note = score_availability(listing, now)
    amenities_score, amenities_note = score_amenities(listing, prefs)
    trust_score, trust_note = score_trust(listing)

    overall = (
        budget_score * WEIGHTS["budget"]
        + location_score * WEIGHTS["location"]
        + property_score * WEIGHTS["property"]
        + commute_score * WEIGHTS["commute"]
        + availability_score * WEIGHTS["availability"]
        + amenities_score * WEIGHTS["amenities"]
        + trust_score * WEIGHTS["trust"]
    )

    return ScoreBreakdown(
        overall_score=round(overall),
        budget_score=budget_score,
        location_score=location_score,
        property_score=property_score,
        commute_score=commute_score,
        availability_score=availability_score,
        amenities_score=amenities_score,
        trust_score=trust_score,
        notes=[
            budget_note,
            location_note,
            property_note,
            commute_note,
            availability_note,
            amenities_note,
            trust_note,
        ],
    )


def rank_listings(
    listings: list[ListingSnapshot],
    prefs: RenterPreferences,
    now: Optional[datetime] = None,
) -> list[tuple[ListingSnapshot, ScoreBreakdown]]:
    """Score every candidate and return them sorted best-first.

    This is what Backend/search/service.py should call after hard filtering
    (Phase 2) and before returning results to the API layer. Sponsored
    placements must NEVER be blended into this ranked list — they are
    surfaced separately and labelled, per roadmap section 9.
    """
    scored = [(listing, compute_match_score(listing, prefs, now)) for listing in listings]
    scored.sort(key=lambda pair: pair[1].overall_score, reverse=True)
    return scored
