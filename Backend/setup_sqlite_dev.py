"""
Local dev database setup using SQLite — no Postgres install required.

This creates the Phase 0 schema (locations / properties / listings split,
sponsored_placements, widened users.account_type) directly, since SQLite
doesn't support the Postgres-specific migration syntax used in
Backend/migrations/. This is a DEV-ONLY convenience script.

IMPORTANT: Production still targets PostgreSQL via Alembic
(Backend/migrations/). This script exists only so you can build/run/test
the API locally without installing Postgres yet. When you're ready to move
to Postgres, use `alembic upgrade head` instead (see migrations/PHASE0_NOTES.md).

Usage:
    python setup_sqlite_dev.py
Creates Backend/makao_dev.db
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "makao_dev.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name            TEXT NOT NULL,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    phone                TEXT,
    profile_photo        TEXT,
    host_type            TEXT NOT NULL CHECK (host_type IN ('landlord', 'agency')),
    account_type         TEXT NOT NULL CHECK (account_type IN
                            ('renter','landlord','agent','agency','property_manager','developer')),
    company_name         TEXT,
    verification_status  TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN
                            ('unverified','partially_verified','verified')),
    stripe_customer_id   TEXT,
    plan_status          TEXT NOT NULL DEFAULT 'inactive' CHECK (plan_status IN
                            ('active','inactive','suspended')),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    latitude      REAL,
    longitude     REAL,
    address       TEXT,
    county        TEXT NOT NULL,
    subcounty     TEXT,
    ward          TEXT,
    neighbourhood TEXT,
    estate        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS properties (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    property_type     TEXT NOT NULL DEFAULT 'apartment',
    title             TEXT NOT NULL,
    description       TEXT,
    property_category TEXT NOT NULL DEFAULT 'residential' CHECK (property_category IN
                            ('residential','commercial')),
    address           TEXT,
    location_id       INTEGER REFERENCES locations(id),
    size_sqft         INTEGER,
    bedrooms          INTEGER NOT NULL DEFAULT 1,
    bathrooms         INTEGER NOT NULL DEFAULT 1,
    floor             TEXT,
    furnished         INTEGER NOT NULL DEFAULT 0,
    parking_spaces    INTEGER NOT NULL DEFAULT 0,
    photos            TEXT NOT NULL DEFAULT '[]',
    amenities         TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS listings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id       INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    host_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    listing_type      TEXT NOT NULL DEFAULT 'rent' CHECK (listing_type IN ('rent','sale')),
    asking_price      NUMERIC NOT NULL DEFAULT 0,
    service_charge    NUMERIC NOT NULL DEFAULT 0,
    deposit           NUMERIC NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL DEFAULT 'KES',
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                            ('active','inactive','rented','removed')),
    available_from    TEXT,
    last_confirmed_at TEXT,
    expires_at        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_method TEXT NOT NULL CHECK (payment_method IN ('mpesa','stripe_card','stripe_paypal')),
    transaction_id TEXT UNIQUE,
    amount         NUMERIC NOT NULL DEFAULT 0,
    currency       TEXT NOT NULL DEFAULT 'KES',
    package_type   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending','completed','failed','refunded')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sponsored_placements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    payment_id  INTEGER REFERENCES payments(id),
    placement   TEXT NOT NULL DEFAULT 'homepage' CHECK (placement IN
                    ('homepage','search_top_labelled')),
    start_date  TEXT NOT NULL DEFAULT (datetime('now')),
    end_date    TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_properties_location ON properties(location_id);
CREATE INDEX IF NOT EXISTS idx_listings_property ON listings(property_id);
CREATE INDEX IF NOT EXISTS idx_listings_host ON listings(host_id);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_sponsored_listing ON sponsored_placements(listing_id);
"""

def main():
    if os.path.exists(DB_PATH):
        print(f"Database already exists at {DB_PATH} — leaving it as is.")
        print("Delete the file first if you want a clean rebuild.")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    print(f"Created dev SQLite database at {DB_PATH}")

if __name__ == "__main__":
    main()