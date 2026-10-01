"""
models.py — Makao Rental Platform
===================================
All Pydantic v2 schemas used for:
  - Request body validation (what the API receives)
  - Response serialisation (what the API returns)
  - Internal data shapes passed between modules

Naming convention:
  <Entity>Create   — inbound payload to create a resource
  <Entity>Update   — inbound payload to partially update a resource
  <Entity>Response — outbound shape returned to the client
  <Entity>DB       — full DB row shape (internal use only)

Never import from database.py here — models are pure data contracts.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


# ===========================================================================
# ── SHARED / BASE ────────────────────────────────────────────────────────────
# ===========================================================================

class OKResponse(BaseModel):
    """Generic success acknowledgement."""
    ok: bool = True
    message: str = "Success"


class ErrorResponse(BaseModel):
    """Generic error shape returned on 4xx / 5xx."""
    ok: bool = False
    detail: str


# ===========================================================================
# ── AUTH ─────────────────────────────────────────────────────────────────────
# ===========================================================================

class SignupRequest(BaseModel):
    """
    POST /auth/signup
    Sent by the registration page (signup.html).
    """
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    phone: str = Field(..., min_length=9, max_length=20)
    host_type: str = Field(..., pattern="^(landlord|agency)$")

    @field_validator("phone")
    @classmethod
    def validate_kenyan_phone(cls, v: str) -> str:
        """
        Accept formats: 07XXXXXXXX, 01XXXXXXXX, +2547XXXXXXXX, 2547XXXXXXXX.
        Normalise to +254XXXXXXXXX for storage.
        """
        digits = re.sub(r"\D", "", v)
        if digits.startswith("0") and len(digits) == 10:
            digits = "254" + digits[1:]
        if digits.startswith("254") and len(digits) == 12:
            return "+" + digits
        raise ValueError("Enter a valid Kenyan phone number (e.g. 0712345678).")

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class LoginRequest(BaseModel):
    """
    POST /auth/login
    """
    email: EmailStr
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    """
    Returned on successful login or signup.
    access_token is a signed JWT.
    """
    access_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds
    host: "UserResponse"     # lightweight user object for the frontend


class PasswordChangeRequest(BaseModel):
    """
    POST /auth/change-password  (protected)
    """
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=128)


# ===========================================================================
# ── USERS ────────────────────────────────────────────────────────────────────
# ===========================================================================

class UserResponse(BaseModel):
    """
    Safe public shape of a user — never includes password_hash.
    Returned inside TokenResponse and from GET /hosts/me.
    """
    id: int
    full_name: str
    email: str
    phone: Optional[str] = None
    profile_photo: Optional[str] = None
    host_type: str           # 'landlord' | 'agency'
    plan_status: str         # 'active' | 'inactive' | 'suspended'
    created_at: datetime


class UserUpdateRequest(BaseModel):
    """
    PATCH /hosts/me  (protected)
    All fields optional — only provided fields are updated.
    """
    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone: Optional[str] = Field(None, min_length=9, max_length=20)
    profile_photo: Optional[str] = None   # URL returned after file upload

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        if digits.startswith("0") and len(digits) == 10:
            digits = "254" + digits[1:]
        if digits.startswith("254") and len(digits) == 12:
            return "+" + digits
        raise ValueError("Enter a valid Kenyan phone number.")


# ===========================================================================
# ── PROPERTIES & LISTINGS (Phase 0/1 shape) ─────────────────────────────────
# ===========================================================================
#
# Architectural invariant: PROPERTY (the physical asset) is distinct from
# LISTING (an advertisement for that property). A property can in principle
# be referenced by more than one listing (e.g. re-advertised by a new agent)
# even though Phase 1 still creates them 1:1 through a single combined form.
# Organic ranking of listings must never depend on payment — there is no
# visibility_rank / is_featured / package_type on listings by design.
# ===========================================================================

# All 47 counties in Kenya — used for validation
KENYAN_COUNTIES = {
    "Baringo", "Bomet", "Bungoma", "Busia", "Elgeyo-Marakwet", "Embu",
    "Garissa", "Homa Bay", "Isiolo", "Kajiado", "Kakamega", "Kericho",
    "Kiambu", "Kilifi", "Kirinyaga", "Kisii", "Kisumu", "Kitui", "Kwale",
    "Laikipia", "Lamu", "Machakos", "Makueni", "Mandera", "Marsabit",
    "Meru", "Migori", "Mombasa", "Murang'a", "Nairobi", "Nakuru", "Nandi",
    "Narok", "Nyamira", "Nyandarua", "Nyeri", "Samburu", "Siaya",
    "Taita-Taveta", "Tana River", "Tharaka-Nithi", "Trans Nzoia", "Turkana",
    "Uasin Gishu", "Vihiga", "Wajir", "West Pokot",
}

# Allowed amenity tags
VALID_AMENITIES = {
    "wifi", "parking", "security", "garden", "gym", "swimming_pool",
    "backup_power", "water_storage", "cctv", "elevator", "balcony",
    "servant_quarter", "furnished", "air_conditioning", "borehole",
    "solar_power", "fibre", "pet_friendly", "wheelchair_accessible",
}

# Residential property types supported at MVP (land/commercial excluded)
PROPERTY_TYPES = {
    "bedsitter", "studio", "1_bedroom", "2_bedroom", "3_bedroom",
    "4plus_bedroom", "apartment", "house", "townhouse", "maisonette",
}

LISTING_TYPES = {"rent", "sale"}
LISTING_STATUSES = {"active", "inactive", "rented", "removed"}
AVAILABILITY_STATUSES = {"available", "unavailable", "uncertain"}


def _validate_amenities(v: List[str]) -> List[str]:
    invalid = [a for a in v if a.lower() not in VALID_AMENITIES]
    if invalid:
        raise ValueError(
            f"Unknown amenities: {invalid}. Valid options: {sorted(VALID_AMENITIES)}"
        )
    return [a.lower() for a in v]


def _validate_county(v: str) -> str:
    normalised = v.strip().title()
    if normalised not in KENYAN_COUNTIES:
        raise ValueError(f"'{v}' is not a recognised Kenyan county.")
    return normalised


# ---------------------------------------------------------------------------
# ── LOCATION ─────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class LocationInput(BaseModel):
    """
    Structured location supplied when creating/updating a property.
    A matching or new `locations` row is resolved server-side — the host
    never supplies a location_id directly.
    """
    county: str = Field(..., min_length=2, max_length=100)
    subcounty: Optional[str] = Field(None, max_length=100)
    ward: Optional[str] = Field(None, max_length=100)
    neighbourhood: Optional[str] = Field(None, max_length=120)
    estate: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = Field(None, max_length=255)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("county")
    @classmethod
    def _county(cls, v: str) -> str:
        return _validate_county(v)


class LocationResponse(BaseModel):
    id: int
    county: str
    subcounty: Optional[str] = None
    ward: Optional[str] = None
    neighbourhood: Optional[str] = None
    estate: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# ---------------------------------------------------------------------------
# ── PROPERTIES ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class PropertyCreate(BaseModel):
    """
    The physical-asset half of POST /listings.
    Not exposed as its own top-level endpoint at Phase 1 — hosts still
    create a property and its first listing together in one form — but
    modelled and persisted as a distinct `properties` row from day one so
    a property can later be referenced by more than one listing.
    """
    property_type: str = Field(..., pattern="^(" + "|".join(PROPERTY_TYPES) + ")$")
    title: str = Field(..., min_length=5, max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    property_category: str = Field("residential", pattern="^(residential|commercial)$")
    location: LocationInput
    size_sqft: Optional[int] = Field(None, gt=0)
    bedrooms: int = Field(..., ge=0, le=50)
    bathrooms: int = Field(..., ge=1, le=20)
    floor: Optional[str] = Field(None, max_length=20)
    furnished: bool = False
    parking_spaces: int = Field(0, ge=0, le=50)
    amenities: List[str] = Field(default_factory=list)
    photos: List[str] = Field(default_factory=list)

    @field_validator("amenities")
    @classmethod
    def _amenities(cls, v: List[str]) -> List[str]:
        return _validate_amenities(v)

    @field_validator("photos")
    @classmethod
    def _photos(cls, v: List[str]) -> List[str]:
        if len(v) > 50:
            raise ValueError("Maximum 50 photos per property.")
        return v

    @field_validator("property_category")
    @classmethod
    def _residential_only_mvp(cls, v: str) -> str:
        if v != "residential":
            raise ValueError(
                "Commercial properties are out of scope for MVP/V1. "
                "Only 'residential' is accepted."
            )
        return v


class PropertyUpdate(BaseModel):
    """PATCH-style partial update to the physical asset."""
    title: Optional[str] = Field(None, min_length=5, max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    location: Optional[LocationInput] = None
    size_sqft: Optional[int] = Field(None, gt=0)
    bedrooms: Optional[int] = Field(None, ge=0, le=50)
    bathrooms: Optional[int] = Field(None, ge=1, le=20)
    floor: Optional[str] = Field(None, max_length=20)
    furnished: Optional[bool] = None
    parking_spaces: Optional[int] = Field(None, ge=0, le=50)
    amenities: Optional[List[str]] = None
    photos: Optional[List[str]] = None

    @field_validator("amenities")
    @classmethod
    def _amenities(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return None if v is None else _validate_amenities(v)


class PropertyResponse(BaseModel):
    id: int
    property_type: str
    title: str
    description: Optional[str] = None
    property_category: str
    location: Optional[LocationResponse] = None
    size_sqft: Optional[int] = None
    bedrooms: int
    bathrooms: int
    floor: Optional[str] = None
    furnished: bool
    parking_spaces: int
    amenities: List[str] = []
    photos: List[str] = []
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# ── AVAILABILITY CONFIRMATIONS ───────────────────────────────────────────────
# ---------------------------------------------------------------------------

class AvailabilityConfirmationCreate(BaseModel):
    """
    POST /listings/{id}/availability  (protected — host, or admin override)
    A Boolean is_available is deliberately NOT the source of truth — every
    confirmation is appended so the UI can show real freshness
    ("Available — confirmed today") rather than a single stale flag.
    """
    status: str = Field(..., pattern="^(available|unavailable|uncertain)$")
    confirmation_method: str = Field(
        "host_manual",
        pattern="^(host_manual|admin_override|system_expiry|renter_report)$",
    )
    notes: Optional[str] = Field(None, max_length=500)


class AvailabilityConfirmationResponse(BaseModel):
    id: int
    listing_id: int
    confirmed_by: Optional[int] = None
    confirmation_method: str
    status: str
    confirmed_at: datetime
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# ── MAKAO INTELLIGENCE (Phase 4) ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

class ListingIntelligence(BaseModel):
    """
    Optional "Makao intelligence" block attached to a ListingResponse when
    the caller supplies enough context (monthly_budget and/or workplace
    coordinates + transport_mode as query params on GET /listings/{id}).
    Deterministic output only — see Backend/intelligence/affordability.py
    and Backend/intelligence/commute.py. Never guaranteed figures; the UI
    must present these as estimates, not confirmed costs.
    """
    estimated_monthly_cost: float
    advertised_rent: float
    service_charge: float
    estimated_utilities: float
    estimated_transport: float
    budget: Optional[float] = None
    budget_difference: Optional[float] = None
    affordability_score: Optional[int] = None
    affordability_confidence: str
    commute_minutes: Optional[int] = None
    commute_minutes_low: Optional[int] = None
    commute_minutes_high: Optional[int] = None
    commute_range_label: Optional[str] = None
    commute_destination_name: Optional[str] = None
    is_estimate: bool = True


# ---------------------------------------------------------------------------
# ── LISTINGS ─────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class ListingCreate(BaseModel):
    """
    POST /listings  (protected)
    Creates ONE property row and ONE listing row that references it, in a
    single request — the simplest host-facing shape for Phase 1. Multiple
    listings sharing a property (e.g. building consolidation) is a V1
    concern (see buildings / deduplication).
    """
    property: PropertyCreate
    listing_type: str = Field("rent", pattern="^(rent|sale)$")
    asking_price: float = Field(..., gt=0)
    service_charge: float = Field(0, ge=0)
    deposit: float = Field(0, ge=0)
    currency: str = Field("KES", max_length=5)
    available_from: Optional[datetime] = None


class ListingUpdate(BaseModel):
    """
    PATCH /listings/{id}  (protected — host only)
    Listing-level fields only. Use PATCH /properties/{id} for physical
    attributes of the underlying property.
    """
    asking_price: Optional[float] = Field(None, gt=0)
    service_charge: Optional[float] = Field(None, ge=0)
    deposit: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, max_length=5)
    status: Optional[str] = Field(None, pattern="^(active|inactive|rented|removed)$")
    available_from: Optional[datetime] = None


class ListingResponse(BaseModel):
    """
    Full listing shape returned to the client — the advertisement plus its
    referenced property, never re-flattened back into one legacy row.
    No visibility_rank / is_featured / package_type: organic placement is
    never influenced by payment. Sponsored placement, when present, is
    surfaced separately and must be labelled in the UI.
    """
    id: int
    host_id: int
    property: PropertyResponse
    listing_type: str
    asking_price: float
    service_charge: float
    deposit: float
    currency: str
    status: str
    available_from: Optional[datetime] = None
    last_confirmed_at: Optional[datetime] = None
    latest_availability: Optional[AvailabilityConfirmationResponse] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    host_name: Optional[str] = None
    host_account_type: Optional[str] = None
    host_verification_status: Optional[str] = None
    is_sponsored: bool = False   # labelled, isolated from organic rank
    intelligence: Optional[ListingIntelligence] = None   # Phase 4 — populated only when requested


class ListingCardResponse(BaseModel):
    """Lightweight listing shape for cards (browse page, homepage, saved list)."""
    id: int
    property_id: int
    title: str
    property_type: str
    county: str
    neighbourhood: Optional[str] = None
    bedrooms: int
    bathrooms: int
    asking_price: float
    currency: str
    photos: List[str] = []
    status: str
    availability_status: Optional[str] = None
    last_confirmed_at: Optional[datetime] = None
    host_name: Optional[str] = None
    host_account_type: Optional[str] = None
    is_sponsored: bool = False


class ListingsPageResponse(BaseModel):
    """Paginated listing results for the browse page."""
    total: int
    page: int
    page_size: int
    pages: int
    listings: List[ListingCardResponse]


class ListingFilters(BaseModel):
    """
    Query params for GET /listings
    Hard filters only — no field here influences ranking by payment status.
    """
    county: Optional[str] = None
    neighbourhood: Optional[str] = None
    property_type: Optional[str] = None
    min_price: Optional[float] = Field(None, ge=0)
    max_price: Optional[float] = Field(None, ge=0)
    bedrooms: Optional[int] = Field(None, ge=0)
    bathrooms: Optional[int] = Field(None, ge=1)
    amenities: Optional[List[str]] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def price_range_check(self) -> "ListingFilters":
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot exceed max_price.")
        return self


# ===========================================================================
# ── USER PREFERENCES (renter) ───────────────────────────────────────────────
# ===========================================================================
#
# One row per renter. Upserted after every search or chat interaction.
# Feeds the Phase 2 search service and Phase 3 deterministic scoring engine.
# The Phase 5 LLM preference extractor writes to this same shape — it never
# ranks properties itself.
# ===========================================================================

class UserPreferencesUpsert(BaseModel):
    """PUT /preferences  (protected — renter)"""
    workplace_latitude: Optional[float] = Field(None, ge=-90, le=90)
    workplace_longitude: Optional[float] = Field(None, ge=-180, le=180)
    workplace_name: Optional[str] = Field(None, max_length=150)
    income_range: Optional[str] = Field(None, max_length=30)
    rent_budget: Optional[float] = Field(None, gt=0)
    total_housing_budget: Optional[float] = Field(None, gt=0)
    bedrooms: Optional[int] = Field(None, ge=0, le=50)
    preferred_property_type: Optional[str] = Field(
        None, pattern="^(" + "|".join(PROPERTY_TYPES) + ")$"
    )
    transport_mode: Optional[str] = Field(
        None, pattern="^(walking|matatu|bus|boda|car|mixed)$"
    )
    commute_limit_minutes: Optional[int] = Field(None, gt=0, le=180)
    safety_importance: Optional[int] = Field(None, ge=1, le=5)
    water_importance: Optional[int] = Field(None, ge=1, le=5)
    internet_importance: Optional[int] = Field(None, ge=1, le=5)
    parking_required: bool = False
    quietness_preference: Optional[int] = Field(None, ge=1, le=5)
    furnished_preference: Optional[str] = Field(
        None, pattern="^(furnished|unfurnished|no_preference)$"
    )
    preferred_neighbourhoods: List[str] = Field(default_factory=list)
    household_size: Optional[int] = Field(None, ge=1, le=30)
    pets: bool = False
    other_preferences: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def budget_sanity(self) -> "UserPreferencesUpsert":
        if (
            self.rent_budget is not None
            and self.total_housing_budget is not None
            and self.rent_budget > self.total_housing_budget
        ):
            raise ValueError(
                "rent_budget cannot exceed total_housing_budget."
            )
        return self


class UserPreferencesResponse(BaseModel):
    user_id: int
    workplace_latitude: Optional[float] = None
    workplace_longitude: Optional[float] = None
    workplace_name: Optional[str] = None
    income_range: Optional[str] = None
    rent_budget: Optional[float] = None
    total_housing_budget: Optional[float] = None
    bedrooms: Optional[int] = None
    preferred_property_type: Optional[str] = None
    transport_mode: Optional[str] = None
    commute_limit_minutes: Optional[int] = None
    safety_importance: Optional[int] = None
    water_importance: Optional[int] = None
    internet_importance: Optional[int] = None
    parking_required: bool
    quietness_preference: Optional[int] = None
    furnished_preference: Optional[str] = None
    preferred_neighbourhoods: List[str] = []
    household_size: Optional[int] = None
    pets: bool
    other_preferences: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ===========================================================================
# ── SAVED SEARCHES (P1 stretch) ─────────────────────────────────────────────
# ===========================================================================

class SavedSearchCreate(BaseModel):
    """POST /saved-searches  (protected — renter)"""
    search_criteria: ListingFilters
    search_type: str = Field("residential", pattern="^(residential|commercial)$")


class SavedSearchResponse(BaseModel):
    id: int
    user_id: int
    search_criteria: Dict[str, Any]
    search_type: str
    created_at: datetime
    last_run_at: Optional[datetime] = None


# ===========================================================================
# ── FAVORITES (P1 stretch) ──────────────────────────────────────────────────
# ===========================================================================

class FavoriteCreate(BaseModel):
    """POST /favorites  (protected — renter). Keyed on property_id."""
    property_id: int


class FavoriteResponse(BaseModel):
    id: int
    user_id: int
    property_id: int
    created_at: datetime


# ===========================================================================
# ── PAYMENTS ─────────────────────────────────────────────────────────────────
# ===========================================================================

class MpesaPaymentRequest(BaseModel):
    """
    POST /payments/mpesa/initiate  (protected)
    Frontend sends the host's M-Pesa number and what they are paying for.
    """
    phone: str = Field(..., min_length=9, max_length=20)
    package_type: str          # 'landlord' | 'agency' | booster key
    listing_id: Optional[int] = None   # required for booster purchases

    @field_validator("phone")
    @classmethod
    def normalise_phone(cls, v: str) -> str:
        """Daraja STK push requires format: 2547XXXXXXXX (no +)."""
        digits = re.sub(r"\D", "", v)
        if digits.startswith("0") and len(digits) == 10:
            digits = "254" + digits[1:]
        if digits.startswith("254") and len(digits) == 12:
            return digits
        raise ValueError("Enter a valid Safaricom M-Pesa number (e.g. 0712345678).")


class MpesaCallbackPayload(BaseModel):
    """
    POST /payments/mpesa/callback
    Payload received from Safaricom Daraja after STK push completes.
    Only the fields Makao needs are modelled here.
    """
    Body: Dict[str, Any]       # Daraja wraps everything in Body


class StripePaymentRequest(BaseModel):
    """
    POST /payments/stripe/create-intent  (protected)
    Frontend requests a PaymentIntent for card or PayPal flow.
    """
    package_type: str          # 'landlord' | 'agency' | booster key
    payment_method_type: str = Field(..., pattern="^(card|paypal)$")
    listing_id: Optional[int] = None


class StripeWebhookEvent(BaseModel):
    """
    POST /payments/stripe/webhook  (public — verified by Stripe signature)
    Only the fields needed to process payment confirmation.
    """
    id: str
    type: str                  # e.g. 'payment_intent.succeeded'
    data: Dict[str, Any]


class PaymentResponse(BaseModel):
    """Shape of a single payment record (billing history page)."""
    id: int
    payment_method: str
    transaction_id: Optional[str] = None
    amount: float
    currency: str = "KES"
    package_type: str
    status: str                # 'pending' | 'completed' | 'failed' | 'refunded'
    created_at: datetime


class MpesaInitiateResponse(BaseModel):
    """
    Returned to the frontend after STK push is triggered.
    Frontend polls /payments/mpesa/status/{checkout_request_id}.
    """
    ok: bool = True
    checkout_request_id: str
    message: str = "Check your phone and enter your M-Pesa PIN to complete payment."


class PaymentStatusResponse(BaseModel):
    """Returned when frontend polls for payment status."""
    status: str                # 'pending' | 'completed' | 'failed'
    message: str


# ===========================================================================
# ── BOOSTERS ─────────────────────────────────────────────────────────────────
# ===========================================================================

class BoosterPurchaseRequest(BaseModel):
    """
    POST /boosters/purchase  (protected)
    Host selects a booster for one of their listings.
    """
    listing_id: int
    booster_type: str = Field(
        ...,
        pattern="^(homepage_feature|chatbot_priority|refresh_listing|extend_visibility)$",
    )
    payment_method: str = Field(..., pattern="^(mpesa|stripe_card|stripe_paypal)$")
    phone: Optional[str] = None   # required when payment_method = 'mpesa'

    @model_validator(mode="after")
    def phone_required_for_mpesa(self) -> "BoosterPurchaseRequest":
        if self.payment_method == "mpesa" and not self.phone:
            raise ValueError("phone is required for M-Pesa payment.")
        return self


class BoosterResponse(BaseModel):
    """Shape of an active booster record."""
    id: int
    listing_id: int
    booster_type: str
    start_date: datetime
    end_date: Optional[datetime] = None
    is_active: bool


# ===========================================================================
# ── CHATBOT ──────────────────────────────────────────────────────────────────
# ===========================================================================

class ChatMessage(BaseModel):
    """A single message turn in the chat widget."""
    role: str = Field(..., pattern="^(user|assistant|system)$")
    message: str = Field(..., min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    """
    POST /chat/message
    Sent by chatbot.js each time the visitor types a message.
    session_id is generated client-side (UUID v4) and persisted in localStorage.
    """
    session_id: str = Field(..., min_length=8, max_length=100)
    message: str = Field(..., min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    """
    Returned to the chat widget after Groq processes the message.
    listings is populated only when the AI has matched properties.
    """
    session_id: str
    reply: str
    listings: Optional[List[ListingCardResponse]] = None   # matched properties


class ConversationHistoryResponse(BaseModel):
    """GET /chat/history/{session_id} — for restoring a chat session."""
    session_id: str
    messages: List[ChatMessage]


# ===========================================================================
# ── VISITOR PREFERENCES (internal — chatbot → matcher) ──────────────────────
# ===========================================================================

class VisitorPreferences(BaseModel):
    """
    Structured preferences extracted from the chat conversation by Groq.
    Passed to matcher.py (OpenAI GPT-4o) alongside candidate listings.
    All fields optional — filled in as the conversation progresses.
    """
    county: Optional[str] = None
    city: Optional[str] = None
    min_budget: Optional[float] = None
    max_budget: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    amenities: List[str] = []
    house_type: Optional[str] = None     # 'bedsitter' | '1 bed' | 'bungalow' etc.
    move_in_urgency: Optional[str] = None  # 'immediately' | 'this month' | 'flexible'
    raw_notes: Optional[str] = None      # any extra context from the conversation


class MatchRequest(BaseModel):
    """
    Internal payload sent from chatbot.py → matcher.py.
    Not exposed as an API endpoint.
    """
    preferences: VisitorPreferences
    candidate_listings: List[Dict[str, Any]]


class MatchResult(BaseModel):
    """
    Single ranked listing returned by the GPT-4o matcher.
    explanation is shown to the visitor in the chat window.
    """
    listing_id: int
    title: str
    city: str
    county: str
    price_per_month: float
    bedrooms: int
    bathrooms: int
    photos: List[str] = []
    explanation: str           # why this listing matches the visitor's needs
    match_score: float = Field(..., ge=0.0, le=1.0)


class MatchResponse(BaseModel):
    """List of ranked matches returned from matcher.py to chatbot.py."""
    matches: List[MatchResult]
    summary: str               # Groq-friendly summary of the top matches


# ===========================================================================
# ── DASHBOARD ────────────────────────────────────────────────────────────────
# ===========================================================================

class DashboardResponse(BaseModel):
    """
    GET /hosts/dashboard  (protected)
    Aggregated data for the host dashboard page.
    """
    host: UserResponse
    listings: List[ListingResponse]
    payments: List[PaymentResponse]
    active_boosters: List[BoosterResponse]
    listing_count: int
    active_listing_count: int


class AnalyticsListingRow(BaseModel):
    """Single row in the analytics table (agency dashboard)."""
    id: int
    title: str
    city: str
    county: str
    is_available: bool
    is_featured: bool
    visibility_rank: int
    active_boosters: int
    total_spent: float
    created_at: datetime


class AnalyticsResponse(BaseModel):
    """
    GET /hosts/analytics  (protected — agency only)
    Per-listing stats for the analytics page.
    """
    listings: List[AnalyticsListingRow]
    total_listings: int
    total_spent: float


# ===========================================================================
# ── PRICING PAGE ─────────────────────────────────────────────────────────────
# ===========================================================================

class PackageDetail(BaseModel):
    """
    Shape of a single listing package as returned by GET /pricing.
    Prices always read from config.py — never hardcoded here.
    """
    name: str
    price: float               # always 0 in code; owner sets real value in config.py
    listings: int              # max listings per purchase
    photos: int                # -1 = unlimited
    chatbot: bool
    featured: bool
    verified_badge: bool


class BoosterDetail(BaseModel):
    """Shape of a single booster option as returned by GET /pricing."""
    name: str
    price: float               # always 0 in code; owner sets in config.py
    duration_days: Optional[int] = None   # None = permanent until cancelled


class PricingResponse(BaseModel):
    """
    GET /pricing  (public)
    Returned to the pricing page — populated entirely from config.py.
    """
    packages: Dict[str, PackageDetail]    # keys: 'landlord', 'agency'
    boosters: Dict[str, BoosterDetail]    # keys: booster slug names


# forward reference resolution (Python < 3.10 compatibility)
TokenResponse.model_rebuild()
