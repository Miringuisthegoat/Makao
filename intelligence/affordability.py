"""
Backend/intelligence/affordability.py

Makao Affordability Engine (MVP Phase 4)

Purpose
-------
Advertised rent is not the real cost of living somewhere. This module estimates
the *total* monthly housing cost a renter should expect — rent + service charge
+ estimated utilities + estimated transport — and compares it against the
renter's stated budget.

Architectural rules honoured here:
- This is deterministic. No LLM involvement.
- Never present an estimate as a guaranteed cost — every output carries an
  `is_estimate` flag and, where relevant, a `confidence` label.
- Degrades gracefully on missing data instead of raising or fabricating precision.

This module does not know about the database. It takes plain dicts/values in
and returns plain dicts out, so it can be unit tested in isolation and reused
from routes, scoring, or the AI explanation layer.
"""

from dataclasses import dataclass, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Tunable defaults (Nairobi MVP baseline estimates)
# ---------------------------------------------------------------------------
# These are intentionally simple, transparent multipliers rather than a
# black-box model. They should move to a config/DB table once Makao has
# enough real utility-bill and transport-cost data to replace them.

# Estimated monthly utilities (water + electricity + gas, KSh) by property
# category. Keys MUST match models.py's PROPERTY_TYPES exactly (underscore
# convention, e.g. "1_bedroom" not "1 bedroom") — a mismatch here silently
# falls through to DEFAULT_UTILITIES_FALLBACK for every property, which is
# what happened before this was aligned to the real schema.
DEFAULT_UTILITIES_BY_BEDROOMS = {
    "bedsitter": 1500,
    "studio": 1500,
    "1_bedroom": 2500,
    "2_bedroom": 3500,
    "3_bedroom": 4500,
    "4plus_bedroom": 6000,
    "apartment": 3000,
    "house": 5000,
    "townhouse": 5500,
    "maisonette": 6000,
}
DEFAULT_UTILITIES_FALLBACK = 3000

# Estimated one-way trip cost (KSh) by transport mode, used to derive a
# monthly transport estimate when no commute-specific fare data exists.
TRANSPORT_MODE_DAILY_ROUND_TRIP_COST = {
    "matatu": 200,       # ~100 KSh each way, rough Nairobi average
    "bus": 160,
    "boda_boda": 300,
    "walking": 0,
    "own_car": 400,      # fuel-equivalent estimate, not fuel price modelling
    "ride_hailing": 700,
}
DEFAULT_TRANSPORT_MODE = "matatu"
WORKING_DAYS_PER_MONTH = 22

# models.py's UserPreferencesUpsert.transport_mode enum (walking|matatu|bus|
# boda|car|mixed) does not match this engine's mode vocabulary (walking|
# matatu|bus|boda_boda|own_car|ride_hailing). Callers passing a raw stored
# preference should run it through this map first so "boda"/"car"/"mixed"
# don't silently fall back to matatu pricing.
PREFERENCE_MODE_TO_ENGINE_MODE = {
    "walking": "walking",
    "matatu": "matatu",
    "bus": "bus",
    "boda": "boda_boda",
    "car": "own_car",
    "mixed": "matatu",  # no dedicated "mixed" profile yet; matatu is the closest baseline
}


def map_preference_transport_mode(preference_mode: Optional[str]) -> Optional[str]:
    """Translate a models.py-style transport_mode into this engine's vocabulary.
    Unrecognized/None input passes through unchanged so the engine's own
    fallback-to-default handling still applies."""
    if preference_mode is None:
        return None
    return PREFERENCE_MODE_TO_ENGINE_MODE.get(preference_mode.strip().lower(), preference_mode)


@dataclass
class AffordabilityResult:
    advertised_rent: float
    service_charge: float
    estimated_utilities: float
    estimated_transport: float
    estimated_monthly_cost: float
    budget: Optional[float]
    budget_difference: Optional[float]
    affordability_score: Optional[int]  # 0-100, None if no budget supplied
    is_estimate: bool
    confidence: str  # "low" | "medium" | "high"
    notes: list

    def to_dict(self) -> dict:
        return asdict(self)


def _estimate_utilities(property_category: Optional[str], provided_utilities: Optional[float]) -> tuple[float, str]:
    """Return (amount, source_note)."""
    if provided_utilities is not None:
        return float(provided_utilities), "utilities: landlord-provided figure"

    category = (property_category or "").strip().lower()
    amount = DEFAULT_UTILITIES_BY_BEDROOMS.get(category, DEFAULT_UTILITIES_FALLBACK)
    return float(amount), f"utilities: estimated from property category '{category or 'unknown'}'"


def _estimate_transport(
    transport_mode: Optional[str],
    commute_minutes: Optional[float],
    provided_transport_cost: Optional[float],
) -> tuple[float, str]:
    """Return (amount, source_note)."""
    if provided_transport_cost is not None:
        return float(provided_transport_cost), "transport: user-provided figure"

    mode = (transport_mode or DEFAULT_TRANSPORT_MODE).strip().lower()
    if mode == "walking" and (commute_minutes or 0) <= 45:
        return 0.0, "transport: walking distance, no fare assumed"

    daily_round_trip = TRANSPORT_MODE_DAILY_ROUND_TRIP_COST.get(mode, TRANSPORT_MODE_DAILY_ROUND_TRIP_COST[DEFAULT_TRANSPORT_MODE])
    monthly = daily_round_trip * WORKING_DAYS_PER_MONTH
    return float(monthly), f"transport: estimated from mode '{mode}' over {WORKING_DAYS_PER_MONTH} working days"


def _score_affordability(estimated_monthly_cost: float, budget: Optional[float]) -> Optional[int]:
    """
    Deterministic affordability score, 0-100.
    100  = comfortably within budget
    50   = right at budget
    0    = double budget or worse
    None = no budget supplied, cannot score
    """
    if not budget or budget <= 0:
        return None

    ratio = estimated_monthly_cost / budget

    if ratio <= 0.85:
        # Comfortably under budget — reward but don't over-reward well below budget,
        # since "way under budget" often means a compromise elsewhere (size, location).
        score = 100 - max(0, (0.85 - ratio)) * 40
    elif ratio <= 1.0:
        # Within budget, tightening as it approaches 100%
        score = 100 - ((ratio - 0.85) / 0.15) * 30
    elif ratio <= 1.5:
        # Over budget, linear penalty down to 0 at 150% of budget
        score = 70 - ((ratio - 1.0) / 0.5) * 70
    else:
        score = 0

    return int(round(max(0, min(100, score))))


def calculate_affordability(
    advertised_rent: float,
    service_charge: float = 0.0,
    property_category: Optional[str] = None,
    utilities_override: Optional[float] = None,
    transport_mode: Optional[str] = None,
    commute_minutes: Optional[float] = None,
    transport_cost_override: Optional[float] = None,
    monthly_budget: Optional[float] = None,
) -> AffordabilityResult:
    """
    Core entry point. Pure function — no DB, no network calls.

    Parameters
    ----------
    advertised_rent : the listing's asking_price
    service_charge : the listing's service_charge (default 0)
    property_category : properties.property_category, used to estimate utilities
        when no explicit figure is available
    utilities_override : if the listing/property already states a utilities
        figure, pass it here to skip estimation
    transport_mode : renter's user_preferences.transport_mode
    commute_minutes : from commute_estimates, used only to decide whether a
        "walking" mode should be treated as free
    transport_cost_override : if a known fare/cost figure exists, pass it here
    monthly_budget : renter's user_preferences.total_housing_budget (or
        rent_budget as a fallback) — optional; scoring is skipped without it

    Returns
    -------
    AffordabilityResult
    """
    if advertised_rent is None or advertised_rent < 0:
        raise ValueError("advertised_rent must be a non-negative number")

    service_charge = float(service_charge or 0.0)

    utilities, utilities_note = _estimate_utilities(property_category, utilities_override)
    transport, transport_note = _estimate_transport(transport_mode, commute_minutes, transport_cost_override)

    estimated_monthly_cost = round(advertised_rent + service_charge + utilities + transport, 2)

    budget_difference = None
    if monthly_budget is not None:
        budget_difference = round(monthly_budget - estimated_monthly_cost, 2)

    score = _score_affordability(estimated_monthly_cost, monthly_budget)

    # Confidence reflects how much of the estimate is real data vs. assumption.
    provided_count = sum([
        utilities_override is not None,
        transport_cost_override is not None,
    ])
    if provided_count == 2:
        confidence = "high"
    elif provided_count == 1:
        confidence = "medium"
    else:
        confidence = "low"

    notes = [utilities_note, transport_note]
    if monthly_budget is None:
        notes.append("budget: not supplied, affordability_score omitted")

    return AffordabilityResult(
        advertised_rent=round(float(advertised_rent), 2),
        service_charge=round(service_charge, 2),
        estimated_utilities=round(utilities, 2),
        estimated_transport=round(transport, 2),
        estimated_monthly_cost=estimated_monthly_cost,
        budget=monthly_budget,
        budget_difference=budget_difference,
        affordability_score=score,
        is_estimate=True,
        confidence=confidence,
        notes=notes,
    )


def calculate_affordability_dict(**kwargs) -> dict:
    """Convenience wrapper returning a plain dict — handy for route handlers
    and for feeding straight into an API response / scoring module."""
    return calculate_affordability(**kwargs).to_dict()
