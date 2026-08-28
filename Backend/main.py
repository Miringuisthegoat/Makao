"""
main.py — Makao Rental Platform
==================================
FastAPI application entry point.

Responsibilities:
  - App instance and OpenAPI metadata
  - Startup / shutdown lifecycle (DB pool, booster expiry cron)
  - Middleware (CORS, trusted hosts, request logging)
  - Router registration for all feature modules
  - Direct route handlers that don't belong to a feature module:
      GET  /api/health          — liveness probe
      GET  /api/pricing         — public pricing page data (from config.py)
      GET  /api/listings/{id}   — single listing detail (public)
      GET  /api/listings        — paginated browse (public)
      GET  /api/listings/featured — homepage featured listings (public)
      POST /api/listings        — create listing (protected)
      PATCH /api/listings/{id}  — update listing (protected)
      DELETE /api/listings/{id} — delete listing (protected)
      POST /api/listings/{id}/deactivate — pause listing (protected)
      GET  /api/hosts/me        — current host profile (protected)
      PATCH /api/hosts/me       — update host profile (protected)
      POST /api/auth/change-password — change password (protected)
      GET  /api/hosts/dashboard — dashboard data (protected)
      GET  /api/hosts/analytics — analytics (agency only)
  - Frontend static file serving

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — configure before any module imports so all loggers inherit format
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("makao.main")

# ---------------------------------------------------------------------------
# Internal module imports
# ---------------------------------------------------------------------------
import database as db
import listings as listing_logic
import auth as auth_logic
from auth import get_current_host, require_agency
from config import BOOSTERS, PRICING
from models import (
    AnalyticsResponse,
    AvailabilityConfirmationCreate,
    AvailabilityConfirmationResponse,
    DashboardResponse,
    FavoriteCreate,
    FavoriteResponse,
    ListingCardResponse,
    ListingCreate,
    ListingFilters,
    ListingResponse,
    ListingsPageResponse,
    ListingUpdate,
    LoginRequest,
    OKResponse,
    PackageDetail,
    BoosterDetail,
    PasswordChangeRequest,
    PricingResponse,
    PropertyUpdate,
    SavedSearchCreate,
    SavedSearchResponse,
    SignupRequest,
    TokenResponse,
    UserPreferencesResponse,
    UserPreferencesUpsert,
    UserResponse,
    UserUpdateRequest,
)
import preferences as preferences_logic

# Feature routers — each module owns its own APIRouter
from auth import router as auth_router
from chatbot import router as chatbot_router
from payments import router as payments_router


# ===========================================================================
# ── LIFESPAN — startup and shutdown ─────────────────────────────────────────
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Replaces the deprecated @app.on_event("startup") / ("shutdown") pattern.

    On startup:
      - Initialise the database connection pool
      - Create tables if they don't exist
      - Deactivate any boosters that expired while the server was offline
      - Start background booster expiry task (runs every hour)

    On shutdown:
      - Cancel background task
      - Close DB connection pool cleanly
    """
    # ── STARTUP ─────────────────────────────────────────────────────────
    logger.info("Makao API starting up…")

    await db.init_db()
    logger.info("Database ready.")

    # Catch any boosters that expired while server was down
    expired = await db.deactivate_expired_boosters()
    if expired:
        logger.info(f"Deactivated {expired} expired booster(s) on startup.")

    # Background task: check booster expiry every hour
    bg_task = asyncio.create_task(_booster_expiry_loop())
    logger.info("Background booster expiry task started.")

    yield  # ← app runs here

    # ── SHUTDOWN ─────────────────────────────────────────────────────────
    logger.info("Makao API shutting down…")
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass
    await db.close_pool()
    logger.info("Database pool closed. Goodbye.")


async def _booster_expiry_loop():
    """
    Background coroutine — deactivates expired boosters every hour.
    When a homepage_feature booster expires, the listing is also unfeatured.
    """
    while True:
        await asyncio.sleep(3600)   # run every hour
        try:
            count = await db.deactivate_expired_boosters()
            if count:
                logger.info(f"Booster expiry check: deactivated {count} booster(s).")
                # Unfeature any listings whose homepage_feature booster just expired
                await _sync_featured_status_after_expiry()
        except Exception as exc:
            logger.error(f"Booster expiry loop error: {exc}")


async def _sync_featured_status_after_expiry():
    """
    DEPRECATED no-op. `listings.is_featured` / `listings.package_type` no
    longer exist — Phase 0 moved paid placement to `sponsored_placements`,
    which already has its own start/end date bounds and needs no expiry
    sync here. Kept as a stub (rather than deleted) so the background loop
    below doesn't need restructuring; a Phase 2 monetization pass should
    remove this entirely once boosters/packages are redesigned around
    sponsored_placements.
    """
    return


# ===========================================================================
# ── APP INSTANCE ─────────────────────────────────────────────────────────────
# ===========================================================================

app = FastAPI(
    title="Makao Ke API",
    description=(
        "Kenya's nationwide house rental platform. "
        "Hosts advertise properties; visitors browse and find homes via AI chatbot."
    ),
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# ===========================================================================
# ── MIDDLEWARE ────────────────────────────────────────────────────────────────
# ===========================================================================

# CORS — restrict to your domain in production via ALLOWED_ORIGINS env var
_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "*")
_allowed_origins = (
    [o.strip() for o in _allowed_origins_raw.split(",")]
    if _allowed_origins_raw != "*"
    else ["*"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted hosts — prevent Host header attacks in production
_trusted_hosts_raw = os.getenv("TRUSTED_HOSTS", "*")
if _trusted_hosts_raw != "*":
    _trusted_hosts = [h.strip() for h in _trusted_hosts_raw.split(",")]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)


# ===========================================================================
# ── ROUTER REGISTRATION ───────────────────────────────────────────────────────
# ===========================================================================

# Auth — POST /api/auth/signup, POST /api/auth/login
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])

# Chatbot — POST /api/chat/message, GET /api/chat/history/{id}, DELETE /api/chat/clear/{id}
app.include_router(chatbot_router, prefix="/api", tags=["Chatbot"])

# Payments — /api/payments/* (M-Pesa + Stripe + boosters + billing history)
app.include_router(payments_router, prefix="/api/payments", tags=["Payments"])


# ===========================================================================
# ── GLOBAL EXCEPTION HANDLER ─────────────────────────────────────────────────
# ===========================================================================

@app.exception_handler(404)
async def not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={"ok": False, "detail": "The requested resource was not found."},
    )


@app.exception_handler(500)
async def server_error_handler(request, exc):
    logger.error(f"Unhandled 500 error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": "An unexpected error occurred. Please try again."},
    )


# ===========================================================================
# ── HEALTH CHECK ─────────────────────────────────────────────────────────────
# ===========================================================================

@app.get(
    "/api/health",
    tags=["Health"],
    summary="Liveness probe",
)
async def health_check():
    """Returns 200 when the API is running. Used by load balancers and monitors."""
    return {
        "status": "ok",
        "service": "Makao Ke API",
        "version": "1.0.0",
    }


# ===========================================================================
# ── PRICING (public) ─────────────────────────────────────────────────────────
# ===========================================================================

@app.get(
    "/api/pricing",
    response_model=PricingResponse,
    tags=["Pricing"],
    summary="Get listing packages and booster prices",
    description=(
        "Returns all package and booster details from config.py. "
        "Prices are set privately by the platform owner — never hardcoded here."
    ),
)
async def get_pricing() -> PricingResponse:
    """
    Public endpoint — powers the pricing.html page.
    All values read from config.py; the owner sets actual prices there.
    """
    packages = {
        key: PackageDetail(
            name=val["name"],
            price=float(val.get("price", 0)),
            listings=val["listings"],
            photos=val["photos"],
            chatbot=val["chatbot"],
            featured=val["featured"],
            verified_badge=val["verified_badge"],
        )
        for key, val in PRICING.items()
    }

    boosters = {
        key: BoosterDetail(
            name=val["name"],
            price=float(val.get("price", 0)),
            duration_days=val.get("duration_days"),
        )
        for key, val in BOOSTERS.items()
    }

    return PricingResponse(packages=packages, boosters=boosters)


# ===========================================================================
# ── LISTINGS (public read + protected write) ─────────────────────────────────
# ===========================================================================

@app.get(
    "/api/listings/featured",
    response_model=List[ListingCardResponse],
    tags=["Listings"],
    summary="Featured listings for the homepage",
    description="Returns top featured, available listings ordered by visibility rank.",
)
async def featured_listings(
    limit: int = Query(8, ge=1, le=20),
) -> List[ListingCardResponse]:
    return await listing_logic.get_featured_listings(limit=limit)


@app.get(
    "/api/listings",
    response_model=ListingsPageResponse,
    tags=["Listings"],
    summary="Browse all listings with filters",
    description=(
        "Paginated, filterable list of active listings. "
        "Featured and agency listings surface first."
    ),
)
async def browse_listings(
    county: Optional[str] = Query(None),
    neighbourhood: Optional[str] = Query(None),
    property_type: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    bedrooms: Optional[int] = Query(None, ge=0),
    bathrooms: Optional[int] = Query(None, ge=1),
    amenities: Optional[List[str]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ListingsPageResponse:
    """
    Hard-filtered, organic results only — ordered by created_at DESC.
    Sponsored placements are never mixed in here; see GET /listings/featured.
    """
    filters = ListingFilters(
        county=county,
        neighbourhood=neighbourhood,
        property_type=property_type,
        min_price=min_price,
        max_price=max_price,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        amenities=amenities,
        page=page,
        page_size=page_size,
    )
    return await listing_logic.get_listings(filters)


@app.get(
    "/api/listings/{listing_id}",
    response_model=ListingResponse,
    tags=["Listings"],
    summary="Single listing detail",
    description="Full details for one listing. Powers listing.html.",
)
async def get_listing(listing_id: int) -> ListingResponse:
    return await listing_logic.get_listing(listing_id)


@app.post(
    "/api/listings",
    response_model=ListingResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Listings"],
    summary="Create a property + its first listing",
    description=(
        "Creates ONE property row (the physical asset) and ONE listing row "
        "(the advertisement) referencing it, and goes live immediately — "
        "organic visibility is never gated behind payment. Sponsored "
        "placement, if purchased separately, is layered on top and labelled."
    ),
)
async def create_listing(
    body: ListingCreate,
    host: UserResponse = Depends(get_current_host),
) -> ListingResponse:
    return await listing_logic.create_property_and_listing(host_id=host.id, data=body)


@app.patch(
    "/api/listings/{listing_id}/property",
    response_model=ListingResponse,
    tags=["Listings"],
    summary="Update the physical property behind a listing",
    description="Partial update to property fields (bedrooms, photos, location, etc). Host must own the listing.",
)
async def update_listing_property(
    listing_id: int,
    body: PropertyUpdate,
    host: UserResponse = Depends(get_current_host),
) -> ListingResponse:
    return await listing_logic.update_property_for_listing(listing_id, host.id, body)


@app.post(
    "/api/listings/{listing_id}/availability",
    response_model=AvailabilityConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Listings"],
    summary="Confirm listing availability",
    description=(
        "Appends a new availability confirmation (available / unavailable / "
        "uncertain). This is the source of truth for freshness shown on the "
        "frontend (e.g. 'Available — confirmed today') — there is no single "
        "Boolean flag. Host must own the listing."
    ),
)
async def confirm_listing_availability(
    listing_id: int,
    body: AvailabilityConfirmationCreate,
    host: UserResponse = Depends(get_current_host),
) -> AvailabilityConfirmationResponse:
    return await listing_logic.confirm_availability(listing_id, host.id, body)


@app.patch(
    "/api/listings/{listing_id}",
    response_model=ListingResponse,
    tags=["Listings"],
    summary="Update a listing",
    description="Partial update — only supplied fields are changed. Host must own the listing.",
)
async def update_listing(
    listing_id: int,
    body: ListingUpdate,
    host: UserResponse = Depends(get_current_host),
) -> ListingResponse:
    return await listing_logic.update_listing(listing_id, host.id, body)


@app.delete(
    "/api/listings/{listing_id}",
    response_model=OKResponse,
    tags=["Listings"],
    summary="Delete a listing",
    description="Permanently deletes a listing. Host must own the listing.",
)
async def delete_listing(
    listing_id: int,
    host: UserResponse = Depends(get_current_host),
) -> OKResponse:
    return await listing_logic.delete_listing(listing_id, host.id)


@app.post(
    "/api/listings/{listing_id}/deactivate",
    response_model=OKResponse,
    tags=["Listings"],
    summary="Pause a listing",
    description="Sets is_available=False without deleting. Host can reactivate from dashboard.",
)
async def deactivate_listing(
    listing_id: int,
    host: UserResponse = Depends(get_current_host),
) -> OKResponse:
    return await listing_logic.deactivate_listing(listing_id, host.id)


# ===========================================================================
# ── HOST PROFILE (protected) ─────────────────────────────────────────────────
# ===========================================================================

@app.get(
    "/api/hosts/me",
    response_model=UserResponse,
    tags=["Hosts"],
    summary="Get current host profile",
)
async def get_me(host: UserResponse = Depends(get_current_host)) -> UserResponse:
    """Returns the authenticated host's profile. Used to populate the profile page."""
    return host


@app.patch(
    "/api/hosts/me",
    response_model=UserResponse,
    tags=["Hosts"],
    summary="Update host profile",
    description="Update full_name, phone, or profile_photo. All fields optional.",
)
async def update_me(
    body: UserUpdateRequest,
    host: UserResponse = Depends(get_current_host),
) -> UserResponse:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields provided to update.",
        )
    updated = await db.update_user(host.id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Host not found.")
    return UserResponse(**updated)


@app.post(
    "/api/auth/change-password",
    response_model=OKResponse,
    tags=["Auth"],
    summary="Change password",
    description="Authenticated host changes their password. Current password is verified first.",
)
async def change_password(
    body: PasswordChangeRequest,
    host: UserResponse = Depends(get_current_host),
) -> OKResponse:
    result = await auth_logic.change_password(
        host=host,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return OKResponse(message=result["message"])


# ===========================================================================
# ── HOST DASHBOARD (protected) ───────────────────────────────────────────────
# ===========================================================================

@app.get(
    "/api/hosts/dashboard",
    response_model=DashboardResponse,
    tags=["Hosts"],
    summary="Host dashboard data",
    description=(
        "Returns the host's listings, payment history, and active boosters "
        "in a single call. Powers dashboard.html."
    ),
)
async def get_dashboard(
    host: UserResponse = Depends(get_current_host),
) -> DashboardResponse:
    listings_data = await listing_logic.get_host_listings(host.id)
    payments_data = await db.get_payments_by_host(host.id)
    boosters_raw = []
    for lst in listings_data:
        boosters_raw.extend(await db.get_active_boosters(lst.id))

    from models import BoosterResponse, PaymentResponse

    return DashboardResponse(
        host=host,
        listings=listings_data,
        payments=[PaymentResponse(**p) for p in payments_data],
        active_boosters=[BoosterResponse(**b) for b in boosters_raw],
        listing_count=len(listings_data),
        active_listing_count=sum(1 for l in listings_data if l.is_available),
    )


# ===========================================================================
# ── ANALYTICS (agency only, protected) ───────────────────────────────────────
# ===========================================================================

@app.get(
    "/api/hosts/analytics",
    response_model=AnalyticsResponse,
    tags=["Hosts"],
    summary="Listing analytics (Agency only)",
    description=(
        "Per-listing stats: active boosters, total spend, visibility rank. "
        "Restricted to agency accounts."
    ),
)
async def get_analytics(
    host: UserResponse = Depends(require_agency),
) -> AnalyticsResponse:
    rows = await db.get_listing_stats(host.id)
    total_spent = sum(float(r.get("total_spent", 0)) for r in rows)

    from models import AnalyticsListingRow

    return AnalyticsResponse(
        listings=[AnalyticsListingRow(**r) for r in rows],
        total_listings=len(rows),
        total_spent=total_spent,
    )


# ===========================================================================
# ── FRONTEND STATIC FILE SERVING ─────────────────────────────────────────────
# ===========================================================================

_frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")

if os.path.exists(_frontend_path):
    # Serve /images, /css, /js as static assets
    app.mount(
        "/static",
        StaticFiles(directory=_frontend_path),
        name="static",
    )

    # Serve each HTML page at its clean URL path
    _html_pages = {
        "/":                    "index.html",
        "/listings":            "listings.html",
        "/listing":             "listing.html",
        "/pricing":             "pricing.html",
        "/login":               "login.html",
        "/signup":              "signup.html",
        "/dashboard":           "dashboard.html",
        "/add-listing":         "add-listing.html",
        "/billing":             "billing.html",
        "/analytics":           "analytics.html",
        "/payment-method":      "payment-method.html",
        "/payment-mpesa":       "payment-mpesa.html",
        "/payment-success":     "payment-success.html",
        "/payment-failed":      "payment-failed.html",
    }

    for _route, _filename in _html_pages.items():
        _filepath = os.path.join(_frontend_path, _filename)

        # Build a closure to capture the correct filepath per iteration
        def _make_handler(fp: str):
            async def _handler():
                return FileResponse(fp)
            return _handler

        app.get(_route, include_in_schema=False)(_make_handler(_filepath))

    logger.info(f"Frontend serving enabled from: {_frontend_path}")
else:
    logger.warning(
        f"Frontend directory not found at {_frontend_path}. "
        "Static file serving disabled — API-only mode."
    )