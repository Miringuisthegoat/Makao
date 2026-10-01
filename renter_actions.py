"""
renter_actions.py — Makao Phase 6: the renter journey after search.

Routes (all under /api, none payment-gated):
  POST   /renter/signup, /renter/login, GET /renter/me
  GET/POST/DELETE /favorites                    (wraps preferences.py)
  POST   /listings/{id}/fit                     score + explanation for ONE listing
  POST   /listings/{id}/viewing-requests        (renter)
  GET    /viewing-requests/mine                 (renter)
  PATCH  /viewing-requests/{id}/cancel          (renter)
  POST   /listings/{id}/contact                 (anonymous OK, rate-limited)
  GET    /listings/{id}/share
  GET    /host/viewing-requests, PATCH /host/viewing-requests/{id}   (any host)
  GET    /host/leads

Register in main.py:
    from renter_actions import router as renter_router
    app.include_router(renter_router, prefix="/api", tags=["Renter"])
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

import actions_rules as rules
import auth as auth_logic
import database as db
import preferences as preferences_logic
from intelligence.explanation import explain
from intelligence.preference_extractor import groq_llm_call, to_renter_preferences
from intelligence.scoring import ListingSnapshot, compute_match_score
from models import FavoriteCreate, UserPreferencesUpsert

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
_contact_limiter = rules.RateLimiter(max_events=10, window_seconds=600)
_auth_limiter = rules.RateLimiter(max_events=10, window_seconds=300)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ===========================================================================
# ── ACCOUNT DEPENDENCIES (any account type; never checks plan/payment) ─────
# ===========================================================================
class Account(BaseModel):
    id: int
    full_name: str
    email: str
    phone: Optional[str] = None
    account_type: str


async def _load_account(credentials: Optional[HTTPAuthorizationCredentials]) -> Optional[Account]:
    if not credentials:
        return None
    payload = auth_logic.decode_access_token(credentials.credentials)
    try:
        user_id = int(payload.get("sub", ""))
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token.")
    user = await db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found.")
    if user.get("plan_status") == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has been suspended.")
    return Account(
        id=user["id"], full_name=user["full_name"], email=user["email"], phone=user.get("phone"),
        account_type=user.get("account_type") or user.get("host_type") or "renter",
    )


async def get_account(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Account:
    acct = await _load_account(credentials)
    if not acct:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in to continue.",
                            headers={"WWW-Authenticate": "Bearer"})
    return acct


async def get_optional_account(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Optional[Account]:
    return await _load_account(credentials)


# ===========================================================================
# ── RENTER SIGNUP / LOGIN ───────────────────────────────────────────────────
# ===========================================================================
class RenterSignup(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    phone: str = Field(..., min_length=9, max_length=20)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        d = rules.ke_phone_digits(v)
        if not d:
            raise ValueError("Enter a valid Kenyan phone number (e.g. 0712345678).")
        return "+" + d

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return re.sub(r"\s+", " ", v.strip())


class RenterLogin(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


def _token_response(user: Dict[str, Any], account_type: str) -> Dict[str, Any]:
    return {
        "access_token": auth_logic.create_access_token(user["id"], account_type),
        "token_type": "bearer",
        "expires_in": auth_logic.JWT_EXPIRE_HOURS * 3600,
        "user": {"id": user["id"], "full_name": user["full_name"], "email": user["email"],
                 "phone": user.get("phone"), "account_type": account_type},
    }


@router.post("/renter/signup", status_code=201)
async def renter_signup(body: RenterSignup, request: Request):
    if not _auth_limiter.allow(f"signup:{_ip(request)}"):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    if await db.get_user_by_email(body.email):
        raise HTTPException(409, "An account with this email already exists. Please log in.")
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (full_name, email, password_hash, phone, account_type)
                VALUES ($1, $2, $3, $4, 'renter')
                RETURNING id, full_name, email, phone
                """,
                body.full_name, body.email, auth_logic.hash_password(body.password), body.phone,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "An account with this email already exists. Please log in.")
    return _token_response(dict(row), "renter")


@router.post("/renter/login")
async def renter_login(body: RenterLogin, request: Request):
    if not _auth_limiter.allow(f"login:{_ip(request)}"):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    user = await db.get_user_by_email(body.email)
    if not user or not auth_logic.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Incorrect email or password.")
    if user.get("plan_status") == "suspended":
        raise HTTPException(403, "Your account has been suspended.")
    return _token_response(user, user.get("account_type") or user.get("host_type") or "renter")


@router.get("/renter/me")
async def renter_me(account: Account = Depends(get_account)):
    return account


# ===========================================================================
# ── FAVORITES (property-keyed; thin wrapper over preferences.py) ────────────
# ===========================================================================
@router.get("/favorites")
async def favorites_list(account: Account = Depends(get_account)):
    return await preferences_logic.list_favorites(account.id)


@router.post("/favorites", status_code=201)
async def favorites_add(body: FavoriteCreate, account: Account = Depends(get_account)):
    return await preferences_logic.add_favorite(account.id, body)


@router.delete("/favorites/{property_id}")
async def favorites_remove(property_id: int, account: Account = Depends(get_account)):
    return await preferences_logic.remove_favorite(account.id, property_id)


# ===========================================================================
# ── SHARED LISTING LOOKUP ───────────────────────────────────────────────────
# ===========================================================================
async def _active_listing(listing_id: int) -> Dict[str, Any]:
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(404, f"Listing {listing_id} not found.")
    if row["status"] != "active":
        raise HTTPException(409, "This listing is no longer active.")
    return row


# ===========================================================================
# ── FIT: match score + "why this fits you" for a single listing ─────────────
# Ranking authority stays in scoring.py. The LLM only rephrases (explanation.py).
# ===========================================================================
class FitRequest(BaseModel):
    preferences: UserPreferencesUpsert
    estimated_monthly_cost: Optional[float] = Field(None, ge=0, le=5_000_000)
    commute_minutes: Optional[float] = Field(None, ge=0, le=600)
    use_ai: bool = True


@router.post("/listings/{listing_id}/fit")
async def listing_fit(listing_id: int, body: FitRequest):
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(404, f"Listing {listing_id} not found.")
    latest = await db.get_latest_availability(listing_id)
    snap = ListingSnapshot(
        listing_id=row["id"], property_id=row["property_id"],
        asking_price=float(row["asking_price"]),
        service_charge=float(row["service_charge"] or 0), deposit=float(row["deposit"] or 0),
        property_type=row["property_type"], bedrooms=row["bedrooms"], bathrooms=row["bathrooms"],
        furnished=bool(row["furnished"]), parking_spaces=row["parking_spaces"] or 0,
        latitude=row.get("loc_latitude"), longitude=row.get("loc_longitude"),
        neighbourhood=row.get("loc_neighbourhood"),
        last_confirmed_at=row.get("last_confirmed_at"),
        availability_status=(latest or {}).get("status", "uncertain"),
        host_verification_status=row.get("host_verification_status") or "unverified",
        estimated_monthly_cost=body.estimated_monthly_cost,
        commute_minutes=body.commute_minutes,
        **rules.amenity_flags(row.get("amenities")),
    )
    score = compute_match_score(snap, to_renter_preferences(body.preferences.model_dump())).to_dict()
    facts = {"asking_price": snap.asking_price, "estimated_monthly_cost": body.estimated_monthly_cost,
             "commute_minutes": body.commute_minutes}
    explanation = await explain(score, {k: v for k, v in facts.items() if v is not None},
                                llm_call=groq_llm_call if body.use_ai else None)
    return {"score": score, "explanation": explanation,
            "freshness": rules.freshness_label((latest or {}).get("status"), row.get("last_confirmed_at")),
            "verification": rules.verification_label(row.get("host_verification_status"))}


# ===========================================================================
# ── VIEWING REQUESTS ────────────────────────────────────────────────────────
# ===========================================================================
class ViewingCreate(BaseModel):
    requested_time: datetime
    message: Optional[str] = Field(None, max_length=500)


class ViewingHostUpdate(BaseModel):
    status: Literal["confirmed", "completed", "cancelled", "no_show"]
    host_note: Optional[str] = Field(None, max_length=500)


_VIEWING_SELECT = """
    SELECT v.*, p.title, loc.neighbourhood, l.asking_price,
           h.full_name AS host_name, r.full_name AS renter_name, r.phone AS renter_phone
    FROM viewing_requests v
    JOIN listings l   ON l.id = v.listing_id
    JOIN properties p ON p.id = v.property_id
    LEFT JOIN locations loc ON loc.id = p.location_id
    JOIN users h ON h.id = v.host_id
    JOIN users r ON r.id = v.user_id
"""


@router.post("/listings/{listing_id}/viewing-requests", status_code=201)
async def viewing_create(listing_id: int, body: ViewingCreate, account: Account = Depends(get_account)):
    listing = await _active_listing(listing_id)
    if listing["host_id"] == account.id:
        raise HTTPException(400, "You cannot request a viewing of your own listing.")
    err = rules.validate_requested_time(body.requested_time)
    if err:
        raise HTTPException(422, err)
    latest = await db.get_latest_availability(listing_id)
    if latest and latest["status"] == "unavailable":
        raise HTTPException(409, "The host has marked this listing as unavailable.")
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO viewing_requests
                    (user_id, property_id, listing_id, host_id, requested_time, message)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING *
                """,
                account.id, listing["property_id"], listing_id, listing["host_id"],
                body.requested_time, (body.message or "").strip() or None,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "You already have an active viewing request for this listing.")
    return dict(row)


@router.get("/viewing-requests/mine")
async def viewing_mine(account: Account = Depends(get_account)):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _VIEWING_SELECT + " WHERE v.user_id = $1 ORDER BY v.requested_time DESC LIMIT 100", account.id)
    return [dict(r) for r in rows]


async def _apply_transition(vid: int, actor: str, account: Account, new: str,
                            host_note: Optional[str] = None) -> Dict[str, Any]:
    owner_col = "host_id" if actor == "host" else "user_id"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT * FROM viewing_requests WHERE id = $1 AND {owner_col} = $2 FOR UPDATE",
                vid, account.id)
            if not row:  # same 404 whether missing or not yours — no ID probing
                raise HTTPException(404, "Viewing request not found.")
            err = rules.transition_error(actor, row["status"], new, row["requested_time"])
            if err:
                raise HTTPException(409, err)
            updated = await conn.fetchrow(
                "UPDATE viewing_requests SET status = $2, host_note = COALESCE($3, host_note) "
                "WHERE id = $1 RETURNING *", vid, new, host_note)
    return dict(updated)


@router.patch("/viewing-requests/{vid}/cancel")
async def viewing_cancel(vid: int, account: Account = Depends(get_account)):
    return await _apply_transition(vid, "renter", account, "cancelled")


@router.get("/host/viewing-requests")
async def host_viewings(
    status_filter: Optional[Literal["requested", "confirmed", "completed", "cancelled", "no_show"]] = Query(None, alias="status"),
    account: Account = Depends(get_account),
):
    sql, args = _VIEWING_SELECT + " WHERE v.host_id = $1", [account.id]
    if status_filter:
        sql += " AND v.status = $2"
        args.append(status_filter)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql + " ORDER BY v.requested_time DESC LIMIT 200", *args)
    return [dict(r) for r in rows]


@router.patch("/host/viewing-requests/{vid}")
async def host_viewing_update(vid: int, body: ViewingHostUpdate, account: Account = Depends(get_account)):
    return await _apply_transition(vid, "host", account, body.status, body.host_note)


# ===========================================================================
# ── CONTACT (call / WhatsApp / message) ─────────────────────────────────────
# ===========================================================================
class ContactCreate(BaseModel):
    channel: Literal["call", "whatsapp", "message"] = "message"
    name: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=20)
    message: Optional[str] = Field(None, max_length=1000)


@router.post("/listings/{listing_id}/contact")
async def listing_contact(listing_id: int, body: ContactCreate, request: Request,
                          account: Optional[Account] = Depends(get_optional_account)):
    if not _contact_limiter.allow(f"{_ip(request)}:{account.id if account else 'anon'}"):
        raise HTTPException(429, "Too many contact attempts. Please try again later.")
    listing = await _active_listing(listing_id)

    name = (body.name or (account.full_name if account else "") or "").strip() or None
    phone = body.phone or (account.phone if account else None)
    message = (body.message or "").strip() or None
    if body.channel == "message":
        if not (name and message and rules.ke_phone_digits(phone)):
            raise HTTPException(422, "Name, a valid Kenyan phone number and a message are required.")
    phone_norm = "+" + rules.ke_phone_digits(phone) if rules.ke_phone_digits(phone) else None

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO contact_requests
               (listing_id, property_id, host_id, user_id, channel, name, phone, message)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            listing_id, listing["property_id"], listing["host_id"],
            account.id if account else None, body.channel, name, phone_norm, message)
        host = await conn.fetchrow("SELECT full_name, phone FROM users WHERE id = $1", listing["host_id"])

    out: Dict[str, Any] = {"ok": True, "host_name": host["full_name"] if host else None}
    if body.channel in ("call", "whatsapp") and host:
        text = f"Hi, I'm interested in \"{listing['title']}\" on Makao (listing #{listing_id}). Is it still available?"
        out["tel_url"] = rules.tel_url(host["phone"])
        out["whatsapp_url"] = rules.whatsapp_url(host["phone"], text)
    return out


@router.get("/host/leads")
async def host_leads(account: Account = Depends(get_account)):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.listing_id, p.title, c.channel, c.name, c.phone, c.message, c.created_at
               FROM contact_requests c JOIN properties p ON p.id = c.property_id
               WHERE c.host_id = $1 ORDER BY c.created_at DESC LIMIT 200""", account.id)
    return [dict(r) for r in rows]


# ===========================================================================
# ── SHARE ───────────────────────────────────────────────────────────────────
# ===========================================================================
@router.get("/listings/{listing_id}/share")
async def listing_share(listing_id: int):
    row = await db.get_listing_by_id(listing_id)
    if not row:
        raise HTTPException(404, f"Listing {listing_id} not found.")
    return rules.build_share(row["id"], row["title"], float(row["asking_price"]),
                             row.get("loc_neighbourhood"), PUBLIC_BASE_URL)
