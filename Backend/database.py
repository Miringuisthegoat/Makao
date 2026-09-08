"""
database.py — Makao Rental Platform
====================================
All database connections and queries live here.
No raw SQL should appear in any other file.

Supports: PostgreSQL (primary) or MySQL (fallback).
Connection pooling via asyncpg (PostgreSQL) or aiomysql (MySQL).
"""

import os
import json
import asyncpg
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

try:
    import asyncpg
except ImportError:
    raise ImportError(
        "asyncpg is required. Install it with: pip install asyncpg"
    )
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool (module-level singleton)
# ---------------------------------------------------------------------------
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", 5432)),
            database=os.getenv("DB_NAME", "makao"),
            user=os.getenv("DB_USER", "makao_user"),
            password=os.getenv("DB_PASSWORD", ""),
            # Neon (and most hosted Postgres) requires TLS. Set DB_SSLMODE=disable
            # in .env for a local/Docker Postgres that has no TLS listener.
            ssl=os.getenv("DB_SSLMODE", "disable"),
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("Database connection pool created.")
    return _pool


async def close_pool():
    """Gracefully close the connection pool on app shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")


# ---------------------------------------------------------------------------
# DEPRECATED (Phase 0): schema is now owned by Alembic migrations under
# Backend/migrations/versions/. This SQL is kept ONLY as a historical
# reference for what init_db() used to create — it is no longer executed.
# Run `alembic upgrade head` to create/update the schema instead.
# ---------------------------------------------------------------------------
_LEGACY_CREATE_TABLES_SQL = """
-- ── Users (hosts: landlords and agencies) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                SERIAL PRIMARY KEY,
    full_name         VARCHAR(120)  NOT NULL,
    email             VARCHAR(255)  NOT NULL UNIQUE,
    password_hash     TEXT          NOT NULL,
    phone             VARCHAR(20),
    profile_photo     TEXT,                          -- URL / path
    host_type         VARCHAR(20)   NOT NULL         -- 'landlord' | 'agency'
                        CHECK (host_type IN ('landlord', 'agency')),
    stripe_customer_id VARCHAR(100),
    plan_status       VARCHAR(20)   NOT NULL DEFAULT 'inactive'
                        CHECK (plan_status IN ('active', 'inactive', 'suspended')),
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ── Listings ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS listings (
    id               SERIAL PRIMARY KEY,
    host_id          INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            VARCHAR(200)  NOT NULL,
    description      TEXT,
    location         VARCHAR(255),                   -- street / estate
    city             VARCHAR(100),
    county           VARCHAR(100),
    price_per_month  NUMERIC(12,2) NOT NULL DEFAULT 0,
    bedrooms         SMALLINT      NOT NULL DEFAULT 1,
    bathrooms        SMALLINT      NOT NULL DEFAULT 1,
    size_sqft        INTEGER,
    amenities        JSONB         NOT NULL DEFAULT '[]',   -- ["wifi","parking",…]
    photos           JSONB         NOT NULL DEFAULT '[]',   -- list of URLs
    is_available     BOOLEAN       NOT NULL DEFAULT TRUE,
    is_featured      BOOLEAN       NOT NULL DEFAULT FALSE,
    package_type     VARCHAR(20)   NOT NULL DEFAULT 'landlord'
                        CHECK (package_type IN ('landlord', 'agency')),
    visibility_rank  INTEGER       NOT NULL DEFAULT 0,      -- higher = shown first
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Keep updated_at current automatically
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_listings_updated_at'
    ) THEN
        CREATE TRIGGER trg_listings_updated_at
        BEFORE UPDATE ON listings
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END;
$$;

-- ── Payments ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id               SERIAL PRIMARY KEY,
    host_id          INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_method   VARCHAR(30)   NOT NULL
                        CHECK (payment_method IN ('mpesa', 'stripe_card', 'stripe_paypal')),
    transaction_id   VARCHAR(200)  UNIQUE,           -- Mpesa CheckoutRequestID or Stripe PI id
    amount           NUMERIC(12,2) NOT NULL DEFAULT 0,
    currency         VARCHAR(5)    NOT NULL DEFAULT 'KES',
    package_type     VARCHAR(30)   NOT NULL,         -- 'landlord' | 'agency' | booster key
    status           VARCHAR(20)   NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'failed', 'refunded')),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ── Boosters ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS boosters (
    id               SERIAL PRIMARY KEY,
    listing_id       INTEGER       NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    booster_type     VARCHAR(50)   NOT NULL,         -- key from config.py BOOSTERS
    payment_id       INTEGER       REFERENCES payments(id),
    start_date       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    end_date         TIMESTAMPTZ,                    -- NULL = no expiry (e.g. chatbot_priority)
    is_active        BOOLEAN       NOT NULL DEFAULT TRUE
);

-- ── Conversations (chatbot history) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id               SERIAL PRIMARY KEY,
    session_id       VARCHAR(100)  NOT NULL,         -- anonymous visitor session UUID
    role             VARCHAR(10)   NOT NULL
                        CHECK (role IN ('user', 'assistant', 'system')),
    message          TEXT          NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_listings_host        ON listings(host_id);
CREATE INDEX IF NOT EXISTS idx_listings_county      ON listings(county);
CREATE INDEX IF NOT EXISTS idx_listings_city        ON listings(city);
CREATE INDEX IF NOT EXISTS idx_listings_available   ON listings(is_available);
CREATE INDEX IF NOT EXISTS idx_listings_featured    ON listings(is_featured);
CREATE INDEX IF NOT EXISTS idx_listings_rank        ON listings(visibility_rank DESC);
CREATE INDEX IF NOT EXISTS idx_payments_host        ON payments(host_id);
CREATE INDEX IF NOT EXISTS idx_payments_txn         ON payments(transaction_id);
CREATE INDEX IF NOT EXISTS idx_boosters_listing     ON boosters(listing_id);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
"""


async def init_db():
    """
    Verify the DB is reachable and migrated. Schema creation/changes now
    live exclusively in Alembic migrations (Backend/migrations/versions/) —
    this function intentionally does NOT run any DDL anymore.

    Run `alembic upgrade head` (from Backend/) before starting the app the
    first time, and after pulling any migration that adds new versions.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT version_num FROM alembic_version LIMIT 1"
        )
        if row is None:
            logger.warning(
                "No alembic_version found — database has not been migrated. "
                "Run `alembic upgrade head` in Backend/ before starting the app."
            )
        else:
            logger.info("Database schema at migration version: %s", row["version_num"])


# ===========================================================================
# ── USER QUERIES ────────────────────────────────────────────────────────────
# ===========================================================================

async def create_user(
    full_name: str,
    email: str,
    password_hash: str,
    phone: str,
    host_type: str,  # 'landlord' | 'agency'
) -> Dict[str, Any]:
    """Insert a new host account and return the created row."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (full_name, email, password_hash, phone, host_type)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, full_name, email, phone, host_type, plan_status, created_at
            """,
            full_name, email, password_hash, phone, host_type,
        )
        return dict(row)


async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Fetch a user row by email address (used during login)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE email = $1", email
        )
        return dict(row) if row else None


async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a user row by primary key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", user_id
        )
        return dict(row) if row else None


async def update_user(user_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Partially update a user row.
    Only these fields are allowed to be updated externally:
    full_name, phone, profile_photo, stripe_customer_id, plan_status
    """
    allowed = {"full_name", "phone", "profile_photo", "stripe_customer_id", "plan_status"}
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        raise ValueError("No valid fields to update.")

    # Build parameterised SET clause dynamically
    set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(safe))
    values = list(safe.values())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE users SET {set_clause} WHERE id = $1 "
            f"RETURNING id, full_name, email, phone, host_type, plan_status",
            user_id, *values,
        )
        return dict(row) if row else {}


# ===========================================================================
# ── LOCATIONS ────────────────────────────────────────────────────────────────
# ===========================================================================

async def resolve_or_create_location(location: Dict[str, Any]) -> int:
    """
    Find an existing `locations` row matching the given structured fields,
    or create one. Matching is on (county, neighbourhood, estate, address)
    since Phase 1 does not yet require exact geocoding for a match.
    """
    county = location.get("county")
    neighbourhood = location.get("neighbourhood")
    estate = location.get("estate")
    address = location.get("address")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM locations
            WHERE county = $1
              AND neighbourhood IS NOT DISTINCT FROM $2
              AND estate IS NOT DISTINCT FROM $3
              AND address IS NOT DISTINCT FROM $4
            LIMIT 1
            """,
            county, neighbourhood, estate, address,
        )
        if row:
            return row["id"]

        row = await conn.fetchrow(
            """
            INSERT INTO locations
                (latitude, longitude, address, county, subcounty, ward, neighbourhood, estate)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            location.get("latitude"),
            location.get("longitude"),
            address,
            county,
            location.get("subcounty"),
            location.get("ward"),
            neighbourhood,
            estate,
        )
        return row["id"]


async def get_location_by_id(location_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM locations WHERE id = $1", location_id)
        return dict(row) if row else None


# ===========================================================================
# ── PROPERTY QUERIES ─────────────────────────────────────────────────────────
# ===========================================================================

async def create_property(fields: Dict[str, Any]) -> int:
    """Insert a new `properties` row (the physical asset). Returns its id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO properties (
                property_type, title, description, property_category,
                address, location_id, size_sqft, bedrooms, bathrooms,
                floor, furnished, parking_spaces, photos, amenities
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING id
            """,
            fields["property_type"],
            fields["title"],
            fields.get("description"),
            fields.get("property_category", "residential"),
            fields.get("address"),
            fields["location_id"],
            fields.get("size_sqft"),
            fields["bedrooms"],
            fields["bathrooms"],
            fields.get("floor"),
            fields.get("furnished", False),
            fields.get("parking_spaces", 0),
            json.dumps(fields.get("photos", [])),
            json.dumps(fields.get("amenities", [])),
        )
        return row["id"]


async def get_property_by_id(property_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.*,
                   l.id AS loc_id, l.county AS loc_county, l.subcounty AS loc_subcounty,
                   l.ward AS loc_ward, l.neighbourhood AS loc_neighbourhood,
                   l.estate AS loc_estate, l.address AS loc_address,
                   l.latitude AS loc_latitude, l.longitude AS loc_longitude
            FROM properties p
            LEFT JOIN locations l ON l.id = p.location_id
            WHERE p.id = $1
            """,
            property_id,
        )
        return dict(row) if row else None


async def update_property(property_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Partially update a property row. Returns the updated row or {}."""
    allowed = {
        "title", "description", "address", "location_id", "size_sqft",
        "bedrooms", "bathrooms", "floor", "furnished", "parking_spaces",
        "photos", "amenities",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return {}

    for json_field in ("photos", "amenities"):
        if json_field in safe and isinstance(safe[json_field], (list, dict)):
            safe[json_field] = json.dumps(safe[json_field])

    set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(safe))
    values = list(safe.values())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE properties SET {set_clause} WHERE id = $1 RETURNING *",
            property_id, *values,
        )
        return dict(row) if row else {}


# ===========================================================================
# ── LISTING QUERIES (Phase 0/1 shape: listing references a property) ───────
# ===========================================================================
#
# Organic ordering is ALWAYS `created_at DESC` (or a future deterministic
# score in Phase 3) — never a payment-derived column. Sponsored placements
# live entirely in `sponsored_placements` and are surfaced separately by
# the caller, always labelled.
# ===========================================================================

_LISTING_SELECT = """
    SELECT
        l.id, l.property_id, l.host_id, l.listing_type, l.asking_price,
        l.service_charge, l.deposit, l.currency, l.status,
        l.available_from, l.last_confirmed_at, l.expires_at,
        l.created_at, l.updated_at,
        p.property_type, p.title, p.description, p.property_category,
        p.address AS property_address, p.location_id, p.size_sqft,
        p.bedrooms, p.bathrooms, p.floor, p.furnished, p.parking_spaces,
        p.photos, p.amenities,
        p.created_at AS property_created_at, p.updated_at AS property_updated_at,
        loc.id AS loc_id, loc.county AS loc_county, loc.subcounty AS loc_subcounty,
        loc.ward AS loc_ward, loc.neighbourhood AS loc_neighbourhood,
        loc.estate AS loc_estate, loc.address AS loc_address,
        loc.latitude AS loc_latitude, loc.longitude AS loc_longitude,
        u.full_name AS host_name, u.account_type AS host_account_type,
        u.verification_status AS host_verification_status,
        EXISTS (
            SELECT 1 FROM sponsored_placements sp
            WHERE sp.listing_id = l.id AND sp.is_active = TRUE
              AND (sp.end_date IS NULL OR sp.end_date > NOW())
        ) AS is_sponsored
    FROM listings l
    JOIN properties p ON p.id = l.property_id
    LEFT JOIN locations loc ON loc.id = p.location_id
    JOIN users u ON u.id = l.host_id
"""


async def create_listing_row(fields: Dict[str, Any]) -> int:
    """Insert a new `listings` row referencing an existing property."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO listings (
                property_id, host_id, listing_type, asking_price,
                service_charge, deposit, currency, status, available_from
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
            """,
            fields["property_id"],
            fields["host_id"],
            fields.get("listing_type", "rent"),
            fields["asking_price"],
            fields.get("service_charge", 0),
            fields.get("deposit", 0),
            fields.get("currency", "KES"),
            fields.get("status", "active"),
            fields.get("available_from"),
        )
        return row["id"]


async def get_listing_by_id(listing_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single listing joined with its property, location and host."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LISTING_SELECT + " WHERE l.id = $1", listing_id)
        return dict(row) if row else None


async def get_listings_paginated(
    county: Optional[str] = None,
    neighbourhood: Optional[str] = None,
    property_type: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    bedrooms: Optional[int] = None,
    bathrooms: Optional[int] = None,
    amenities: Optional[List[str]] = None,
    page: int = 1,
    page_size: int = 20,
    only_active: bool = True,
) -> tuple[List[Dict[str, Any]], int]:
    """
    Paginated, hard-filtered browse query. Ordered by created_at DESC only —
    no visibility/rank/payment column exists on listings by design.
    """
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if only_active:
        conditions.append("l.status = 'active'")

    if county:
        conditions.append(f"LOWER(loc.county) = LOWER(${idx})")
        params.append(county)
        idx += 1
    if neighbourhood:
        conditions.append(f"LOWER(loc.neighbourhood) = LOWER(${idx})")
        params.append(neighbourhood)
        idx += 1
    if property_type:
        conditions.append(f"p.property_type = ${idx}")
        params.append(property_type)
        idx += 1
    if min_price is not None:
        conditions.append(f"l.asking_price >= ${idx}")
        params.append(min_price)
        idx += 1
    if max_price is not None:
        conditions.append(f"l.asking_price <= ${idx}")
        params.append(max_price)
        idx += 1
    if bedrooms is not None:
        conditions.append(f"p.bedrooms >= ${idx}")
        params.append(bedrooms)
        idx += 1
    if bathrooms is not None:
        conditions.append(f"p.bathrooms >= ${idx}")
        params.append(bathrooms)
        idx += 1
    for amenity in (amenities or []):
        conditions.append(f"p.amenities @> ${idx}::jsonb")
        params.append(json.dumps([amenity]))
        idx += 1

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    pool = await get_pool()
    async with pool.acquire() as conn:
        count_sql = f"""
            SELECT COUNT(*) FROM listings l
            JOIN properties p ON p.id = l.property_id
            LEFT JOIN locations loc ON loc.id = p.location_id
            {where_clause}
        """
        total = await conn.fetchval(count_sql, *params)

        sql = (
            _LISTING_SELECT + where_clause +
            f" ORDER BY l.created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        )
        rows = await conn.fetch(sql, *params, page_size, offset)
        return [dict(r) for r in rows], int(total or 0)


async def get_listings_by_host(host_id: int) -> List[Dict[str, Any]]:
    """Return ALL listings (any status) belonging to a host, newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _LISTING_SELECT + " WHERE l.host_id = $1 ORDER BY l.created_at DESC",
            host_id,
        )
        results = [dict(r) for r in rows]
        # Compatibility keys for payments.py's legacy booster/package logic,
        # which predates the property/listing split. `package_type` no
        # longer exists on a listing (it isn't a payment attribute of the
        # advertisement) so it is always None here — see the note above
        # bump_visibility_rank(). Do not use these two keys for anything new.
        for r in results:
            r["is_available"] = r["status"] == "active"
            r["package_type"] = None
        return results


async def update_listing_row(listing_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Partially update listing-level fields (not property fields)."""
    allowed = {
        "listing_type", "asking_price", "service_charge", "deposit",
        "currency", "status", "available_from", "last_confirmed_at",
        "expires_at",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return {}

    set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(safe))
    values = list(safe.values())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE listings SET {set_clause} WHERE id = $1 RETURNING *",
            listing_id, *values,
        )
        return dict(row) if row else {}


async def delete_listing_row(listing_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM listings WHERE id = $1", listing_id)
        return result == "DELETE 1"


async def search_listings_for_ai(preferences: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Broad candidate fetch for the AI layer (chatbot.py / matcher.py).
    Hard-filtered, then ordered by created_at DESC only — matcher.py's
    GPT-4o ranking is scheduled for replacement by the Phase 3 deterministic
    scoring engine (see Backend/intelligence/scoring.py, not yet built);
    this query intentionally applies no payment-derived ordering already.
    """
    conditions = ["l.status = 'active'"]
    params: List[Any] = []
    idx = 1

    if preferences.get("county"):
        conditions.append(f"LOWER(loc.county) = LOWER(${idx})")
        params.append(preferences["county"])
        idx += 1
    if preferences.get("neighbourhood") or preferences.get("city"):
        conditions.append(f"LOWER(loc.neighbourhood) = LOWER(${idx})")
        params.append(preferences.get("neighbourhood") or preferences.get("city"))
        idx += 1
    if preferences.get("max_budget") is not None:
        conditions.append(f"l.asking_price <= ${idx}")
        params.append(preferences["max_budget"])
        idx += 1
    if preferences.get("bedrooms") is not None:
        conditions.append(f"p.bedrooms >= ${idx}")
        params.append(preferences["bedrooms"])
        idx += 1

    where_clause = " AND ".join(conditions)
    sql = _LISTING_SELECT + f" WHERE {where_clause} ORDER BY l.created_at DESC LIMIT 30"

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


# ===========================================================================
# ── AVAILABILITY CONFIRMATIONS ───────────────────────────────────────────────
# ===========================================================================
#
# Freshness is derived from an append-only log, never a single Boolean.
# Every confirmation also refreshes listings.last_confirmed_at so browse/
# search queries can sort/filter by freshness without joining the log.
# ===========================================================================

async def create_availability_confirmation(
    listing_id: int,
    confirmed_by: Optional[int],
    status_value: str,
    confirmation_method: str = "host_manual",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO availability_confirmations
                    (listing_id, confirmed_by, confirmation_method, status, notes)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING *
                """,
                listing_id, confirmed_by, confirmation_method, status_value, notes,
            )
            # Keep listings.last_confirmed_at in sync; flip status to
            # inactive/active only for the two unambiguous confirmation states.
            new_listing_status = None
            if status_value == "unavailable":
                new_listing_status = "inactive"
            elif status_value == "available":
                new_listing_status = "active"

            if new_listing_status:
                await conn.execute(
                    "UPDATE listings SET last_confirmed_at = $2, status = $3 WHERE id = $1",
                    listing_id, row["confirmed_at"], new_listing_status,
                )
            else:
                await conn.execute(
                    "UPDATE listings SET last_confirmed_at = $2 WHERE id = $1",
                    listing_id, row["confirmed_at"],
                )
        return dict(row)


async def get_latest_availability(listing_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM availability_confirmations
            WHERE listing_id = $1
            ORDER BY confirmed_at DESC
            LIMIT 1
            """,
            listing_id,
        )
        return dict(row) if row else None


# ===========================================================================
# ── USER PREFERENCES ─────────────────────────────────────────────────────────
# ===========================================================================

_PREF_COLUMNS = [
    "workplace_latitude", "workplace_longitude", "workplace_name",
    "income_range", "rent_budget", "total_housing_budget", "bedrooms",
    "preferred_property_type", "transport_mode", "commute_limit_minutes",
    "safety_importance", "water_importance", "internet_importance",
    "parking_required", "quietness_preference", "furnished_preference",
    "preferred_neighbourhoods", "household_size", "pets", "other_preferences",
]


async def upsert_user_preferences(user_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert or update the single preferences row for a renter.
    Called after every search / chat interaction, so this is a full upsert
    of the allowed columns rather than a sparse patch.
    """
    safe = {k: fields[k] for k in _PREF_COLUMNS if k in fields}
    if "preferred_neighbourhoods" in safe:
        safe["preferred_neighbourhoods"] = json.dumps(safe["preferred_neighbourhoods"])

    columns = list(safe.keys())
    values = list(safe.values())

    col_list = ", ".join(columns)
    placeholders = ", ".join(f"${i+2}" for i in range(len(columns)))
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO user_preferences (user_id, {col_list})
            VALUES ($1, {placeholders})
            ON CONFLICT (user_id) DO UPDATE SET {update_clause}, updated_at = NOW()
            RETURNING *
            """,
            user_id, *values,
        )
        return dict(row)


async def get_user_preferences(user_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_preferences WHERE user_id = $1", user_id
        )
        return dict(row) if row else None


# ===========================================================================
# ── SAVED SEARCHES (P1 stretch) ─────────────────────────────────────────────
# ===========================================================================

async def create_saved_search(
    user_id: int, search_criteria: Dict[str, Any], search_type: str = "residential"
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO saved_searches (user_id, search_criteria_json, search_type)
            VALUES ($1, $2, $3)
            RETURNING *
            """,
            user_id, json.dumps(search_criteria), search_type,
        )
        return dict(row)


async def list_saved_searches(user_id: int) -> List[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM saved_searches WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]


async def delete_saved_search(saved_search_id: int, user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM saved_searches WHERE id = $1 AND user_id = $2",
            saved_search_id, user_id,
        )
        return result == "DELETE 1"


# ===========================================================================
# ── FAVORITES (P1 stretch) — keyed on property_id ───────────────────────────
# ===========================================================================

async def create_favorite(user_id: int, property_id: int) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO favorites (user_id, property_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, property_id) DO UPDATE SET user_id = EXCLUDED.user_id
            RETURNING *
            """,
            user_id, property_id,
        )
        return dict(row)


async def list_favorites(user_id: int) -> List[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM favorites WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]


async def delete_favorite(user_id: int, property_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM favorites WHERE user_id = $1 AND property_id = $2",
            user_id, property_id,
        )
        return result == "DELETE 1"


# ===========================================================================
# ── SPONSORED PLACEMENTS (homepage / labelled search-top) ───────────────────
# ===========================================================================
#
# Deliberately isolated from organic browse/search. Anything returned here
# MUST be rendered with a visible "Sponsored" label by the frontend — see
# architectural invariant in sponsored_placements (migration 0002).
# ===========================================================================

async def get_featured_listings(limit: int = 6) -> List[Dict[str, Any]]:
    """
    Return currently active sponsored placements, newest-purchased first.
    This replaces the old is_featured/visibility_rank homepage query — the
    result must be labelled "Sponsored" by the caller/UI, never presented
    as an organic recommendation.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _LISTING_SELECT + """
            JOIN sponsored_placements sp ON sp.listing_id = l.id
            WHERE sp.is_active = TRUE
              AND (sp.end_date IS NULL OR sp.end_date > NOW())
              AND l.status = 'active'
            ORDER BY sp.start_date DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Compatibility shims for payments.py's legacy booster logic, which still
# assumes a pre-Phase0 flat listing shape (package_type / is_available /
# visibility_rank directly on `listings`). That shape no longer exists after
# the Phase 0 property/listing split. These shims prevent hard crashes while
# a proper Phase 2 monetization redesign (subscriptions + sponsored
# placements, see roadmap V1 §32) replaces the booster/package model
# entirely. They intentionally do NOT restore payment-influenced organic
# ranking.
# ---------------------------------------------------------------------------

async def bump_visibility_rank(listing_id: int, increment: int = 100) -> bool:
    """
    DEPRECATED no-op shim. There is no visibility_rank column on `listings`
    (organic ranking must never depend on payment). Kept only so
    payments.py's legacy booster code path does not raise AttributeError
    until it is redesigned around sponsored_placements in Phase 2.
    """
    logger.warning(
        "bump_visibility_rank() called for listing %s — this is a no-op "
        "compatibility shim; booster/monetization logic needs a Phase 2 "
        "redesign around sponsored_placements.",
        listing_id,
    )
    return False
# ===========================================================================
# ── PAYMENT QUERIES ─────────────────────────────────────────────────────────
# ===========================================================================

async def create_payment(
    host_id: int,
    payment_method: str,
    amount: float,
    package_type: str,
    transaction_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert a new payment record with status='pending'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO payments
                (host_id, payment_method, transaction_id, amount, currency, package_type)
            VALUES ($1, $2, $3, $4, 'KES', $5)
            RETURNING *
            """,
            host_id, payment_method, transaction_id, amount, package_type,
        )
        return dict(row)


async def update_payment_status(
    transaction_id: str,
    status: str,  # 'completed' | 'failed' | 'refunded'
) -> Optional[Dict[str, Any]]:
    """Update payment status, e.g. after M-Pesa callback or Stripe webhook."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE payments SET status = $2
            WHERE  transaction_id = $1
            RETURNING *
            """,
            transaction_id, status,
        )
        return dict(row) if row else None


async def get_payment_by_transaction(transaction_id: str) -> Optional[Dict[str, Any]]:
    """Look up a payment by M-Pesa CheckoutRequestID or Stripe PaymentIntent ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payments WHERE transaction_id = $1", transaction_id
        )
        return dict(row) if row else None


async def get_payments_by_host(host_id: int) -> List[Dict[str, Any]]:
    """Return all payment records for a host (billing history page)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM payments WHERE host_id = $1 ORDER BY created_at DESC",
            host_id,
        )
        return [dict(r) for r in rows]


# ===========================================================================
# ── BOOSTER QUERIES ─────────────────────────────────────────────────────────
# ===========================================================================

async def create_booster(
    listing_id: int,
    booster_type: str,
    payment_id: int,
    end_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Activate a booster after payment is confirmed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO boosters (listing_id, booster_type, payment_id, end_date)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            listing_id, booster_type, payment_id, end_date,
        )
        return dict(row)


async def deactivate_expired_boosters() -> int:
    """
    Set is_active = FALSE for all boosters whose end_date has passed.
    Intended to be called periodically (e.g. daily cron / startup).
    Returns the number of boosters deactivated.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE boosters
            SET    is_active = FALSE
            WHERE  is_active = TRUE
              AND  end_date IS NOT NULL
              AND  end_date < NOW()
            """
        )
        count = int(result.split()[-1])
        if count:
            logger.info(f"Deactivated {count} expired booster(s).")
        return count


async def get_active_boosters(listing_id: int) -> List[Dict[str, Any]]:
    """Return all currently active boosters for a listing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM boosters
            WHERE  listing_id = $1
              AND  is_active = TRUE
              AND  (end_date IS NULL OR end_date > NOW())
            """,
            listing_id,
        )
        return [dict(r) for r in rows]


# ===========================================================================
# ── CONVERSATION QUERIES ────────────────────────────────────────────────────
# ===========================================================================

async def save_message(session_id: str, role: str, message: str) -> Dict[str, Any]:
    """Persist a single chatbot message (user or assistant turn)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (session_id, role, message)
            VALUES ($1, $2, $3)
            RETURNING *
            """,
            session_id, role, message,
        )
        return dict(row)


async def get_conversation_history(
    session_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Return the last N messages for a chat session, oldest first.
    Used to rebuild context when sending to Groq.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, message, created_at
            FROM   conversations
            WHERE  session_id = $1
            ORDER  BY created_at DESC
            LIMIT  $2
            """,
            session_id, limit,
        )
        # Reverse so the list is chronological (oldest → newest)
        return [dict(r) for r in reversed(rows)]


async def clear_conversation(session_id: str) -> bool:
    """Delete all messages for a session (visitor starts fresh)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE session_id = $1", session_id
        )
        return int(result.split()[-1]) > 0


# ===========================================================================
# ── ANALYTICS QUERIES (Agency only) ─────────────────────────────────────────
# ===========================================================================

async def get_listing_stats(host_id: int) -> List[Dict[str, Any]]:
    """
    Return per-listing stats for the analytics dashboard.
    Currently tracks: listing count, active boosters, payment totals.
    (View/enquiry counters can be added later with a separate events table.)

    NOTE: this query still references l.city / l.county / l.is_available /
    l.is_featured / l.visibility_rank directly on `listings`, which predate
    the Phase 0 property/listing split and no longer exist as columns on
    `listings`. It will raise an UndefinedColumnError against the current
    schema. Flagged for a Phase 1B/dashboard fix — not modified here since
    it's outside today's scope (getting the app booting against a real DB).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                l.id,
                l.title,
                l.city,
                l.county,
                l.is_available,
                l.is_featured,
                l.visibility_rank,
                l.created_at,
                COUNT(b.id) FILTER (WHERE b.is_active = TRUE) AS active_boosters,
                COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'completed'), 0) AS total_spent
            FROM   listings l
            LEFT   JOIN boosters b ON b.listing_id = l.id
            LEFT   JOIN payments p ON p.host_id = l.host_id
            WHERE  l.host_id = $1
            GROUP  BY l.id
            ORDER  BY l.created_at DESC
            """,
            host_id,
        )
        return [dict(r) for r in rows]