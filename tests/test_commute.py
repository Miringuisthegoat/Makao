"""
Backend/tests/test_commute.py

Run with: pytest Backend/tests/test_commute.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from intelligence.commute import estimate_commute, format_commute_range, to_commute_estimates_row

# Rough real coordinates for testing
NAIROBI_CBD = (-1.2864, 36.8172)
UPPER_HILL = (-1.2966, 36.8119)
KITENGELA = (-1.4649, 36.9569)  # far outside CBD, should be a long commute


def test_short_distance_matatu_estimate_is_reasonable():
    result = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
    )
    assert result.estimated_minutes > 0
    assert result.estimated_minutes_low <= result.estimated_minutes <= result.estimated_minutes_high
    assert result.is_estimate is True
    assert result.method == "haversine_estimate"


def test_long_distance_takes_longer_than_short_distance():
    short = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
    )
    long = estimate_commute(
        origin_lat=KITENGELA[0], origin_lon=KITENGELA[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
    )
    assert long.estimated_minutes > short.estimated_minutes


def test_walking_is_slower_than_matatu_for_same_route():
    matatu = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
    )
    walking = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="walking",
    )
    assert walking.estimated_minutes > matatu.estimated_minutes


def test_unknown_mode_falls_back_to_default():
    result = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="teleporter",
    )
    assert result.transport_mode == "matatu"  # DEFAULT_MODE


def test_preference_mode_names_map_to_engine_mode_names():
    from intelligence.commute import map_preference_transport_mode
    assert map_preference_transport_mode("boda") == "boda_boda"
    assert map_preference_transport_mode("car") == "own_car"
    assert map_preference_transport_mode("mixed") == "matatu"


def test_missing_coordinates_raises():
    with pytest.raises(ValueError):
        estimate_commute(
            origin_lat=None, origin_lon=36.8,
            destination_lat=-1.28, destination_lon=36.8,
        )


def test_format_commute_range_produces_readable_string():
    result = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
    )
    formatted = format_commute_range(result)
    assert "-" in formatted or formatted.startswith("~")
    assert "minutes" in formatted


def test_row_shape_matches_commute_estimates_table():
    result = estimate_commute(
        origin_lat=UPPER_HILL[0], origin_lon=UPPER_HILL[1],
        destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1],
        transport_mode="matatu",
        property_id=42,
        destination_name="CBD Office",
    )
    row = to_commute_estimates_row(result, destination_lat=NAIROBI_CBD[0], destination_lon=NAIROBI_CBD[1])
    expected_keys = {
        "property_id", "destination_name", "destination_latitude",
        "destination_longitude", "transport_mode", "estimated_minutes",
    }
    assert set(row.keys()) == expected_keys
    assert row["property_id"] == 42
    assert row["destination_name"] == "CBD Office"
