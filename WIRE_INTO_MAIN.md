# Wiring Phase 2 (corrected) into `main.py`

## What changed from the first delivery

The first Phase 2 zip assumed a SQLAlchemy ORM (`Property`, `Listing`,
`Location` model classes, `db.query(...)`, a `get_db()` dependency). Your
actual `database.py` is raw asyncpg with async functions returning plain
dicts. This version is rewritten against that reality:

- `Backend/search/filters.py` — now a pure pydantic schema, no DB imports
  at all. Safe to unit test without Postgres running.
- `Backend/search/service.py` — builds parameterised asyncpg SQL directly
  (`$1, $2, ...` placeholders), using `get_pool()` and
  `get_latest_availability()` from your `database.py`. No SQLAlchemy
  anywhere.
- `Backend/search/routes.py` — the endpoint is `async def` and calls
  `await search_properties(filters)` directly. No `Depends(get_db)`.
- `geo.py` is unchanged — it never touched the DB layer.

## Add to `Backend/main.py`

```python
from Backend.search.routes import router as search_router

app.include_router(search_router)
```

## Two things to check in your existing code before this goes live

**1. `search_listings_for_ai()` in `database.py`.** This already does
hard-filter + `created_at DESC` ordering for the AI/chatbot path — good,
it's clean. But it now duplicates logic with the new `search/service.py`.
For Phase 2 I left it alone rather than touching AI-chat code that isn't
in scope yet. When Phase 5 (AI preference extraction) is built, the right
move is to have the chatbot call `search/service.py`'s
`search_properties()` instead of `search_listings_for_ai()`, so there's
one search pipeline instead of two that could drift apart. Flagging now so
it's not a surprise later.

**2. `get_listing_stats()` in `database.py` is broken.** It selects
`l.city`, `l.county`, `l.is_available`, `l.is_featured`, `l.visibility_rank`
— none of which exist on `listings` after the property/listing split
(location fields moved to `locations`, and the visibility/featured columns
were intentionally removed). This will raise a Postgres "column does not
exist" error the first time the host analytics dashboard calls it. This is
unrelated to Phase 2 — let me know if you want it patched, since it
touches `payments.py`/dashboard code, not search.

## Run tests, then run the server

From the **project root** (`Rental Website`, the parent of `Backend`,
not inside `Backend` itself):

```powershell
cd "C:\Users\user\Projects\Rental Website"
python -m pytest Backend\search\test_search_phase2.py -v
uvicorn Backend.main:app --reload
```

The tests here only cover `geo.py` and `filters.py` (pure functions, no DB
needed). An integration test that actually calls `search_properties()`
against Postgres is a good next step once you have a dedicated test
database — happy to write that when you're ready.

## Schema fields this assumes exist

Pulled directly from your `create_property()` / listings SELECT in
`database.py`:

- `properties`: `id`, `property_type`, `title`, `bedrooms`, `bathrooms`,
  `furnished`, `parking_spaces`, `size_sqft`, `location_id`
- `listings`: `id`, `property_id`, `host_id`, `asking_price`,
  `service_charge`, `deposit`, `currency`, `status`, `available_from`,
  `last_confirmed_at`, `created_at`
- `locations`: `id`, `latitude`, `longitude`, `county`, `subcounty`,
  `neighbourhood`, `estate`
- `sponsored_placements`: `listing_id`, `is_active`, `end_date`,
  `start_date` (matches your existing `get_featured_listings()` query)

If any of these are off, it's a quick fix in `service.py`'s
`_SEARCH_SELECT` / `_build_where()` — the pipeline logic doesn't change.
