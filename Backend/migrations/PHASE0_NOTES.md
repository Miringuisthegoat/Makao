# Phase 0 — Foundations (complete)

## What changed
- Added Alembic to `Backend/` (`alembic.ini`, `migrations/env.py`), reading
  `DATABASE_URL` or `DB_*` env vars — no hardcoded credentials.
- `0001_baseline`: captures the exact schema `database.py` used to create
  inline (`users`, `listings` [old shape], `payments`, `boosters`,
  `conversations`). Run `alembic stamp 0001_baseline` on an existing
  production DB that already has these tables (do NOT run `upgrade` there,
  it would just no-op harmlessly due to `IF NOT EXISTS`, but `stamp` is the
  correct way to adopt Alembic on a live DB).
- `0002_phase0_foundations`:
  - `users`: adds `account_type` (renter/landlord/agent/agency/
    property_manager/developer), `company_name`, `verification_status`,
    `updated_at`. `host_type` is left in place untouched for backwards
    compatibility with `auth.py` until Phase 1 cuts auth over.
  - New `locations` table (structured lat/lng/county/subcounty/ward/
    neighbourhood/estate), backfilled from every distinct legacy
    `(county, city)` pair.
  - New `properties` table (the physical asset), backfilled 1:1 from
    legacy listings.
  - `listings` renamed to `listings_legacy` (kept, untouched, for audit/
    rollback) and rebuilt in the new shape: `property_id` FK, no
    `visibility_rank` / `is_featured` columns — organic ranking can no
    longer be influenced by payment at the schema level.
  - New `sponsored_placements` table, isolated from `listings`/`properties`
    scoring; legacy `is_featured = TRUE` rows were migrated here so no
    paying host silently lost what they paid for.
- `database.py`: `init_db()` no longer runs DDL. It now just checks
  `alembic_version` exists and logs the current migration version. The old
  inline SQL is kept as `_LEGACY_CREATE_TABLES_SQL` for reference only.
- Added `Backend/requirements.txt` (was previously missing from the repo).

## Verified locally
Ran against a real local Postgres 16 instance:
- `alembic upgrade 0001_baseline` → `alembic upgrade head` on seeded legacy
  data (1 host, 2 listings, one `is_featured=TRUE`) confirmed:
  - 2 `properties` rows created, correctly linked to 2 backfilled `locations`.
  - 2 new-shape `listings` rows, each pointing at its `property_id`.
  - The featured listing produced exactly 1 `sponsored_placements` row.
  - `listings_legacy` retained both original rows unmodified.
- `alembic downgrade 0001_baseline` cleanly restored the original schema
  and data (0 rows lost).

## Exit test (per phase plan): PASSED
"Existing listing CRUD works on new schema, no data loss" — confirmed via
the migration dry run above. `listings.py` / `database.py` query functions
still need to be repointed to the new `properties`+`listings` shape, which
is scoped into Phase 1 ("listings.py CRUD writes property+listing
separately") per the phase plan already in memory.

## How to run
```bash
cd Backend
pip install -r requirements.txt
cp .env.example .env   # fill in DB credentials
alembic upgrade head
```
