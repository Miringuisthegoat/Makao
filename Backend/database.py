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
import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

import asyncpg
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
# Table initialisation — runs once on startup
# ---------------------------------------------------------------------------
CREATE_TABLES_SQL = """
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
    """Create all tables if they do not already exist. Call on app startup."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
    logger.info("Database tables initialised.")


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
# ── LISTING QUERIES ─────────────────────────────────────────────────────────
# ===========================================================================

async def create_listing(data: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a new listing (status inactive until payment confirmed)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO listings (
                host_id, title, description, location, city, county,
                price_per_month, bedrooms, bathrooms, size_sqft,
                amenities, photos, package_type, is_available
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
            )
            RETURNING *
            """,
            data["host_id"],
            data["title"],
            data.get("description", ""),
            data.get("location", ""),
            data.get("city", ""),
            data.get("county", ""),
            data.get("price_per_month", 0),
            data.get("bedrooms", 1),
            data.get("bathrooms", 1),
            data.get("size_sqft"),
            json.dumps(data.get("amenities", [])),
            json.dumps(data.get("photos", [])),
            data.get("package_type", "landlord"),
            False,  # inactive until payment confirmed
        )
        return dict(row)


async def activate_listing(listing_id: int) -> bool:
    """Mark a listing as available after payment is confirmed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE listings SET is_available = TRUE WHERE id = $1", listing_id
        )
        return result == "UPDATE 1"


async def get_listing_by_id(listing_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single listing with host details joined."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.*,
                   u.full_name  AS host_name,
                   u.host_type  AS host_account_type,
                   u.profile_photo AS host_photo
            FROM   listings l
            JOIN   users u ON u.id = l.host_id
            WHERE  l.id = $1
            """,
            listing_id,
        )
        return dict(row) if row else None


async def get_listings(
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Fetch available listings with optional filters.
    Filters keys: county, city, min_price, max_price, bedrooms, amenities (list).
    Results ordered by: is_featured DESC, visibility_rank DESC, created_at DESC.
    """
    filters = filters or {}
    conditions = ["l.is_available = TRUE"]
    params: List[Any] = []
    idx = 1  # $1, $2, … counter

    if filters.get("county"):
        conditions.append(f"LOWER(l.county) = LOWER(${idx})")
        params.append(filters["county"])
        idx += 1

    if filters.get("city"):
        conditions.append(f"LOWER(l.city) = LOWER(${idx})")
        params.append(filters["city"])
        idx += 1

    if filters.get("min_price") is not None:
        conditions.append(f"l.price_per_month >= ${idx}")
        params.append(filters["min_price"])
        idx += 1

    if filters.get("max_price") is not None:
        conditions.append(f"l.price_per_month <= ${idx}")
        params.append(filters["max_price"])
        idx += 1

    if filters.get("bedrooms") is not None:
        conditions.append(f"l.bedrooms >= ${idx}")
        params.append(filters["bedrooms"])
        idx += 1

    if filters.get("bathrooms") is not None:
        conditions.append(f"l.bathrooms >= ${idx}")
        params.append(filters["bathrooms"])
        idx += 1

    # Amenity filter — listing must contain ALL requested amenities
    for amenity in filters.get("amenities", []):
        conditions.append(f"l.amenities @> ${idx}::jsonb")
        params.append(json.dumps([amenity]))
        idx += 1

    where_clause = " AND ".join(conditions)
    params.extend([limit, offset])

    sql = f"""
        SELECT l.*,
               u.full_name       AS host_name,
               u.host_type       AS host_account_type
        FROM   listings l
        JOIN   users u ON u.id = l.host_id
        WHERE  {where_clause}
        ORDER  BY l.is_featured DESC, l.visibility_rank DESC, l.created_at DESC
        LIMIT  ${idx} OFFSET ${idx+1}
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def count_listings(filters: Optional[Dict[str, Any]] = None) -> int:
    """Return total count of available listings matching filters (for pagination)."""
    filters = filters or {}
    conditions = ["is_available = TRUE"]
    params: List[Any] = []
    idx = 1

    if filters.get("county"):
        conditions.append(f"LOWER(county) = LOWER(${idx})")
        params.append(filters["county"])
        idx += 1
    if filters.get("city"):
        conditions.append(f"LOWER(city) = LOWER(${idx})")
        params.append(filters["city"])
        idx += 1
    if filters.get("min_price") is not None:
        conditions.append(f"price_per_month >= ${idx}")
        params.append(filters["min_price"])
        idx += 1
    if filters.get("max_price") is not None:
        conditions.append(f"price_per_month <= ${idx}")
        params.append(filters["max_price"])
        idx += 1
    if filters.get("bedrooms") is not None:
        conditions.append(f"bedrooms >= ${idx}")
        params.append(filters["bedrooms"])
        idx += 1

    where_clause = " AND ".join(conditions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM listings WHERE {where_clause}", *params
        )


async def get_featured_listings(limit: int = 6) -> List[Dict[str, Any]]:
    """Fetch homepage-featured listings (is_featured = TRUE), top-ranked first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.*,
                   u.full_name  AS host_name,
                   u.host_type  AS host_account_type
            FROM   listings l
            JOIN   users u ON u.id = l.host_id
            WHERE  l.is_available = TRUE AND l.is_featured = TRUE
            ORDER  BY l.visibility_rank DESC, l.created_at DESC
            LIMIT  $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def get_listings_by_host(host_id: int) -> List[Dict[str, Any]]:
    """Return all listings belonging to a specific host (dashboard view)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM listings WHERE host_id = $1 ORDER BY created_at DESC",
            host_id,
        )
        return [dict(r) for r in rows]


async def update_listing(listing_id: int, host_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update listing fields. host_id enforces ownership — a host can only
    edit their own listings. Returns updated row or empty dict if not found.
    """
    allowed = {
        "title", "description", "location", "city", "county",
        "price_per_month", "bedrooms", "bathrooms", "size_sqft",
        "amenities", "photos", "is_available",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        raise ValueError("No valid listing fields to update.")

    # Serialize JSON fields
    for json_field in ("amenities", "photos"):
        if json_field in safe and isinstance(safe[json_field], (list, dict)):
            safe[json_field] = json.dumps(safe[json_field])

    set_clause = ", ".join(f"{col} = ${i+3}" for i, col in enumerate(safe))
    values = list(safe.values())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE listings SET {set_clause} "
            f"WHERE id = $1 AND host_id = $2 RETURNING *",
            listing_id, host_id, *values,
        )
        return dict(row) if row else {}


async def delete_listing(listing_id: int, host_id: int) -> bool:
    """Delete a listing. Ownership enforced by host_id check."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM listings WHERE id = $1 AND host_id = $2",
            listing_id, host_id,
        )
        return result == "DELETE 1"


async def set_listing_featured(listing_id: int, is_featured: bool) -> bool:
    """Toggle the featured flag on a listing (called by booster activation)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE listings SET is_featured = $2 WHERE id = $1",
            listing_id, is_featured,
        )
        return result == "UPDATE 1"


async def bump_visibility_rank(listing_id: int, increment: int = 100) -> bool:
    """
    Increase visibility_rank so a listing appears higher in search.
    Called when a 'refresh_listing' booster is activated.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE listings SET visibility_rank = visibility_rank + $2 WHERE id = $1",
            listing_id, increment,
        )
        return result == "UPDATE 1"


async def search_listings_for_ai(preferences: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Broad listing fetch for the AI matcher (OpenAI GPT-4o).
    Returns up to 30 candidate listings based on loose preference matching.
    Chatbot-priority and agency listings come first.
    """
    conditions = ["l.is_available = TRUE"]
    params: List[Any] = []
    idx = 1

    if preferences.get("county"):
        conditions.append(f"LOWER(l.county) = LOWER(${idx})")
        params.append(preferences["county"])
        idx += 1

    if preferences.get("city"):
        conditions.append(f"LOWER(l.city) = LOWER(${idx})")
        params.append(preferences["city"])
        idx += 1

    if preferences.get("max_budget") is not None:
        conditions.append(f"l.price_per_month <= ${idx}")
        params.append(preferences["max_budget"])
        idx += 1

    if preferences.get("bedrooms") is not None:
        conditions.append(f"l.bedrooms >= ${idx}")
        params.append(preferences["bedrooms"])
        idx += 1

    where_clause = " AND ".join(conditions)

    # Prioritise: chatbot_priority boosters active → agency → featured → rank
    sql = f"""
        SELECT l.*,
               u.full_name  AS host_name,
               u.host_type  AS host_account_type,
               EXISTS (
                   SELECT 1 FROM boosters b
                   WHERE  b.listing_id = l.id
                     AND  b.booster_type = 'chatbot_priority'
                     AND  b.is_active = TRUE
                     AND  (b.end_date IS NULL OR b.end_date > NOW())
               ) AS has_chatbot_priority
        FROM   listings l
        JOIN   users u ON u.id = l.host_id
        WHERE  {where_clause}
        ORDER  BY has_chatbot_priority DESC,
                  (l.package_type = 'agency') DESC,
                  l.is_featured DESC,
                  l.visibility_rank DESC
        LIMIT  30
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


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