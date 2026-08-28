"""
listings.py — Makao Rental Platform
=====================================
Business logic for PROPERTIES and LISTINGS.

Architectural invariant (Phase 0/1):
  PROPERTY = the physical real-world asset.
  LISTING  = an advertisement for that property, created by a host.
A property and its first listing are created together via one host-facing
form (create_property_and_listing), but are always persisted as two
separate rows from day one, so a property can later be referenced by more
than one listing (agency re-advertising, building consolidation in V1).

Organic ranking here is always created_at DESC. There is no
visibility_rank / is_featured / package_type column anywhere in this
module — sponsored placement is a fully separate, labelled concept
(see database.get_featured_listings / sponsored_placements).

This module only contains business logic. All raw SQL lives in database.py.
All request/response shapes come from models.py.

Exposed functions (called from main.py route handlers):
  create_property_and_listing(host_id, data)      -> ListingResponse
  get_listing(listing_id)                         -> ListingResponse
  get_listings(filters)                           -> ListingsPageResponse
  get_host_listings(host_id)                      -> List[ListingResponse]
  update_listing(listing_id, host_id, data)       -> ListingResponse
  update_property_for_listing(listing_id, host_id, data) -> ListingResponse
  delete_listing(listing_id, host_id)             -> OKResponse
  confirm_availability(listing_id, host_id, data) -> AvailabilityConfirmationResponse
  get_featured_listings(limit)                    -> List[ListingCardResponse]  (sponsored, labelled)
  get_listings_for_matcher(...)                   -> List[dict]
"""

from __future__ import annotations

import math
from typing import List, Optional

from fastapi import HTTPException, status

import database as db
from models import (
    AvailabilityConfirmationCreate,
    AvailabilityConfirmationResponse,
    ListingCardResponse,
    ListingCreate,
    ListingFilters,
    ListingResponse,
    ListingsPageResponse,
    ListingUpdate,
    LocationResponse,
    OKResponse,
    PropertyResponse,
    PropertyUpdate,
)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _assert_listing_owner(row: dict, host_id: int) -> None:
    if row["host_id"] != host_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this listing.",
        )


def _row_to_location_response(row: dict) -> Optional[LocationResponse]:
    if not row.get("loc_id"):
        return None
    return LocationResponse(
        id=row["loc_id"],
        county=row["loc_county"],
        subcounty=row.get("loc_subcounty"),
        ward=row.get("loc_ward"),
        neighbourhood=row.get("loc_neighbourhood"),
        estate=row.get("loc_estate"),
        address=row.get("loc_address"),
        latitude=row.get("loc_latitude"),
        longitude=row.get("loc_longitude"),
    )


def _row_to_property_response(row: dict) -> PropertyResponse:
    """Build a PropertyResponse from a joined listing row OR a plain property row."""
    return PropertyResponse(
        id=row.get("property_id", row.get("id")),
        property_type=row["property_type"],
        title=row["title"],
        description=row.get("description"),
        property_category=row["property_category"],
        location=_row_to_location_response(row),
        size_sqft=row.get("size_sqft"),
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        floor=row.get("floor"),
        furnished=bool(row["furnished"]),
        parking_spaces=row["parking_spaces"],
        amenities=row.get("amenities") or [],
        photos=row.get("photos") or [],
        created_at=row.get("property_created_at", row.get("created_at")),
        updated_at=row.get("property_updated_at", row.get("updated_at")),
    )


def _row_to_availability_response(row: Optional[dict]) -> Optional[AvailabilityConfirmationResponse]:
    if not row:
        return None
    return AvailabilityConfirmationResponse(
        id=row["id"],
        listing_id=row["listing_id"],
        confirmed_by=row.get("confirmed_by"),
        confirmation_method=row["confirmation_method"],
        status=row["status"],
        confirmed_at=row["confirmed_at"],
        notes=row.get("notes"),
    )


def _row_to_listing_response(row: dict, latest_availability: Optional[dict] = None) -> ListingResponse:
    return ListingResponse(
        id=row["id"],
        host_id=row["host_id"],
        property=_row_to_property_response(row),
        listing_type=row["listing_type"],
        asking_price=float(row["asking_price"]),
        service_charge=float(row["service_charge"]),
        deposit=float(row["deposit"]),
        currency=row["currency"],
        status=row["status"],
        available_from=row.get("available_from"),
        last_confirmed_at=row.get("last_confirmed_at"),
        latest_availability=_row_to_availability_response(latest_availability),
        expires_at=row.get("expires_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        host_name=row.get("host_name"),
        host_account_type=row.get("host_account_type"),
        host_verification_status=row.get("host_verification_status"),
        is_sponsored=bool(row.get("is_sponsored", False)),
    )


def _row_to_card_response(row: dict) -> ListingCardResponse:
    return ListingCardResponse(
        id=row["id"],
        property_id=row["property_id"],
        title=row["title"],
        property_type=row["property_type"],
        county=row.get("loc_county") or "",
        neighbourhood=row.get("loc_neighbourhood"),
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        asking_price=float(row["asking_price"]),
        currency=row["currency"],
        photos=row.get("photos") or [],
        status=row["status"],
        last_confirmed_at=row.get("last_confirmed_at"),
        host_name=row.get("host_name"),
        host_account_type=row.get("host_account_type"),
        is_sponsored=bool(row.get("is_sponsored", False)),
    )


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

async def create_property_and_listing(host_id: int, data: ListingCreate) -> ListingResponse:
    """
    Create ONE property row and ONE listing row referencing it.
    This is the only write path for new inventory at Phase 1 — a host who
    wants a second advertisement for the same physical home is a V1
    (building/dedup) concern, not handled here.
    """
    prop = data.property
    location_id = await db.resolve_or_create_location(prop.location.model_dump())

    property_id = await db.create_property(
        {
            "property_type": prop.property_type,
            "title": prop.title,
            "description": prop.description,
            "property_category": prop.property_category,
            "address": prop.location.address,
            "location_id": location_id,
            "size_sqft": prop.size_sqft,
            "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms,
            "floor": prop.floor,
            "furnished": prop.furnished,
            "parking_spaces": prop.parking_spaces,
            "photos": prop.photos,
            "amenities": prop.amenities,
        }
    )

    listing_id = await db.create_listing_row(
        {
            "property_id": property_id,
            "host_id": host_id,
            "listing_type": data.listing_type,
            "asking_price": data.asking_price,
            "service_charge": data.service_charge,
            "deposit": data.deposit,
            "currency": data.currency,
            "status": "active",
            "available_from": data.available_from,
        }
    )

    # Every new listing starts with one confirmation so freshness has a
    # real starting point ("Available - confirmed today") rather than NULL.
    await db.create_availability_confirmation(
        listing_id=listing_id,
        confirmed_by=host_id,
        status_value="available",
        confirmation_method="host_manual",
        notes="Initial confirmation at listing creation.",
    )

    return await get_listing(listing_id)


# ---------------------------------------------------------------------------
# READ - SINGLE
# ---------------------------------------------------------------------------

async def get_listing(listing_id: int) -> ListingResponse:
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id} not found.",
        )
    latest_availability = await db.get_latest_availability(listing_id)
    return _row_to_listing_response(row, latest_availability)


# ---------------------------------------------------------------------------
# READ - PAGINATED BROWSE
# ---------------------------------------------------------------------------

async def get_listings(filters: ListingFilters) -> ListingsPageResponse:
    """
    Hard-filtered, paginated browse results — organic ordering only
    (created_at DESC). Sponsored placements are never mixed in here; the
    frontend requests them separately via GET /listings/featured and must
    label them "Sponsored".
    """
    rows, total = await db.get_listings_paginated(
        county=filters.county,
        neighbourhood=filters.neighbourhood,
        property_type=filters.property_type,
        min_price=filters.min_price,
        max_price=filters.max_price,
        bedrooms=filters.bedrooms,
        bathrooms=filters.bathrooms,
        amenities=filters.amenities,
        page=filters.page,
        page_size=filters.page_size,
        only_active=True,
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
# READ - HOST DASHBOARD
# ---------------------------------------------------------------------------

async def get_host_listings(host_id: int) -> List[ListingResponse]:
    rows = await db.get_listings_by_host(host_id)
    return [_row_to_listing_response(r) for r in rows]


# ---------------------------------------------------------------------------
# READ - SPONSORED / HOMEPAGE (fully isolated, must be labelled)
# ---------------------------------------------------------------------------

async def get_featured_listings(limit: int = 8) -> List[ListingCardResponse]:
    """
    Return active sponsored placements only. The frontend MUST render a
    visible "Sponsored" label — these are never organic recommendations
    and never influence GET /listings ordering.
    """
    rows = await db.get_featured_listings(limit=limit)
    return [_row_to_card_response(r) for r in rows]


# ---------------------------------------------------------------------------
# READ - AI CANDIDATE FEED
# ---------------------------------------------------------------------------

async def get_listings_for_matcher(
    county: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    max_budget: Optional[float] = None,
    bedrooms: Optional[int] = None,
    limit: int = 50,
) -> List[dict]:
    """
    Broad, hard-filtered candidate set for the AI layer. No ranking signal
    here is payment-derived. matcher.py's direct LLM ranking is a known
    architecture violation scheduled for replacement by the Phase 3
    deterministic scoring engine — this function stays a plain data feed.
    """
    return await db.search_listings_for_ai(
        {
            "county": county,
            "neighbourhood": neighbourhood,
            "max_budget": max_budget,
            "bedrooms": bedrooms,
        }
    )


# ---------------------------------------------------------------------------
# UPDATE - LISTING FIELDS
# ---------------------------------------------------------------------------

async def update_listing(listing_id: int, host_id: int, data: ListingUpdate) -> ListingResponse:
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Listing {listing_id} not found.")
    _assert_listing_owner(row, host_id)

    updates = data.model_dump(exclude_none=True)
    if updates:
        await db.update_listing_row(listing_id, updates)

    return await get_listing(listing_id)


# ---------------------------------------------------------------------------
# UPDATE - PROPERTY FIELDS (via a listing the host owns)
# ---------------------------------------------------------------------------

async def update_property_for_listing(listing_id: int, host_id: int, data: PropertyUpdate) -> ListingResponse:
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Listing {listing_id} not found.")
    _assert_listing_owner(row, host_id)

    updates = data.model_dump(exclude={"location"}, exclude_none=True)
    if data.location is not None:
        updates["location_id"] = await db.resolve_or_create_location(data.location.model_dump())
        if data.location.address:
            updates["address"] = data.location.address

    if updates:
        await db.update_property(row["property_id"], updates)

    return await get_listing(listing_id)


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

async def delete_listing(listing_id: int, host_id: int) -> OKResponse:
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Listing {listing_id} not found.")
    _assert_listing_owner(row, host_id)
    await db.delete_listing_row(listing_id)
    return OKResponse(message=f"Listing {listing_id} deleted successfully.")


# ---------------------------------------------------------------------------
# AVAILABILITY FRESHNESS
# ---------------------------------------------------------------------------

async def confirm_availability(
    listing_id: int, actor_id: int, data: AvailabilityConfirmationCreate, is_admin: bool = False
) -> AvailabilityConfirmationResponse:
    """
    Append a new availability confirmation. Only the listing's host (or an
    admin, via confirmation_method='admin_override') may confirm. This is
    the single source of truth for freshness — never a Boolean flag.
    """
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Listing {listing_id} not found.")
    if not is_admin:
        _assert_listing_owner(row, actor_id)

    result = await db.create_availability_confirmation(
        listing_id=listing_id,
        confirmed_by=actor_id,
        status_value=data.status,
        confirmation_method=data.confirmation_method,
        notes=data.notes,
    )
    return _row_to_availability_response(result)


async def deactivate_listing(listing_id: int, host_id: int) -> OKResponse:
    """Pause a listing (status='inactive') without deleting it."""
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Listing {listing_id} not found.")
    _assert_listing_owner(row, host_id)
    await db.update_listing_row(listing_id, {"status": "inactive"})
    return OKResponse(message=f"Listing {listing_id} has been paused.")


# ---------------------------------------------------------------------------
# DEPRECATED COMPATIBILITY SHIMS
# ---------------------------------------------------------------------------
# These four functions predate the Phase 0 property/listing split and the
# payment-isolation invariant (no visibility_rank / is_featured / package
# activation on `listings`). payments.py's booster/package flow still calls
# them. They are kept as safe no-ops so the app imports and runs, but they
# intentionally do NOT restore payment-influenced organic ranking. The
# underlying monetization model (packages that "activate" a listing,
# boosters that change its rank) needs a real Phase 2 redesign around
# sponsored_placements — see roadmap V1 sec32 and PHASE1_NOTES.md.
# ---------------------------------------------------------------------------

async def activate_listing(listing_id: int) -> OKResponse:
    """
    DEPRECATED shim. A listing created via create_property_and_listing is
    already active — there is no separate "pay to activate" state in the
    Phase 0/1 schema. Left as a no-op so payments.py's legacy package flow
    does not crash; it should be redesigned in Phase 2 around subscriptions
    and sponsored_placements rather than gating organic listing visibility.
    """
    return OKResponse(message=f"Listing {listing_id} activation is a no-op under the new schema.")


async def feature_listing(listing_id: int) -> OKResponse:
    """DEPRECATED no-op shim — see module docstring above. Use sponsored_placements."""
    return OKResponse(message=f"feature_listing({listing_id}) is a no-op; use sponsored_placements instead.")


async def unfeature_listing(listing_id: int) -> OKResponse:
    """DEPRECATED no-op shim — see module docstring above."""
    return OKResponse(message=f"unfeature_listing({listing_id}) is a no-op; use sponsored_placements instead.")


async def bump_listing(listing_id: int) -> OKResponse:
    """DEPRECATED no-op shim — see module docstring above. There is no rank column to bump."""
    return OKResponse(message=f"bump_listing({listing_id}) is a no-op; there is no visibility_rank column.")
