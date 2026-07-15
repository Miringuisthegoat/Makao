"""
config.py — Makao Rental Platform
===================================
OWNER-MANAGED PRICING CONFIGURATION

This is the single source of truth for all pricing on the platform.
All prices are set here by the owner — they are never hardcoded anywhere
else in the codebase and are never exposed in version control with real values.

HOW TO SET PRICES:
  1. Open this file on the production server
  2. Replace the 0 values with your actual KES amounts
  3. Save and restart the FastAPI server
  4. Prices reflect immediately across the entire platform

CURRENCY: All prices are in KES (Kenyan Shillings).

DO NOT commit real prices to git — keep this file in .gitignore
or use environment variable overrides (see bottom of file).
"""

import os
from typing import Any, Dict


# ===========================================================================
# ── LISTING PACKAGES ─────────────────────────────────────────────────────────
# ===========================================================================
# Each package is a one-time payment that activates a listing slot.
#
# Field reference:
#   price          — amount in KES (set by owner)
#   listings       — how many listings this payment covers
#   photos         — max photos per listing (-1 = unlimited)
#   chatbot        — listing is eligible for chatbot recommendations
#   featured       — listing is featured on the homepage automatically
#   verified_badge — host shows a "Verified Agency" badge
# ===========================================================================

PRICING: Dict[str, Dict[str, Any]] = {

    "landlord": {
        "name": "Landlord Package",
        "price": 0,            # ← Owner sets this (KES)
        "listings": 1,
        "photos": 10,
        "chatbot": True,
        "featured": False,
        "verified_badge": False,
        "description": (
            "Perfect for individual landlords. "
            "List one property with up to 10 photos and reach thousands of tenants."
        ),
    },

    "agency": {
        "name": "Agency Package",
        "price": 0,            # ← Owner sets this (KES)
        "listings": 10,
        "photos": -1,          # -1 = unlimited
        "chatbot": True,
        "featured": True,
        "verified_badge": True,
        "description": (
            "Built for real estate agencies. "
            "List up to 10 properties, appear on the homepage, "
            "get the Verified Agency badge, and priority in AI recommendations."
        ),
    },

}


# ===========================================================================
# ── BOOSTER ADD-ONS ──────────────────────────────────────────────────────────
# ===========================================================================
# Boosters are optional upgrades hosts can purchase from their dashboard
# at any time after their listing goes live.
#
# Field reference:
#   price          — amount in KES (set by owner)
#   duration_days  — how long the booster stays active (None = no expiry)
#   description    — shown on the pricing and dashboard pages
# ===========================================================================

BOOSTERS: Dict[str, Dict[str, Any]] = {

    "homepage_feature": {
        "name": "Homepage Feature",
        "price": 0,            # ← Owner sets this (KES)
        "duration_days": 7,
        "description": (
            "Pin your listing to the Makao homepage for 7 days. "
            "Maximum exposure to every visitor."
        ),
    },

    "chatbot_priority": {
        "name": "Chatbot Priority",
        "price": 0,            # ← Owner sets this (KES)
        "duration_days": None, # Active until manually removed or listing expires
        "description": (
            "Your listing is recommended first by the AI chatbot "
            "whenever a visitor's preferences are a match."
        ),
    },

    "refresh_listing": {
        "name": "Refresh Listing",
        "price": 0,            # ← Owner sets this (KES)
        "duration_days": None, # One-time action — bumps rank immediately
        "description": (
            "Bump your listing back to the top of search results. "
            "Useful if your listing has been buried by newer properties."
        ),
    },

    "extend_visibility": {
        "name": "Extend Visibility",
        "price": 0,            # ← Owner sets this (KES)
        "duration_days": 30,
        "description": (
            "Keep your listing highlighted in search results for 30 more days. "
            "Stands out with a special badge."
        ),
    },

}


# ===========================================================================
# ── PLATFORM SETTINGS ────────────────────────────────────────────────────────
# ===========================================================================
# General operational settings. Adjust as needed.
# ===========================================================================

PLATFORM: Dict[str, Any] = {

    # Currency shown everywhere on the site
    "currency": "KES",
    "currency_symbol": "KSh",

    # Maximum photos enforced in the backend for each package type.
    # Mirrors PRICING values above — kept here for easy lookup in listings.py.
    "max_photos": {
        "landlord": 10,
        "agency": -1,          # unlimited
    },

    # Maximum active listings per package purchase
    "max_listings": {
        "landlord": 1,
        "agency": 10,
    },

    # Visibility rank bump applied when a 'refresh_listing' booster is activated
    "refresh_rank_bump": 500,

    # How many listings appear per page on the browse page
    "listings_per_page": 20,

    # How many featured listings appear on the homepage
    "homepage_featured_count": 6,

    # How many chat history messages are sent to Groq for context
    "chat_history_limit": 20,

    # How many candidate listings are passed to OpenAI GPT-4o for matching
    "ai_match_candidate_limit": 30,

    # Number of top matches the chatbot presents to the visitor
    "ai_match_results_count": 5,

    # JWT token expiry in hours (mirrors .env JWT_EXPIRE_HOURS)
    "jwt_expire_hours": int(os.getenv("JWT_EXPIRE_HOURS", 24)),

}


# ===========================================================================
# ── HELPER FUNCTIONS ─────────────────────────────────────────────────────────
# ===========================================================================

def get_package(package_type: str) -> Dict[str, Any]:
    """
    Return the full package config dict for a given package type.
    Raises KeyError if package_type is not 'landlord' or 'agency'.

    Usage (in payments.py):
        pkg = get_package("landlord")
        amount = pkg["price"]
    """
    if package_type not in PRICING:
        raise KeyError(
            f"Unknown package type '{package_type}'. "
            f"Valid options: {list(PRICING.keys())}"
        )
    return PRICING[package_type]


def get_booster(booster_type: str) -> Dict[str, Any]:
    """
    Return the full booster config dict for a given booster key.
    Raises KeyError if the booster_type is not recognised.

    Usage (in payments.py):
        booster = get_booster("homepage_feature")
        amount  = booster["price"]
        days    = booster["duration_days"]
    """
    if booster_type not in BOOSTERS:
        raise KeyError(
            f"Unknown booster type '{booster_type}'. "
            f"Valid options: {list(BOOSTERS.keys())}"
        )
    return BOOSTERS[booster_type]


def get_price(package_or_booster_type: str) -> float:
    """
    Convenience: return just the price (float, KES) for any package or booster.
    Checks PRICING first, then BOOSTERS.

    Usage (in payments.py):
        amount = get_price("agency")
        amount = get_price("homepage_feature")
    """
    if package_or_booster_type in PRICING:
        return float(PRICING[package_or_booster_type]["price"])
    if package_or_booster_type in BOOSTERS:
        return float(BOOSTERS[package_or_booster_type]["price"])
    raise KeyError(f"No price found for '{package_or_booster_type}'.")


def get_max_photos(package_type: str) -> int:
    """
    Return the photo limit for a package (-1 = unlimited).
    Used in listings.py to enforce photo caps before saving.
    """
    return PLATFORM["max_photos"].get(package_type, 10)


def get_max_listings(package_type: str) -> int:
    """
    Return the listing cap for a package purchase.
    Used in listings.py to enforce limits before creating a new listing.
    """
    return PLATFORM["max_listings"].get(package_type, 1)


def all_pricing_for_api() -> Dict[str, Any]:
    """
    Return a serialisable dict of all packages and boosters for the
    GET /pricing endpoint. Strips the internal 'description' key
    to keep the public API clean — descriptions are in models.py.

    Returns exactly the shape expected by PricingResponse in models.py.
    """
    packages = {
        key: {
            "name": pkg["name"],
            "price": pkg["price"],
            "listings": pkg["listings"],
            "photos": pkg["photos"],
            "chatbot": pkg["chatbot"],
            "featured": pkg["featured"],
            "verified_badge": pkg["verified_badge"],
        }
        for key, pkg in PRICING.items()
    }

    boosters = {
        key: {
            "name": b["name"],
            "price": b["price"],
            "duration_days": b["duration_days"],
        }
        for key, b in BOOSTERS.items()
    }

    return {"packages": packages, "boosters": boosters}