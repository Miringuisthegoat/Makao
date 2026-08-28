# Phase 1 — Progress Notes (partial delivery)

Phase 1 is being split into two sub-sections:

- **Phase 1A — Data & Listings Foundation** (this delivery)
  - Migration `0003_phase1_availability_preferences`: `availability_confirmations`,
    `user_preferences`, `saved_searches`, `favorites` tables; relaxes
    `users.host_type` to nullable so renter accounts can exist.
  - `models.py`: Property/Listing (new shape), AvailabilityConfirmation,
    UserPreferences, SavedSearch, Favorite schemas.
  - `database.py`: property/listing/location CRUD, paginated organic search
    (created_at DESC only — no payment-influenced ordering), availability
    confirmation read/write with `last_confirmed_at` sync, user_preferences
    upsert, saved_searches/favorites CRUD.
  - `listings.py`: rewritten — `create_property_and_listing`,
    `confirm_availability`, new read/update/delete. Deprecated no-op shims
    (`activate_listing`, `feature_listing`, `bump_listing`, `unfeature_listing`)
    keep `payments.py` importable without restoring payment-influenced ranking.
  - `main.py`: fixed imports; neutralized `_sync_featured_status_after_expiry`
    (was querying a column migration 0002 already dropped); updated
    browse/create listing routes; added `PATCH /listings/{id}/property` and
    `POST /listings/{id}/availability`.

- **Phase 1B — Renter Identity & Preferences API** (not yet built)
  - Generalize `auth.py` so renters can sign up/login (today `SignupRequest`
    only accepts `host_type` in `landlord|agency`; `create_access_token`/
    `signup_host` are host-only). This is the one open architectural
    decision point before proceeding.
  - New `preferences.py` module (business logic for `user_preferences`).
  - Routes: `GET/PUT /api/preferences`, `POST/GET /api/saved-searches`,
    `POST/GET/DELETE /api/favorites`.
  - Patch `payments.py`'s two call sites that still index
    `l["is_available"]` / `l["package_type"]` on `get_listings_by_host`
    results (compat keys now exist in `database.py` but aren't wired
    through payments' filtering logic yet).
  - Update `setup_sqlite_dev.py` for the new tables (dev convenience only —
    production schema is Alembic-owned).

## Known deferred items (not Phase 1 scope, flagged for later phases)

- `payments.py`'s package/booster model (`landlord`/`agency` "pay to
  activate/boost a listing") is now architecturally orphaned by the
  property/listing split and payment-isolation invariant. It needs a real
  redesign around `sponsored_placements` — this is Phase 2/V1 monetization
  work (roadmap V1 §32), not a Phase 1 fix.
- `matcher.py` (GPT-4o as ranking engine) is still a known architecture
  violation, explicitly deferred to Phase 5.
- `chatbot.py`'s `search_listings_for_ai` call still returns candidates
  ordered by `created_at DESC` only (fixed to remove payment bias) but the
  chatbot's own re-ranking via GPT-4o has not been touched.
- Host/agency dashboard analytics (`db.get_listing_stats`, `AnalyticsListingRow`)
  still reference the pre-Phase0 flat listing shape and will need a
  follow-up pass once the monetization model is redesigned.
