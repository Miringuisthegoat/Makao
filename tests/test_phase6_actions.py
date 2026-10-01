"""Run from Backend/:  pytest tests/test_phase6_actions.py -v"""
from datetime import datetime, timedelta, timezone

import actions_rules as r

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_host_transitions():
    assert r.transition_error("host", "requested", "confirmed") is None
    assert r.transition_error("host", "requested", "completed")          # must confirm first
    assert r.transition_error("host", "cancelled", "confirmed")          # terminal
    assert r.transition_error("host", "confirmed", "no_show", NOW - timedelta(hours=1), NOW) is None


def test_completed_requires_time_passed():
    assert r.transition_error("host", "confirmed", "completed", NOW + timedelta(hours=2), NOW)
    assert r.transition_error("host", "confirmed", "completed", NOW - timedelta(minutes=1), NOW) is None


def test_renter_can_only_cancel():
    assert r.transition_error("renter", "requested", "cancelled") is None
    assert r.transition_error("renter", "confirmed", "cancelled") is None
    assert r.transition_error("renter", "requested", "confirmed")
    assert r.transition_error("renter", "completed", "cancelled")
    assert r.transition_error("stranger", "requested", "cancelled")


def test_requested_time_window():
    assert r.validate_requested_time(NOW + timedelta(minutes=30), NOW)
    assert r.validate_requested_time(NOW + timedelta(hours=2), NOW) is None
    assert r.validate_requested_time(NOW + timedelta(days=61), NOW)
    assert r.validate_requested_time(datetime(2026, 10, 3, 9, 0), NOW) is None  # naive -> UTC


def test_phone_normalisation():
    for raw in ("0712345678", "+254712345678", "254712345678", "712345678", "0712 345 678"):
        assert r.ke_phone_digits(raw) == "254712345678"
    for bad in (None, "", "12345", "0612345678", "+1 555 123 4567"):
        assert r.ke_phone_digits(bad) is None


def test_links():
    assert r.tel_url("0712345678") == "tel:+254712345678"
    assert r.whatsapp_url("0712345678", "Hi & bye").startswith("https://wa.me/254712345678?text=Hi%20%26%20bye")
    assert r.whatsapp_url("bad") is None


def test_share_payload():
    s = r.build_share(7, "Cosy 1BR", 25000, "Kilimani", "https://makao.ke/")
    assert s["url"] == "https://makao.ke/listing.html?id=7"
    assert "KSh 25,000" in s["text"] and "advertised rent" in s["text"] and "Kilimani" in s["text"]
    assert s["whatsapp_url"].startswith("https://wa.me/?text=")


def test_freshness_never_overclaims():
    assert r.freshness_label("available", NOW - timedelta(hours=3), NOW) == "Available — confirmed today"
    assert r.freshness_label("available", NOW - timedelta(days=5), NOW) == "Available — confirmed 5 days ago"
    assert "out of date" in r.freshness_label("available", NOW - timedelta(days=30), NOW)
    assert r.freshness_label("available", None, NOW) == "Availability not confirmed"
    assert r.freshness_label(None, None, NOW) == "Availability not confirmed"
    assert r.freshness_label("uncertain", NOW, NOW) == "Availability not confirmed"
    assert r.freshness_label("unavailable", NOW, NOW) == "Marked unavailable"


def test_verification_label_defaults_to_unverified():
    assert r.verification_label(None) == "Not yet verified"
    assert r.verification_label("verified") == "Verified host"
    assert r.verification_label("garbage") == "Not yet verified"


def test_amenity_flags_true_or_unknown_never_false():
    f = r.amenity_flags(["Security", "borehole"])
    assert f["has_security"] is True and f["has_water"] is True
    assert f["has_backup_power"] is None and f["has_internet_ready"] is None
    assert r.amenity_flags('["wifi"]')["has_internet_ready"] is True   # raw JSON text from asyncpg
    assert r.amenity_flags("not json")["has_security"] is None
    assert r.amenity_flags(None)["has_water"] is None


def test_rate_limiter_window():
    rl = r.RateLimiter(2, 10)
    assert rl.allow("a", 0) and rl.allow("a", 1) and not rl.allow("a", 2)
    assert rl.allow("b", 2)                      # separate key
    assert rl.allow("a", 11.5)                   # window slid
