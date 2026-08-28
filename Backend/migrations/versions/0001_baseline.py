"""baseline: capture existing pre-roadmap schema

This migration does NOT create tables from scratch on a fresh dev DB in a
destructive way — it mirrors database.py's CREATE_TABLES_SQL so that:
  - a fresh dev/test DB can be built entirely through Alembic, and
  - an existing production DB can be `alembic stamp 0001_baseline` without
    re-running DDL, since the tables already exist there.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-23
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id                SERIAL PRIMARY KEY,
            full_name         VARCHAR(120)  NOT NULL,
            email             VARCHAR(255)  NOT NULL UNIQUE,
            password_hash     TEXT          NOT NULL,
            phone             VARCHAR(20),
            profile_photo     TEXT,
            host_type         VARCHAR(20)   NOT NULL
                                CHECK (host_type IN ('landlord', 'agency')),
            stripe_customer_id VARCHAR(100),
            plan_status       VARCHAR(20)   NOT NULL DEFAULT 'inactive'
                                CHECK (plan_status IN ('active', 'inactive', 'suspended')),
            created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS listings (
            id               SERIAL PRIMARY KEY,
            host_id          INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title            VARCHAR(200)  NOT NULL,
            description      TEXT,
            location         VARCHAR(255),
            city             VARCHAR(100),
            county           VARCHAR(100),
            price_per_month  NUMERIC(12,2) NOT NULL DEFAULT 0,
            bedrooms         SMALLINT      NOT NULL DEFAULT 1,
            bathrooms        SMALLINT      NOT NULL DEFAULT 1,
            size_sqft        INTEGER,
            amenities        JSONB         NOT NULL DEFAULT '[]',
            photos           JSONB         NOT NULL DEFAULT '[]',
            is_available     BOOLEAN       NOT NULL DEFAULT TRUE,
            is_featured      BOOLEAN       NOT NULL DEFAULT FALSE,
            package_type     VARCHAR(20)   NOT NULL DEFAULT 'landlord'
                                CHECK (package_type IN ('landlord', 'agency')),
            visibility_rank  INTEGER       NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

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

        CREATE TABLE IF NOT EXISTS payments (
            id               SERIAL PRIMARY KEY,
            host_id          INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            payment_method   VARCHAR(30)   NOT NULL
                                CHECK (payment_method IN ('mpesa', 'stripe_card', 'stripe_paypal')),
            transaction_id   VARCHAR(200)  UNIQUE,
            amount           NUMERIC(12,2) NOT NULL DEFAULT 0,
            currency         VARCHAR(5)    NOT NULL DEFAULT 'KES',
            package_type     VARCHAR(30)   NOT NULL,
            status           VARCHAR(20)   NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'completed', 'failed', 'refunded')),
            created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS boosters (
            id               SERIAL PRIMARY KEY,
            listing_id       INTEGER       NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            booster_type     VARCHAR(50)   NOT NULL,
            payment_id       INTEGER       REFERENCES payments(id),
            start_date       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            end_date         TIMESTAMPTZ,
            is_active        BOOLEAN       NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id               SERIAL PRIMARY KEY,
            session_id       VARCHAR(100)  NOT NULL,
            role             VARCHAR(10)   NOT NULL
                                CHECK (role IN ('user', 'assistant', 'system')),
            message          TEXT          NOT NULL,
            created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

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
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS boosters;
        DROP TABLE IF EXISTS payments;
        DROP TABLE IF EXISTS listings;
        DROP TABLE IF EXISTS users;
        DROP FUNCTION IF EXISTS set_updated_at();
        """
    )
