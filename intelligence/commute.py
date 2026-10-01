"""
Backend/intelligence/commute.py

Makao Commute Engine (MVP Phase 4)

Purpose
-------
Estimate commute time between a property and a renter's destination
(usually their workplace). MVP-scope: no live routing API dependency —
uses haversine distance plus mode-specific speed/overhead assumptions.
Returns a *range*, never a false-precision single number, unless a real
routing provider is wired in later.

Architectural rules honoured here:
- Deterministic, no LLM.
- Never implies real-time/live traffic data unless an actual provider
  supplies it (none is wired in for MVP).
- Designed to be cached: `commute_estimates` table mirrors this module's
  output shape (property_id, destination, mode, estimated_minutes, calculated_at).
"""

import math
from dataclasses import dataclass, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Mode assumptions (Nairobi MVP baseline)
# ---------------------------------------------------------------------------
# average_speed_kmh: effective average speed including typical Nairobi
# congestion/stop time, not free-flow speed.
# fixed_overhead_minutes: waiting time, walking to/from stage, etc.
MODE_PROFILES = {
    "walking": {"average_speed_kmh": 4.5, "fixed_overhead_minutes": 0},
    "matatu": {"average_speed_kmh": 18, "fixed_overhead_minutes": 10},
    "bus": {"average_speed_kmh": 16, "fixed_overhead_minutes": 12},
    "boda_boda": {"average_speed_kmh": 25, "fixed_overhead_minutes": 3},
    "own_car": {"average_speed_kmh": 22, "fixed_overhead_minutes": 5},
    "ride_hailing": {"average_speed_kmh": 22, "fixed_overhead_minutes": 7},
}
DEFAULT_MODE = "matatu"

# See the matching map in affordability.py — models.py's transport_mode enum
# (walking|matatu|bus|boda|car|mixed) doesn't match this module's mode keys
# (walking|matatu|bus|boda_boda|own_car|ride_hailing). Same translation here
# so a raw stored preference doesn't silently mispriced-fallback.
PREFERENCE_MODE_TO_ENGINE_MODE = {
    "walking": "walking",
    "matatu": "matatu",
    "bus": "bus",
    "boda": "boda_boda",
    "car": "own_car",
    "mixed": "matatu",
}


def map_preference_transport_mode(preference_mode: Optional[str]) -> Optional[str]:
    """Translate a models.py-style transport_mode into this engine's vocabulary."""
    if preference_mode is None:
        return None
    return PREFERENCE_MODE_TO_ENGINE_MODE.get(preference_mode.strip().lower(), preference_mode)

# A route is never a straight line — this factor approximates real road
# distance vs. straight-line (haversine) distance for Nairobi's road grid.
ROAD_DISTANCE_FACTOR = 1.35

# Uncertainty band applied around the point estimate, e.g. 35-50 minutes
RANGE_SPREAD_RATIO = 0.20


@dataclass
class CommuteEstimate:
    property_id: Optional[int]
    destination_name: Optional[str]
    transport_mode: str
    distance_km: float
    estimated_minutes: int
    estimated_minutes_low: int
    estimated_minutes_high: int
    is_estimate: bool
    method: str  # "haversine_estimate" | "cached" | "provider:<name>"
    notes: list

    def to_dict(self) -> dict:
        return asdict(self)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two coordinates, in kilometres."""
    R = 6371.0  # Earth radius, km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def estimate_commute(
    origin_lat: float,
    origin_lon: float,
    destination_lat: float,
    destination_lon: float,
    transport_mode: Optional[str] = None,
    property_id: Optional[int] = None,
    destination_name: Optional[str] = None,
) -> CommuteEstimate:
    """
    Core entry point. Pure function — no DB, no network calls.

    Coordinates required for both origin (property) and destination
    (renter's workplace or other point of interest). Callers are
    responsible for supplying valid lat/lon — this module does not
    do geocoding.
    """
    for name, value in [
        ("origin_lat", origin_lat), ("origin_lon", origin_lon),
        ("destination_lat", destination_lat), ("destination_lon", destination_lon),
    ]:
        if value is None:
            raise ValueError(f"{name} is required to estimate commute")

    requested_mode = (transport_mode or DEFAULT_MODE).strip().lower()
    if requested_mode in MODE_PROFILES:
        mode = requested_mode
    else:
        mode = DEFAULT_MODE  # unknown mode -> fall back to default, both for calc and reporting
    profile = MODE_PROFILES[mode]

    straight_line_km = _haversine_km(origin_lat, origin_lon, destination_lat, destination_lon)
    road_km = straight_line_km * ROAD_DISTANCE_FACTOR

    travel_minutes = (road_km / profile["average_speed_kmh"]) * 60
    point_estimate = travel_minutes + profile["fixed_overhead_minutes"]

    spread = point_estimate * RANGE_SPREAD_RATIO
    low = max(1, round(point_estimate - spread))
    high = round(point_estimate + spread)

    notes = [
        f"estimated via straight-line distance x {ROAD_DISTANCE_FACTOR} road factor, "
        f"mode '{mode}' avg speed {profile['average_speed_kmh']} km/h",
        "not live-traffic data; treat as a typical-conditions estimate",
    ]
    if requested_mode != mode:
        notes.append(f"unrecognized transport_mode '{requested_mode}', fell back to default '{DEFAULT_MODE}'")

    return CommuteEstimate(
        property_id=property_id,
        destination_name=destination_name,
        transport_mode=mode,
        distance_km=round(road_km, 2),
        estimated_minutes=int(round(point_estimate)),
        estimated_minutes_low=int(low),
        estimated_minutes_high=int(high),
        is_estimate=True,
        method="haversine_estimate",
        notes=notes,
    )


def estimate_commute_dict(**kwargs) -> dict:
    """Convenience wrapper returning a plain dict."""
    return estimate_commute(**kwargs).to_dict()


def format_commute_range(estimate: CommuteEstimate) -> str:
    """Human-readable range string, e.g. '35-50 minutes', for property pages."""
    if estimate.estimated_minutes_low == estimate.estimated_minutes_high:
        return f"~{estimate.estimated_minutes_low} minutes"
    return f"{estimate.estimated_minutes_low}-{estimate.estimated_minutes_high} minutes"


# ---------------------------------------------------------------------------
# Cache-shape helper — mirrors the `commute_estimates` table so a route
# handler can write straight to DB without reshaping the dict.
# ---------------------------------------------------------------------------
def to_commute_estimates_row(estimate: CommuteEstimate, destination_lat: float, destination_lon: float) -> dict:
    """
    Shape a CommuteEstimate as a `commute_estimates` table row (minus id/
    calculated_at, which the DB layer should set).
    """
    return {
        "property_id": estimate.property_id,
        "destination_name": estimate.destination_name,
        "destination_latitude": destination_lat,
        "destination_longitude": destination_lon,
        "transport_mode": estimate.transport_mode,
        "estimated_minutes": estimate.estimated_minutes,
    }
