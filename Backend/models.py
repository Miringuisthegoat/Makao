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
# ── LISTINGS ─────────────────────────────────────────────────────────────────
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


class ListingCreate(BaseModel):
    """
    POST /listings  (protected)
    Sent from add-listing.html after host has filled the form.
    Listing is created inactive; activates only after payment confirmed.
    """
    title: str = Field(..., min_length=5, max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    location: Optional[str] = Field(None, max_length=255)   # estate / street
    city: str = Field(..., min_length=2, max_length=100)
    county: str = Field(..., min_length=2, max_length=100)
    price_per_month: float = Field(..., gt=0)
    bedrooms: int = Field(..., ge=0, le=50)   # 0 = bedsitter / studio
    bathrooms: int = Field(..., ge=1, le=20)
    size_sqft: Optional[int] = Field(None, gt=0)
    amenities: List[str] = Field(default_factory=list)
    photos: List[str] = Field(default_factory=list)          # URLs after upload
    package_type: str = Field(..., pattern="^(landlord|agency)$")

    @field_validator("county")
    @classmethod
    def validate_county(cls, v: str) -> str:
        # Title-case the input so "nairobi" → "Nairobi"
        normalised = v.strip().title()
        if normalised not in KENYAN_COUNTIES:
            raise ValueError(f"'{v}' is not a recognised Kenyan county.")
        return normalised

    @field_validator("amenities")
    @classmethod
    def validate_amenities(cls, v: List[str]) -> List[str]:
        invalid = [a for a in v if a.lower() not in VALID_AMENITIES]
        if invalid:
            raise ValueError(f"Unknown amenities: {invalid}. "
                             f"Valid options: {sorted(VALID_AMENITIES)}")
        return [a.lower() for a in v]

    @field_validator("photos")
    @classmethod
    def validate_photos(cls, v: List[str]) -> List[str]:
        if len(v) > 50:
            raise ValueError("Maximum 50 photos per listing.")
        return v


class ListingUpdate(BaseModel):
    """
    PATCH /listings/{id}  (protected — host only)
    All fields optional; only supplied fields are written to DB.
    """
    title: Optional[str] = Field(None, min_length=5, max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    location: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, min_length=2, max_length=100)
    county: Optional[str] = Field(None, min_length=2, max_length=100)
    price_per_month: Optional[float] = Field(None, gt=0)
    bedrooms: Optional[int] = Field(None, ge=0, le=50)
    bathrooms: Optional[int] = Field(None, ge=1, le=20)
    size_sqft: Optional[int] = Field(None, gt=0)
    amenities: Optional[List[str]] = None
    photos: Optional[List[str]] = None
    is_available: Optional[bool] = None

    @field_validator("county")
    @classmethod
    def validate_county(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        normalised = v.strip().title()
        if normalised not in KENYAN_COUNTIES:
            raise ValueError(f"'{v}' is not a recognised Kenyan county.")
        return normalised

    @field_validator("amenities")
    @classmethod
    def validate_amenities(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        invalid = [a for a in v if a.lower() not in VALID_AMENITIES]
        if invalid:
            raise ValueError(f"Unknown amenities: {invalid}.")
        return [a.lower() for a in v]


class ListingResponse(BaseModel):
    """
    Full listing shape returned to the client.
    Used on: single listing page, dashboard, AI matcher results.
    """
    id: int
    host_id: int
    title: str
    description: Optional[str] = None
    location: Optional[str] = None
    city: str
    county: str
    price_per_month: float
    bedrooms: int
    bathrooms: int
    size_sqft: Optional[int] = None
    amenities: List[str] = []
    photos: List[str] = []
    is_available: bool
    is_featured: bool
    package_type: str
    visibility_rank: int
    created_at: datetime
    updated_at: datetime
    # Joined from users table
    host_name: Optional[str] = None
    host_account_type: Optional[str] = None
    host_photo: Optional[str] = None


class ListingCardResponse(BaseModel):
    """
    Lightweight listing shape for listing cards (browse page, homepage).
    Omits description and full amenities list to reduce payload.
    """
    id: int
    title: str
    city: str
    county: str
    price_per_month: float
    bedrooms: int
    bathrooms: int
    photos: List[str] = []
    is_featured: bool
    package_type: str
    host_name: Optional[str] = None
    host_account_type: Optional[str] = None


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
    All optional — absence means no filter applied.
    """
    county: Optional[str] = None
    city: Optional[str] = None
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