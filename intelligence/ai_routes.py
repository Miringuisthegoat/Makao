"""
Backend/intelligence/ai_routes.py — Phase 5 endpoints.

Register in main.py:
    from intelligence.ai_routes import router as ai_router
    app.include_router(ai_router, prefix="/api", tags=["AI"])
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from intelligence.explanation import explain
from intelligence.preference_extractor import extract_preferences

router = APIRouter(prefix="/ai")


class ExtractRequest(BaseModel):
    message: str = Field(..., min_length=3, max_length=1000)


class ExplainRequest(BaseModel):
    score: Dict[str, Any]                 # ScoreBreakdown.to_dict()
    facts: Optional[Dict[str, Any]] = None


@router.post("/extract-preferences")
async def extract_endpoint(body: ExtractRequest):
    """Free text -> UserPreferencesUpsert-shaped dict + at most ONE follow-up."""
    return await extract_preferences(body.message)


@router.post("/explain")
async def explain_endpoint(body: ExplainRequest):
    """Score-grounded explanation. Text only — never changes ranking."""
    return await explain(body.score, body.facts)
