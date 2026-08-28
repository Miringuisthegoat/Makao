"""phase0: property/listing split, structured location, sponsored_placements

Implements Backend Roadmap Phase 0 ("Foundations"):
  - locations table (structured geo instead of free-text city/county)
  - properties table (the physical asset — split out of listings)
  - listings redefined (an advertisement FOR a property; FK property_id;
    drops visibility_rank / is_featured so organic ranking is never
    payment-influenced)
  - sponsored_placements (payment-driven placement, fully isolated from
    organic listings/search and must be labelled "Sponsored" in the UI)
  - users.account_type generalised beyond landlord/agency (adds 'renter',
    'property_manager', 'developer') and account_type/verification_status
    columns added without breaking existing host_type usage

Data migration: every existing `listings` row is split into one `properties`
row + one new-shape `listings` row referencing it, and every existing
`county`/`city` pair is upserted into `locations`. No data is dropped.

Revision ID: 0002_phase0_foundations
Revises: 0001_baseline
Create Date: 2026-08-23
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_phase0_foundations"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. users: generalise account_type, add verification_status
    #    (kept alongside legacy host_type during transition; host_type
    #    stays populated so existing auth.py code keeps working until
    #    Phase 1 cuts over.)
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS account_type VARCHAR(30),
            ADD COLUMN IF NOT EXISTS company_name VARCHAR(200),
            ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20)
                NOT NULL DEFAULT 'unverified',
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        UPDATE users SET account_type = host_type WHERE account_type IS NULL;

        ALTER TABLE users
            ALTER COLUMN account_type SET NOT NULL;

        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_account_type_check;
        ALTER TABLE users
            ADD CONSTRAINT users_account_type_check
            CHECK (account_type IN
                ('renter', 'landlord', 'agent', 'agency',
                 'property_manager', 'developer'));

        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_verification_status_check;
        ALTER TABLE users
            ADD CONSTRAINT users_verification_status_check
            CHECK (verification_status IN
                ('unverified', 'partially_verified', 'verified'));
        """
    )

    # ------------------------------------------------------------------
    # 2. locations — structured geography
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS locations (
            id            SERIAL PRIMARY KEY,
            latitude      DOUBLE PRECISION,
            longitude     DOUBLE PRECISION,
            address       VARCHAR(255),
            county        VARCHAR(100) NOT NULL,
            subcounty     VARCHAR(100),
            ward          VARCHAR(100),
            neighbourhood VARCHAR(120),
            estate        VARCHAR(120),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_locations_county ON locations(county);
        CREATE INDEX IF NOT EXISTS idx_locations_neighbourhood ON locations(neighbourhood);
        CREATE INDEX IF NOT EXISTS idx_locations_lat_lng ON locations(latitude, longitude);
        """
    )

    # Backfill one location row per distinct (county, city) pair seen in
    # legacy listings, using `city` as the neighbourhood/estate proxy for
    # now (Phase 1 lets hosts pick a real structured location).
    op.execute(
        """
        INSERT INTO locations (county, neighbourhood, address, created_at)
        SELECT DISTINCT
            COALESCE(NULLIF(TRIM(county), ''), 'Nairobi'),
            NULLIF(TRIM(city), ''),
            NULLIF(TRIM(location), ''),
            NOW()
        FROM listings
        WHERE NOT EXISTS (
            SELECT 1 FROM locations l
            WHERE l.county = COALESCE(NULLIF(TRIM(listings.county), ''), 'Nairobi')
              AND l.neighbourhood IS NOT DISTINCT FROM NULLIF(TRIM(listings.city), '')
        );
        """
    )

    # ------------------------------------------------------------------
    # 3. properties — the physical asset, split out of listings
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS properties (
            id               SERIAL PRIMARY KEY,
            property_type    VARCHAR(30)   NOT NULL DEFAULT 'apartment',
            title            VARCHAR(200)  NOT NULL,
            description      TEXT,
            property_category VARCHAR(30)  NOT NULL DEFAULT 'residential'
                                CHECK (property_category IN ('residential', 'commercial')),
            address          VARCHAR(255),
            location_id      INTEGER       REFERENCES locations(id),
            size_sqft        INTEGER,
            bedrooms         SMALLINT      NOT NULL DEFAULT 1,
            bathrooms        SMALLINT      NOT NULL DEFAULT 1,
            floor            VARCHAR(20),
            furnished        BOOLEAN       NOT NULL DEFAULT FALSE,
            parking_spaces   SMALLINT      NOT NULL DEFAULT 0,
            photos           JSONB         NOT NULL DEFAULT '[]',
            amenities        JSONB         NOT NULL DEFAULT '[]',
            created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_properties_location ON properties(location_id);
        CREATE INDEX IF NOT EXISTS idx_properties_type ON properties(property_type);
        CREATE INDEX IF NOT EXISTS idx_properties_bedrooms ON properties(bedrooms);

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_properties_updated_at'
            ) THEN
                CREATE TRIGGER trg_properties_updated_at
                BEFORE UPDATE ON properties
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$;
        """
    )

    # ------------------------------------------------------------------
    # 4. Rename legacy listings -> listings_legacy, then rebuild listings
    #    in the new shape and backfill both tables from the legacy data.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE listings RENAME TO listings_legacy;")

    op.execute(
        """
        CREATE TABLE listings (
            id               SERIAL PRIMARY KEY,
            property_id      INTEGER       NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
            host_id          INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            listing_type     VARCHAR(20)   NOT NULL DEFAULT 'rent'
                                CHECK (listing_type IN ('rent', 'sale')),
            asking_price     NUMERIC(12,2) NOT NULL DEFAULT 0,
            service_charge   NUMERIC(12,2) NOT NULL DEFAULT 0,
            deposit          NUMERIC(12,2) NOT NULL DEFAULT 0,
            currency         VARCHAR(5)    NOT NULL DEFAULT 'KES',
            status           VARCHAR(20)   NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'inactive', 'rented', 'removed')),
            available_from   DATE,
            last_confirmed_at TIMESTAMPTZ,
            expires_at       TIMESTAMPTZ,
            created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_listings_property ON listings(property_id);
        CREATE INDEX IF NOT EXISTS idx_listings_host      ON listings(host_id);
        CREATE INDEX IF NOT EXISTS idx_listings_status    ON listings(status);
        CREATE INDEX IF NOT EXISTS idx_listings_price     ON listings(asking_price);

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_listings_updated_at_v2'
            ) THEN
                CREATE TRIGGER trg_listings_updated_at_v2
                BEFORE UPDATE ON listings
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$;
        """
    )

    # One property per legacy listing (1:1 for MVP; later listings can
    # share a property once building/dedup logic exists in V1).
    op.execute(
        """
        INSERT INTO properties
            (id, property_type, title, description, property_category,
             address, location_id, size_sqft, bedrooms, bathrooms,
             furnished, photos, amenities, created_at, updated_at)
        SELECT
            ll.id,
            CASE WHEN ll.bedrooms = 0 THEN 'bedsitter' ELSE 'apartment' END,
            ll.title,
            ll.description,
            'residential',
            ll.location,
            (SELECT l.id FROM locations l
                WHERE l.county = COALESCE(NULLIF(TRIM(ll.county), ''), 'Nairobi')
                  AND l.neighbourhood IS NOT DISTINCT FROM NULLIF(TRIM(ll.city), '')
                LIMIT 1),
            ll.size_sqft,
            ll.bedrooms,
            ll.bathrooms,
            COALESCE(ll.amenities ? 'furnished', FALSE),
            ll.photos,
            ll.amenities,
            ll.created_at,
            ll.updated_at
        FROM listings_legacy ll;

        -- keep the properties id sequence consistent with the explicit ids inserted above
        SELECT setval(pg_get_serial_sequence('properties', 'id'),
                      COALESCE((SELECT MAX(id) FROM properties), 1));

        INSERT INTO listings
            (id, property_id, host_id, listing_type, asking_price, status,
             available_from, last_confirmed_at, created_at, updated_at)
        SELECT
            ll.id,
            ll.id,               -- 1:1 property<->legacy listing id
            ll.host_id,
            'rent',
            ll.price_per_month,
            CASE WHEN ll.is_available THEN 'active' ELSE 'inactive' END,
            NULL,
            ll.updated_at,
            ll.created_at,
            ll.updated_at
        FROM listings_legacy ll;

        SELECT setval(pg_get_serial_sequence('listings', 'id'),
                      COALESCE((SELECT MAX(id) FROM listings), 1));
        """
    )

    # ------------------------------------------------------------------
    # 5. sponsored_placements — payment-driven, isolated from organic search
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sponsored_placements (
            id           SERIAL PRIMARY KEY,
            listing_id   INTEGER      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            payment_id   INTEGER      REFERENCES payments(id),
            placement    VARCHAR(30)  NOT NULL DEFAULT 'homepage'
                            CHECK (placement IN ('homepage', 'search_top_labelled')),
            start_date   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            end_date     TIMESTAMPTZ,
            is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_sponsored_listing ON sponsored_placements(listing_id);

        -- Migrate legacy featured/boosted listings into sponsored_placements
        -- so no paying host silently loses what they paid for, but their
        -- placement is now clearly separated from organic ranking.
        INSERT INTO sponsored_placements (listing_id, placement, is_active, created_at)
        SELECT ll.id, 'homepage', TRUE, NOW()
        FROM listings_legacy ll
        WHERE ll.is_featured = TRUE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS sponsored_placements;
        DROP TABLE IF EXISTS listings;
        ALTER TABLE listings_legacy RENAME TO listings;
        DROP TABLE IF EXISTS properties;
        DROP TABLE IF EXISTS locations;

        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_verification_status_check;
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_account_type_check;
        ALTER TABLE users
            DROP COLUMN IF EXISTS account_type,
            DROP COLUMN IF EXISTS company_name,
            DROP COLUMN IF EXISTS verification_status,
            DROP COLUMN IF EXISTS updated_at;
        """
    )
