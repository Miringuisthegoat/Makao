"""
listings.py — Makao Rental Platform
=====================================
All business logic for property listings:
  - Create a new listing (inactive until payment confirmed)
  - Read listings (single, paginated browse, host-owned)
  - Update listing fields
  - Delete / deactivate a listing
  - Activate a listing after payment confirmation
  - Photo limit enforcement per package type
  - Visibility ranking helpers used by the AI matcher

This module only contains business logic.
All raw SQL / DB calls go through database.py.
All request/response shapes come from models.py.
All pricing / package limits come from config.py.

Exposed functions (called from main.py route handlers):
  create_listing(host_id, data)             → ListingResponse
  get_listing(listing_id)                   → ListingResponse | None
  get_listings(filters)                     → ListingsPageResponse
  get_host_listings(host_id)                → List[ListingResponse]
  update_listing(listing_id, host_id, data) → ListingResponse
  delete_listing(listing_id, host_id)       → OKResponse
  activate_listing(listing_id)              → OKResponse
  deactivate_listing(listing_id, host_id)   → OKResponse
  bump_listing(listing_id)                  → OKResponse
  get_featured_listings(limit)              → List[ListingCardResponse]
  get_listings_for_matcher(filters)         → List[dict]
"""

from __future__ import annotations

import math
from typing import List, Optional

from fastapi import HTTPException, status

import database as db
from config import PRICING
from models import (
    ListingCardResponse,
    ListingCreate,
    ListingFilters,
    ListingResponse,
    ListingsPageResponse,
    ListingUpdate,
    OKResponse,
)


# ---------------------------------------------------------------------------
# ── CONSTANTS ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

# Visibility rank assigned to new listings by package type.
# Higher number = surfaced first in browse and AI matcher results.
# Agency and boosted listings will be ranked higher by the payment / booster
# logic in payments.py; these are the baseline starting ranks.
_BASE_RANK: dict[str, int] = {
    "agency": 20,
    "landlord": 10,
}

# Maximum photos allowed per package type.
# -1 in PRICING config means unlimited; we cap at a safe server limit.
_MAX_PHOTOS_HARD_LIMIT = 50


# ---------------------------------------------------------------------------
# ── HELPERS ─────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _photo_limit_for_package(package_type: str) -> int:
    """
    Return the maximum number of photos allowed for a package.
    Reads from config.py; -1 = unlimited (capped by hard limit).
    """
    limit = PRICING.get(package_type, {}).get("photos", 10)
    if limit == -1:
        return _MAX_PHOTOS_HARD_LIMIT
    return limit


def _enforce_photo_limit(photos: List[str], package_type: str) -> None:
    """
    Raise 400 if the supplied photo list exceeds the package allowance.
    Called on both create and update.
    """
    limit = _photo_limit_for_package(package_type)
    if len(photos) > limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The {package_type} package allows a maximum of {limit} photos. "
                f"You supplied {len(photos)}."
            ),
        )


def _assert_listing_owner(listing: dict, host_id: int) -> None:
    """
    Raise 403 if the listing does not belong to host_id.
    listing is the raw dict row returned by database.py.
    """
    if listing["host_id"] != host_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this listing.",
        )


def _row_to_listing_response(row: dict) -> ListingResponse:
    """Convert a raw DB row dict → ListingResponse Pydantic model."""
    return ListingResponse(
        id=row["id"],
        host_id=row["host_id"],
        title=row["title"],
        description=row.get("description"),
        location=row.get("location"),
        city=row["city"],
        county=row["county"],
        price_per_month=float(row["price_per_month"]),
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        size_sqft=row.get("size_sqft"),
        amenities=row.get("amenities") or [],
        photos=row.get("photos") or [],
        is_available=bool(row["is_available"]),
        is_featured=bool(row["is_featured"]),
        package_type=row["package_type"],
        visibility_rank=row["visibility_rank"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        host_name=row.get("host_name"),
        host_account_type=row.get("host_account_type"),
        host_photo=row.get("host_photo"),
    )


def _row_to_card_response(row: dict) -> ListingCardResponse:
    """Convert a raw DB row dict → lightweight ListingCardResponse."""
    return ListingCardResponse(
        id=row["id"],
        title=row["title"],
        city=row["city"],
        county=row["county"],
        price_per_month=float(row["price_per_month"]),
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        photos=row.get("photos") or [],
        is_featured=bool(row["is_featured"]),
        package_type=row["package_type"],
        host_name=row.get("host_name"),
        host_account_type=row.get("host_account_type"),
    )


# ---------------------------------------------------------------------------
# ── CREATE ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def create_listing(host_id: int, data: ListingCreate) -> ListingResponse:
    """
    Create a new listing for a host.

    The listing is created with is_available=False (inactive).
    It will only go live after payment is confirmed by payments.py
    calling activate_listing().

    Enforces:
      - Photo count limit for the chosen package (from config.py)
      - Package listing quota: the host must not exceed allowed listings
        for their current unpaid / paid count (enforced in DB layer)
    """
    # 1. Validate photo count against the package config
    _enforce_photo_limit(data.photos, data.package_type)

    # 2. Determine starting visibility rank from package type
    visibility_rank = _BASE_RANK.get(data.package_type, 10)

    # 3. Set featured flag based on package (agency listings start featured)
    is_featured = PRICING.get(data.package_type, {}).get("featured", False)

    # 4. Write to DB (inactive — payment not yet confirmed)
    listing_id = await db.create_listing(
        host_id=host_id,
        title=data.title,
        description=data.description,
        location=data.location,
        city=data.city,
        county=data.county,
        price_per_month=data.price_per_month,
        bedrooms=data.bedrooms,
        bathrooms=data.bathrooms,
        size_sqft=data.size_sqft,
        amenities=data.amenities,
        photos=data.photos,
        package_type=data.package_type,
        is_available=False,       # inactive until payment confirmed
        is_featured=is_featured,
        visibility_rank=visibility_rank,
    )

    # 5. Fetch and return the created listing
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Listing was created but could not be retrieved.",
        )
    return _row_to_listing_response(row)


# ---------------------------------------------------------------------------
# ── READ — SINGLE ────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def get_listing(listing_id: int) -> ListingResponse:
    """
    Return full details for a single listing.
    Raises 404 if not found.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )
    return _row_to_listing_response(row)


# ---------------------------------------------------------------------------
# ── READ — PAGINATED BROWSE ──────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def get_listings(filters: ListingFilters) -> ListingsPageResponse:
    """
    Return a paginated, filtered list of ACTIVE listings for the browse page.

    Ordering:
      1. visibility_rank DESC (featured / boosted listings first)
      2. created_at DESC (newest within same rank tier)

    Only is_available=True listings are returned to public visitors.
    """
    rows, total = await db.get_listings_paginated(
        county=filters.county,
        city=filters.city,
        min_price=filters.min_price,
        max_price=filters.max_price,
        bedrooms=filters.bedrooms,
        bathrooms=filters.bathrooms,
        amenities=filters.amenities,
        page=filters.page,
        page_size=filters.page_size,
        only_available=True,
    )

    pages = math.ceil(total / filters.page_size) if total > 0 else 1

    return ListingsPageResponse(
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        pages=pages,
        listings=[_row_to_card_response(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# ── READ — HOST DASHBOARD ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def get_host_listings(host_id: int) -> List[ListingResponse]:
    """
    Return ALL listings (active and inactive) belonging to a host.
    Used on the host dashboard and add-listing flow.
    """
    rows = await db.get_listings_by_host(host_id)
    return [_row_to_listing_response(r) for r in rows]


# ---------------------------------------------------------------------------
# ── READ — HOMEPAGE FEATURED ─────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def get_featured_listings(limit: int = 8) -> List[ListingCardResponse]:
    """
    Return the top N featured, available listings for the homepage.
    Agency-package and homepage-booster listings appear here.
    Ordered by visibility_rank DESC then created_at DESC.
    """
    rows = await db.get_featured_listings(limit=limit)
    return [_row_to_card_response(r) for r in rows]


# ---------------------------------------------------------------------------
# ── READ — AI MATCHER FEED ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def get_listings_for_matcher(
    county: Optional[str] = None,
    city: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    bedrooms: Optional[int] = None,
    amenities: Optional[List[str]] = None,
    limit: int = 50,
) -> List[dict]:
    """
    Return a broad set of active listings as plain dicts for the AI matcher.
    matcher.py (GPT-4o) will further rank and explain these to the visitor.

    Agency and booster-prioritised listings come first (visibility_rank DESC).
    Limit is kept generous so GPT-4o has enough candidates to work with.
    """
    rows = await db.get_listings_for_matcher(
        county=county,
        city=city,
        min_price=min_price,
        max_price=max_price,
        bedrooms=bedrooms,
        amenities=amenities,
        limit=limit,
    )
    return rows  # raw dicts — matcher.py serialises them as needed


# ---------------------------------------------------------------------------
# ── UPDATE ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def update_listing(
    listing_id: int,
    host_id: int,
    data: ListingUpdate,
) -> ListingResponse:
    """
    Partially update a listing.
    Only the host who owns the listing may update it.
    Photo count is re-validated if photos are supplied.
    """
    # 1. Fetch existing row
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )

    # 2. Ownership check
    _assert_listing_owner(row, host_id)

    # 3. Validate new photos against the existing package type
    if data.photos is not None:
        _enforce_photo_limit(data.photos, row["package_type"])

    # 4. Build update payload — only non-None fields
    updates = data.model_dump(exclude_none=True)
    if not updates:
        # Nothing to update — return the current listing unchanged
        return _row_to_listing_response(row)

    # 5. Persist to DB
    await db.update_listing(listing_id, updates)

    # 6. Return updated listing
    updated_row = await db.get_listing_by_id(listing_id)
    return _row_to_listing_response(updated_row)


# ---------------------------------------------------------------------------
# ── DELETE ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def delete_listing(listing_id: int, host_id: int) -> OKResponse:
    """
    Permanently delete a listing.
    Only the listing's host may delete it.
    Active boosters on this listing are also cleaned up by the DB layer.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )
    _assert_listing_owner(row, host_id)
    await db.delete_listing(listing_id)
    return OKResponse(message=f"Listing {listing_id} deleted successfully.")


# ---------------------------------------------------------------------------
# ── ACTIVATE / DEACTIVATE ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def activate_listing(listing_id: int) -> OKResponse:
    """
    Mark a listing as active (is_available=True).
    Called by payments.py after payment confirmation — not directly by host.
    Also boosts visibility_rank to signal freshness.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )

    # Bump visibility rank on activation to surface it in results
    new_rank = _BASE_RANK.get(row["package_type"], 10) + 5

    await db.update_listing(
        listing_id,
        {
            "is_available": True,
            "visibility_rank": new_rank,
        },
    )
    return OKResponse(message=f"Listing {listing_id} is now live.")


async def deactivate_listing(listing_id: int, host_id: int) -> OKResponse:
    """
    Pause a listing (is_available=False) without deleting it.
    Host can re-activate from the dashboard.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )
    _assert_listing_owner(row, host_id)
    await db.update_listing(listing_id, {"is_available": False})
    return OKResponse(message=f"Listing {listing_id} has been paused.")


# ---------------------------------------------------------------------------
# ── BUMP (Refresh Listing booster) ──────────────────────────────────────────
# ---------------------------------------------------------------------------

async def bump_listing(listing_id: int) -> OKResponse:
    """
    Bump a listing's visibility_rank to bring it back to the top.
    Called by payments.py after a 'refresh_listing' booster purchase.
    The bump value is deliberately higher than standard activation rank
    to ensure boosted listings surface above regular ones.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )

    # Bump to a high rank tier so the listing appears at top of results
    bump_rank = _BASE_RANK.get(row["package_type"], 10) + 30

    await db.update_listing(listing_id, {"visibility_rank": bump_rank})
    return OKResponse(message=f"Listing {listing_id} has been refreshed to the top.")


# ---------------------------------------------------------------------------
# ── FEATURE (Homepage Feature booster) ──────────────────────────────────────
# ---------------------------------------------------------------------------

async def feature_listing(listing_id: int) -> OKResponse:
    """
    Mark a listing as featured (is_featured=True).
    Called by payments.py after a 'homepage_feature' booster purchase.
    The boosters.end_date governs automatic expiry — handled by a
    scheduled job outside this module.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )

    await db.update_listing(
        listing_id,
        {
            "is_featured": True,
            "visibility_rank": _BASE_RANK.get(row["package_type"], 10) + 50,
        },
    )
    return OKResponse(message=f"Listing {listing_id} is now featured on the homepage.")


async def unfeature_listing(listing_id: int) -> OKResponse:
    """
    Remove homepage feature flag when the booster expires.
    Called by the scheduled expiry job (not by host directly).
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )

    # Restore rank to the package baseline
    await db.update_listing(
        listing_id,
        {
            "is_featured": False,
            "visibility_rank": _BASE_RANK.get(row["package_type"], 10),
        },
    )
    return OKResponse(message=f"Homepage feature expired for listing {listing_id}.")