"""
preferences.py — Makao Rental Platform
========================================
Business logic for the renter preference layer introduced in Phase 1B:
  - user_preferences  — one structured row per renter (GET/PUT /preferences)
  - saved_searches     — reusable structured searches (P1 stretch)
  - favorites           — saved properties, keyed on property_id (P1 stretch)

This module sits between main.py's route handlers and database.py's raw
queries. It owns validation that needs a DB lookup (e.g. "does this
property exist?") and shapes DB rows into the Pydantic response models
defined in models.py.

Architectural note: user_preferences is the structured shape that both the
renter's own form input AND the Phase 5 AI preference extractor write to.
Nothing in this module ranks or recommends properties — it only stores and
returns what the renter wants. Ranking happens in the Phase 2/3 search and
scoring services, which read this table.

No raw SQL here — all DB calls go through database.py.
No routes defined here — routes are registered in main.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import HTTPException, status

import database as db
from models import (
    FavoriteCreate,
    FavoriteResponse,
    SavedSearchCreate,
    SavedSearchResponse,
    UserPreferencesResponse,
    UserPreferencesUpsert,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# ── HELPERS ──────────────────────────────────────────────────────────────────
# ===========================================================================

def _parse_jsonb(value: Any) -> Any:
    """
    asyncpg returns JSONB columns as raw JSON text (no codec is configured
    on the pool), so callers that expect a Python list/dict must parse it
    themselves. Handles the case where a value already comes back parsed
    (e.g. under a different driver) without raising.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _row_to_preferences_response(row: Dict[str, Any]) -> UserPreferencesResponse:
    data = dict(row)
    data["preferred_neighbourhoods"] = _parse_jsonb(data.get("preferred_neighbourhoods")) or []
    return UserPreferencesResponse(**data)


def _row_to_saved_search_response(row: Dict[str, Any]) -> SavedSearchResponse:
    return SavedSearchResponse(
        id=row["id"],
        user_id=row["user_id"],
        search_criteria=_parse_jsonb(row.get("search_criteria_json")) or {},
        search_type=row["search_type"],
        created_at=row["created_at"],
        last_run_at=row.get("last_run_at"),
    )


def _row_to_favorite_response(row: Dict[str, Any]) -> FavoriteResponse:
    return FavoriteResponse(
        id=row["id"],
        user_id=row["user_id"],
        property_id=row["property_id"],
        created_at=row["created_at"],
    )


# ===========================================================================
# ── USER PREFERENCES ─────────────────────────────────────────────────────────
# ===========================================================================

async def get_preferences(user_id: int) -> UserPreferencesResponse:
    """
    GET /preferences

    Returns the renter's stored preferences, or a set of empty defaults if
    they have never saved any yet — the frontend preference form and the
    search page should not have to special-case "no preferences saved".
    """
    row = await db.get_user_preferences(user_id)
    if not row:
        now = datetime.now(tz=timezone.utc)
        return UserPreferencesResponse(
            user_id=user_id,
            parking_required=False,
            preferred_neighbourhoods=[],
            pets=False,
            created_at=now,
            updated_at=now,
        )
    return _row_to_preferences_response(row)


async def upsert_preferences(
    user_id: int, data: UserPreferencesUpsert
) -> UserPreferencesResponse:
    """
    PUT /preferences

    Full upsert of the renter's structured preferences. Called both from
    the renter's own preference form and (in Phase 5) from the AI
    preference extractor after it parses a free-text request — either way
    the shape written here is the same, and this function never makes a
    ranking decision itself.
    """
    fields = data.model_dump(exclude_unset=False)
    row = await db.upsert_user_preferences(user_id, fields)
    logger.info(f"Preferences upserted for user_id={user_id}")
    return _row_to_preferences_response(row)


def _utcnow_placeholder():
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc)


# ===========================================================================
# ── SAVED SEARCHES (P1 stretch) ─────────────────────────────────────────────
# ===========================================================================

async def create_saved_search(
    user_id: int, data: SavedSearchCreate
) -> SavedSearchResponse:
    """POST /saved-searches"""
    criteria = data.search_criteria.model_dump(exclude_none=True)
    row = await db.create_saved_search(user_id, criteria, data.search_type)
    return _row_to_saved_search_response(row)


async def list_saved_searches(user_id: int) -> List[SavedSearchResponse]:
    """GET /saved-searches"""
    rows = await db.list_saved_searches(user_id)
    return [_row_to_saved_search_response(r) for r in rows]


async def delete_saved_search(user_id: int, saved_search_id: int) -> Dict[str, Any]:
    """DELETE /saved-searches/{id}"""
    deleted = await db.delete_saved_search(saved_search_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Saved search {saved_search_id} not found.",
        )
    return {"ok": True, "message": f"Saved search {saved_search_id} deleted."}


# ===========================================================================
# ── FAVORITES (P1 stretch) — keyed on property_id ───────────────────────────
# ===========================================================================

async def add_favorite(user_id: int, data: FavoriteCreate) -> FavoriteResponse:
    """
    POST /favorites

    Favorites the physical property, not a particular listing/advertisement
    for it — consistent with the property != listing invariant. Validates
    the property actually exists before writing the favorite.
    """
    prop = await db.get_property_by_id(data.property_id)
    if not prop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property {data.property_id} not found.",
        )
    row = await db.create_favorite(user_id, data.property_id)
    return _row_to_favorite_response(row)


async def list_favorites(user_id: int) -> List[FavoriteResponse]:
    """GET /favorites"""
    rows = await db.list_favorites(user_id)
    return [_row_to_favorite_response(r) for r in rows]


async def remove_favorite(user_id: int, property_id: int) -> Dict[str, Any]:
    """DELETE /favorites/{property_id}"""
    deleted = await db.delete_favorite(user_id, property_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property {property_id} is not in your favorites.",
        )
    return {"ok": True, "message": f"Property {property_id} removed from favorites."}
