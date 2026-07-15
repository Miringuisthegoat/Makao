"""
auth.py — Makao Rental Platform
=================================
All authentication logic lives here:
  - Password hashing and verification (bcrypt)
  - JWT token creation and decoding
  - Signup and login business logic
  - FastAPI dependency: get_current_host (protects routes)
  - Agency-only guard: require_agency

No database queries here — all DB calls go through database.py.
No routes defined here — routes are registered in main.py.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config import PLATFORM
from database import create_user, get_user_by_email, get_user_by_id
from models import LoginRequest, SignupRequest, TokenResponse, UserResponse

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read from .env, never hardcoded
# ---------------------------------------------------------------------------
JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRE_HOURS: int = PLATFORM["jwt_expire_hours"]

if not JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set in .env. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

# HTTPBearer extracts the token from the Authorization: Bearer <token> header
_bearer_scheme = HTTPBearer(auto_error=False)


# ===========================================================================
# ── PASSWORD HELPERS ─────────────────────────────────────────────────────────
# ===========================================================================

def hash_password(plain: str) -> str:
    """
    Hash a plain-text password with bcrypt (work factor 12).
    Returns the hashed string for storage in the DB.
    """
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """
    Compare a plain-text password against a stored bcrypt hash.
    Returns True if they match, False otherwise.
    Timing-safe — bcrypt handles constant-time comparison internally.
    """
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ===========================================================================
# ── JWT HELPERS ──────────────────────────────────────────────────────────────
# ===========================================================================

def create_access_token(user_id: int, host_type: str) -> str:
    """
    Create a signed JWT containing the user's id and host_type.
    Expires after JWT_EXPIRE_HOURS (default 24h, set in .env).
    """
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(user_id),       # subject — user primary key
        "host_type": host_type,    # 'landlord' | 'agency'
        "iat": now,                # issued at
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and verify a JWT. Returns the payload dict.
    Raises HTTPException 401 if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        logger.warning(f"JWT decode failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ===========================================================================
# ── FASTAPI DEPENDENCIES ─────────────────────────────────────────────────────
# ===========================================================================

async def get_current_host(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> UserResponse:
    """
    FastAPI dependency — inject into any protected route.
    Extracts the JWT from the Authorization header, verifies it,
    and returns the UserResponse for the authenticated host.

    Usage in main.py:
        @app.get("/hosts/me")
        async def me(host: UserResponse = Depends(get_current_host)):
            return host

    Raises 401 if:
      - No Authorization header is present
      - Token is missing, expired, or tampered with
      - User no longer exists in the database
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token — missing subject.",
        )

    user = await get_user_by_id(int(user_id_str))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found. It may have been deleted.",
        )

    if user["plan_status"] == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Please contact support.",
        )

    return UserResponse(**user)


async def require_agency(
    host: UserResponse = Depends(get_current_host),
) -> UserResponse:
    """
    FastAPI dependency — restrict a route to agency accounts only.
    Used on the analytics page endpoint.

    Usage in main.py:
        @app.get("/hosts/analytics")
        async def analytics(host: UserResponse = Depends(require_agency)):
            ...

    Raises 403 if the authenticated host is not an agency.
    """
    if host.host_type != "agency":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature is available to Agency accounts only.",
        )
    return host


# ===========================================================================
# ── SIGNUP ───────────────────────────────────────────────────────────────────
# ===========================================================================

async def signup_host(data: SignupRequest) -> TokenResponse:
    """
    Register a new host account.

    Steps:
      1. Check email is not already registered
      2. Hash the password with bcrypt
      3. Insert user row into DB (plan_status = 'inactive' by default)
      4. Issue a JWT and return TokenResponse

    Raises:
      409 Conflict  — email already registered
      500           — unexpected DB error
    """
    # 1. Duplicate email check
    existing = await get_user_by_email(data.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists. Please log in.",
        )

    # 2. Hash password — never store plain text
    hashed = hash_password(data.password)

    # 3. Persist to database
    try:
        user = await create_user(
            full_name=data.full_name,
            email=data.email,
            password_hash=hashed,
            phone=data.phone,
            host_type=data.host_type,
        )
    except Exception as exc:
        logger.error(f"Signup DB error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create account. Please try again.",
        )

    # 4. Issue token
    token = create_access_token(user["id"], user["host_type"])
    user_response = UserResponse(**user)

    logger.info(f"New host registered: id={user['id']} type={user['host_type']}")

    return TokenResponse(
        access_token=token,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        host=user_response,
    )


# ===========================================================================
# ── LOGIN ────────────────────────────────────────────────────────────────────
# ===========================================================================

async def login_host(data: LoginRequest) -> TokenResponse:
    """
    Authenticate a host and return a JWT.

    Steps:
      1. Look up user by email
      2. Verify password against stored bcrypt hash
      3. Check account is not suspended
      4. Issue a JWT and return TokenResponse

    Raises:
      401 — email not found or wrong password
            (deliberate vague message — don't reveal which is wrong)
      403 — account suspended
    """
    # 1. Fetch user — same error for not-found and wrong password (security)
    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user = await get_user_by_email(data.email)
    if not user:
        raise _invalid

    # 2. Verify password
    if not verify_password(data.password, user["password_hash"]):
        logger.warning(f"Failed login attempt for email: {data.email}")
        raise _invalid

    # 3. Suspended account check
    if user["plan_status"] == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Please contact support.",
        )

    # 4. Issue token
    token = create_access_token(user["id"], user["host_type"])
    user_response = UserResponse(**user)

    logger.info(f"Host logged in: id={user['id']}")

    return TokenResponse(
        access_token=token,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        host=user_response,
    )


# ===========================================================================
# ── PASSWORD CHANGE ──────────────────────────────────────────────────────────
# ===========================================================================

async def change_password(
    host: UserResponse,
    current_password: str,
    new_password: str,
) -> dict:
    """
    Allow an authenticated host to change their password.

    Steps:
      1. Re-fetch full user row (UserResponse omits password_hash)
      2. Verify current password
      3. Hash and save the new password

    Returns a simple success dict.
    Raises 401 if current_password is wrong.
    """
    # 1. Need the full DB row to get password_hash
    user = await get_user_by_id(host.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # 2. Verify current password
    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    # 3. Hash and update
    new_hash = hash_password(new_password)

    from database import update_user  # local import avoids circular dependency
    await update_user(host.id, {"password_hash": new_hash})

    # Extend allowed fields in update_user if needed — password_hash is
    # intentionally excluded from the default allowed set for safety,
    # so this specific case goes through a direct DB update:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = $2 WHERE id = $1",
            host.id, new_hash,
        )

    logger.info(f"Password changed for host id={host.id}")
    return {"ok": True, "message": "Password updated successfully."}