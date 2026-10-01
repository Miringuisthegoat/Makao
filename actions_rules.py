"""
actions_rules.py — Phase 6 pure business rules (no DB, no network, no FastAPI).

Everything here is unit-tested in tests/test_phase6_actions.py.
Nothing in this module reads payment status: contact / viewing / save / share
must never be payment-gated.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

# ---------------------------------------------------------------------------
# Viewing request state machine
# ---------------------------------------------------------------------------
HOST_TRANSITIONS = {
    "requested": {"confirmed", "cancelled"},
    "confirmed": {"completed", "no_show", "cancelled"},
}
RENTER_TRANSITIONS = {
    "requested": {"cancelled"},
    "confirmed": {"cancelled"},
}
NEEDS_TIME_PASSED = {"completed", "no_show"}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def transition_error(
    actor: str, current: str, new: str,
    requested_time: Optional[datetime] = None, now: Optional[datetime] = None,
) -> Optional[str]:
    """Return an error message, or None when the transition is allowed."""
    table = HOST_TRANSITIONS if actor == "host" else RENTER_TRANSITIONS if actor == "renter" else {}
    if new not in table.get(current, set()):
        return f"A viewing that is '{current}' cannot be changed to '{new}'."
    if new in NEEDS_TIME_PASSED and requested_time is not None:
        now = now or datetime.now(timezone.utc)
        if _aware(requested_time) > now:
            return "This can only be marked after the scheduled viewing time."
    return None


def validate_requested_time(
    dt: datetime, now: Optional[datetime] = None,
    min_lead_minutes: int = 60, max_days: int = 60,
) -> Optional[str]:
    now = now or datetime.now(timezone.utc)
    dt = _aware(dt)
    if dt < now + timedelta(minutes=min_lead_minutes):
        return "Please choose a viewing time at least 1 hour from now."
    if dt > now + timedelta(days=max_days):
        return "Please choose a viewing time within the next 60 days."
    return None


# ---------------------------------------------------------------------------
# Phone / WhatsApp / share helpers
# ---------------------------------------------------------------------------
def ke_phone_digits(phone: Optional[str]) -> Optional[str]:
    """Normalise a Kenyan MOBILE number to '254XXXXXXXXX' digits, or None if invalid."""
    if not phone:
        return None
    d = re.sub(r"\D", "", phone)
    if d.startswith("0") and len(d) == 10:
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "17":
        d = "254" + d
    # Kenyan mobile numbers only (07xx / 01xx) — WhatsApp and SMS need mobiles.
    return d if d.startswith("254") and len(d) == 12 and d[3] in "17" else None


def tel_url(phone: Optional[str]) -> Optional[str]:
    d = ke_phone_digits(phone)
    return f"tel:+{d}" if d else None


def whatsapp_url(phone: Optional[str], text: str = "") -> Optional[str]:
    d = ke_phone_digits(phone)
    if not d:
        return None
    q = f"?text={urllib.parse.quote(text)}" if text else ""
    return f"https://wa.me/{d}{q}"


def build_share(listing_id: int, title: str, asking_price: float,
                neighbourhood: Optional[str], base_url: str) -> Dict[str, str]:
    url = f"{base_url.rstrip('/')}/listing.html?id={int(listing_id)}"
    where = f" in {neighbourhood}" if neighbourhood else ""
    text = f"{title}{where} — KSh {int(asking_price):,}/month (advertised rent). See it on Makao: {url}"
    return {"url": url, "text": text,
            "whatsapp_url": f"https://wa.me/?text={urllib.parse.quote(text)}"}


# ---------------------------------------------------------------------------
# Honest labels (never claim more than the data shows)
# ---------------------------------------------------------------------------
def freshness_label(status: Optional[str], confirmed_at: Optional[datetime],
                    now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if status == "unavailable":
        return "Marked unavailable"
    if status != "available" or confirmed_at is None:
        return "Availability not confirmed"
    days = (now - _aware(confirmed_at)).total_seconds() / 86400
    if days <= 1:
        return "Available — confirmed today"
    if days <= 14:
        return f"Available — confirmed {int(days)} days ago"
    return f"Availability last confirmed {int(days)} days ago — may be out of date"


VERIFICATION_LABELS = {
    "verified": "Verified host",
    "partially_verified": "Partially verified host",
    "unverified": "Not yet verified",
}


def verification_label(status: Optional[str]) -> str:
    return VERIFICATION_LABELS.get(status or "unverified", "Not yet verified")


# ---------------------------------------------------------------------------
# Amenity flags for scoring: True when listed, None (unknown) otherwise —
# an unlisted amenity is NEVER recorded as "absent".
# ---------------------------------------------------------------------------
def amenity_flags(amenities) -> Dict[str, Optional[bool]]:
    if isinstance(amenities, str):  # asyncpg returns JSONB as raw text
        try:
            amenities = json.loads(amenities)
        except ValueError:
            amenities = []
    a = {str(x).lower() for x in (amenities or [])}
    def has(*keys): return True if a & set(keys) else None
    return {
        "has_water": has("borehole", "water_storage"),
        "has_backup_power": has("backup_power", "solar_power"),
        "has_internet_ready": has("wifi", "fibre"),
        "has_security": has("security", "cctv"),
    }


# ---------------------------------------------------------------------------
# In-memory sliding-window rate limiter (single-process; swap for Redis later)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float):
        self.max, self.window = max_events, window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        return True
