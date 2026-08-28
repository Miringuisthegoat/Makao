"""phase1: availability confirmations, renter preferences, saved searches, favorites

Implements Backend Roadmap Phase 1 ("Availability & Preferences"):
  - availability_confirmations: append-only freshness log for a listing,
    replacing any notion of a single Boolean is_available flag as the
    source of truth. listings.last_confirmed_at is kept in sync as a
    denormalised "latest confirmation time" for fast sorting/filtering.
  - user_preferences: one structured preference row per renter, upserted
    after every search / chat interaction. Feeds the Phase 2 search
    service and Phase 3 scoring engine — never used for ranking directly
    by an LLM.
  - saved_searches (P1 stretch): a renter's reusable structured search.
  - favorites (P1 stretch): a renter's saved property. Keyed on
    property_id (not listing_id) per the property != listing invariant —
    a renter favorites the physical home, not a particular advertisement
    for it.

No existing tables are altered destructively. No data is dropped.

Revision ID: 0003_phase1_availability_preferences
Revises: 0002_phase0_foundations
Create Date: 2026-08-27
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_phase1_availability_preferences"
down_revision = "0002_phase0_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 0. users.host_type: relax so non-host accounts (renters) can exist.
    #    Phase 0 added the generalised `account_type` column as the
    #    canonical field; `host_type` is kept only for landlord/agency
    #    accounts created before this migration and is no longer required
    #    on new rows. Renter signups leave it NULL.
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE users ALTER COLUMN host_type DROP NOT NULL;
        """
    )

    # ------------------------------------------------------------------
    # 1. availability_confirmations
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS availability_confirmations (
            id                   SERIAL PRIMARY KEY,
            listing_id           INTEGER      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            confirmed_by         INTEGER      REFERENCES users(id) ON DELETE SET NULL,
            confirmation_method  VARCHAR(30)  NOT NULL DEFAULT 'host_manual'
                                    CHECK (confirmation_method IN
                                        ('host_manual', 'admin_override', 'system_expiry',
                                         'renter_report')),
            status               VARCHAR(20)  NOT NULL
                                    CHECK (status IN ('available', 'unavailable', 'uncertain')),
            confirmed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            notes                VARCHAR(500)
        );

        CREATE INDEX IF NOT EXISTS idx_availability_listing
            ON availability_confirmations(listing_id);
        CREATE INDEX IF NOT EXISTS idx_availability_listing_confirmed_at
            ON availability_confirmations(listing_id, confirmed_at DESC);

        -- Backfill: every existing active listing gets one initial
        -- confirmation so freshness has a starting point instead of NULL.
        INSERT INTO availability_confirmations
            (listing_id, confirmed_by, confirmation_method, status, confirmed_at)
        SELECT l.id, l.host_id, 'system_expiry',
               CASE WHEN l.status = 'active' THEN 'available' ELSE 'unavailable' END,
               COALESCE(l.last_confirmed_at, l.updated_at, l.created_at)
        FROM listings l
        WHERE NOT EXISTS (
            SELECT 1 FROM availability_confirmations ac WHERE ac.listing_id = l.id
        );

        UPDATE listings l
        SET last_confirmed_at = ac.confirmed_at
        FROM (
            SELECT DISTINCT ON (listing_id) listing_id, confirmed_at
            FROM availability_confirmations
            ORDER BY listing_id, confirmed_at DESC
        ) ac
        WHERE ac.listing_id = l.id AND l.last_confirmed_at IS NULL;
        """
    )

    # ------------------------------------------------------------------
    # 2. user_preferences — one row per renter, upserted over time
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            id                        SERIAL PRIMARY KEY,
            user_id                   INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            workplace_latitude        DOUBLE PRECISION,
            workplace_longitude       DOUBLE PRECISION,
            workplace_name            VARCHAR(150),
            income_range              VARCHAR(30),
            rent_budget               NUMERIC(12,2),
            total_housing_budget      NUMERIC(12,2),
            bedrooms                  SMALLINT,
            preferred_property_type   VARCHAR(30),
            transport_mode            VARCHAR(20)
                CHECK (transport_mode IS NULL OR transport_mode IN
                    ('walking', 'matatu', 'bus', 'boda', 'car', 'mixed')),
            commute_limit_minutes     SMALLINT,
            safety_importance         SMALLINT CHECK (safety_importance IS NULL OR safety_importance BETWEEN 1 AND 5),
            water_importance          SMALLINT CHECK (water_importance IS NULL OR water_importance BETWEEN 1 AND 5),
            internet_importance       SMALLINT CHECK (internet_importance IS NULL OR internet_importance BETWEEN 1 AND 5),
            parking_required          BOOLEAN NOT NULL DEFAULT FALSE,
            quietness_preference      SMALLINT CHECK (quietness_preference IS NULL OR quietness_preference BETWEEN 1 AND 5),
            furnished_preference      VARCHAR(20)
                CHECK (furnished_preference IS NULL OR furnished_preference IN
                    ('furnished', 'unfurnished', 'no_preference')),
            preferred_neighbourhoods  JSONB NOT NULL DEFAULT '[]',
            household_size            SMALLINT,
            pets                      BOOLEAN NOT NULL DEFAULT FALSE,
            other_preferences         VARCHAR(1000),
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_id);

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_user_preferences_updated_at'
            ) THEN
                CREATE TRIGGER trg_user_preferences_updated_at
                BEFORE UPDATE ON user_preferences
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$;
        """
    )

    # ------------------------------------------------------------------
    # 3. saved_searches (P1 stretch)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_searches (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            search_criteria_json JSONB    NOT NULL DEFAULT '{}',
            search_type      VARCHAR(20)  NOT NULL DEFAULT 'residential'
                                CHECK (search_type IN ('residential', 'commercial')),
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            last_run_at      TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches(user_id);
        """
    )

    # ------------------------------------------------------------------
    # 4. favorites (P1 stretch) — keyed on property_id, not listing_id
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            property_id  INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, property_id)
        );

        CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
        CREATE INDEX IF NOT EXISTS idx_favorites_property ON favorites(property_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS favorites;
        DROP TABLE IF EXISTS saved_searches;
        DROP TABLE IF EXISTS user_preferences;
        DROP TABLE IF EXISTS availability_confirmations;

        -- Restoring NOT NULL is only safe if no renter rows (NULL host_type)
        -- were created while this migration was applied; guard accordingly.
        UPDATE users SET host_type = 'landlord' WHERE host_type IS NULL;
        ALTER TABLE users ALTER COLUMN host_type SET NOT NULL;
        """
    )
