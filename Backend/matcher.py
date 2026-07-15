"""
matcher.py — Makao Rental Platform
=====================================
OpenAI GPT-4o property matching and ranking logic.

Called exclusively by chatbot.py — not exposed as an API endpoint.

Flow:
  chatbot.py collects visitor preferences via Groq
       ↓
  database.py returns up to 30 candidate listings
       ↓
  match_listings() sends preferences + candidates to GPT-4o
       ↓
  GPT-4o returns a ranked list with per-listing explanations
       ↓
  chatbot.py injects results back into the Groq conversation

Agency listings and chatbot_priority boosted listings are already
sorted first by the DB query in database.search_listings_for_ai().
GPT-4o refines the ranking further by relevance to stated preferences.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from models import MatchRequest, MatchResponse, MatchResult, VisitorPreferences

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------
_openai_client: AsyncOpenAI | None = None


def _get_openai() -> AsyncOpenAI:
    """Return the shared OpenAI async client, initialising on first call."""
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in .env")
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENAI_MODEL = "gpt-4o"

# Maximum listings GPT-4o will return after ranking (top N matches)
MAX_MATCHES_RETURNED = 5

# ---------------------------------------------------------------------------
# System prompt for the matcher
# ---------------------------------------------------------------------------

_MATCHER_SYSTEM_PROMPT = """You are a property matching engine for Makao — Kenya's house rental platform.

You receive:
  1. A visitor's rental preferences (JSON)
  2. A list of candidate property listings from the database (JSON array)

Your job:
  - Analyse each listing against the visitor's preferences
  - Select the TOP 5 most suitable listings
  - For each selected listing, write a SHORT, personalised explanation (1-2 sentences max)
    telling the visitor WHY this property suits their specific needs
  - Assign a match_score between 0.0 and 1.0 (1.0 = perfect match)
  - Listings that already have a higher visibility_rank, are featured, or are agency listings
    should be ranked higher when match quality is otherwise equal

CRITICAL RULES:
  - Return ONLY a valid JSON object — no markdown, no preamble, no extra text
  - Always include "listing_id" from the input data exactly as given
  - explanations must be friendly, specific to the visitor's stated preferences
  - If fewer than 5 listings are available, return all of them
  - If no listings match at all, return an empty matches array

OUTPUT FORMAT (strict JSON only):
{
  "matches": [
    {
      "listing_id": 42,
      "title": "Modern 2BR in Westlands",
      "city": "Westlands",
      "county": "Nairobi",
      "price_per_month": 35000,
      "bedrooms": 2,
      "bathrooms": 1,
      "photos": ["url1", "url2"],
      "explanation": "Fits your 2-bedroom budget perfectly and is in your preferred area of Westlands.",
      "match_score": 0.92
    }
  ],
  "summary": "I found 3 properties in Westlands within your budget of KES 25,000–45,000."
}"""


# ===========================================================================
# ── MAIN MATCHING FUNCTION ───────────────────────────────────────────────────
# ===========================================================================

async def match_listings(
    preferences: VisitorPreferences,
    candidate_listings: list[dict[str, Any]],
) -> MatchResponse:
    """
    Rank candidate listings against visitor preferences using GPT-4o.

    Args:
        preferences:        Structured preferences extracted from the chat by Groq
        candidate_listings: Raw listing dicts from database.search_listings_for_ai()

    Returns:
        MatchResponse with ranked MatchResult list and a friendly summary string

    Raises:
        RuntimeError if the OpenAI call fails (caller handles gracefully)
    """
    if not candidate_listings:
        return MatchResponse(
            matches=[],
            summary="No properties are currently available matching your criteria.",
        )

    # ── Build the user prompt ──────────────────────────────────────────────
    preferences_json = preferences.model_dump(exclude_none=False)

    # Trim candidate data to only fields GPT-4o needs — keeps tokens lean
    slim_candidates = [
        {
            "listing_id":       c["id"],
            "title":            c["title"],
            "city":             c.get("city", ""),
            "county":           c.get("county", ""),
            "price_per_month":  float(c.get("price_per_month", 0)),
            "bedrooms":         c.get("bedrooms", 0),
            "bathrooms":        c.get("bathrooms", 1),
            "size_sqft":        c.get("size_sqft"),
            "amenities":        c.get("amenities") or [],
            "photos":           c.get("photos") or [],
            "is_featured":      c.get("is_featured", False),
            "package_type":     c.get("package_type", "landlord"),
            "visibility_rank":  c.get("visibility_rank", 0),
            "has_chatbot_priority": c.get("has_chatbot_priority", False),
            "description":      (c.get("description") or "")[:300],  # truncate long descriptions
        }
        for c in candidate_listings
    ]

    user_prompt = (
        f"VISITOR PREFERENCES:\n{json.dumps(preferences_json, indent=2)}\n\n"
        f"CANDIDATE LISTINGS ({len(slim_candidates)} properties):\n"
        f"{json.dumps(slim_candidates, indent=2)}\n\n"
        f"Return the top {MAX_MATCHES_RETURNED} matches as JSON only."
    )

    # ── Call GPT-4o ────────────────────────────────────────────────────────
    client = _get_openai()

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _MATCHER_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.2,        # low temperature = consistent, deterministic ranking
            max_tokens=1500,
            response_format={"type": "json_object"},  # enforce JSON output
        )
    except Exception as exc:
        logger.error(f"OpenAI GPT-4o matcher call failed: {exc}")
        raise RuntimeError(f"Property matching failed: {exc}") from exc

    raw_content = response.choices[0].message.content or "{}"

    # ── Parse response ─────────────────────────────────────────────────────
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error(f"GPT-4o returned invalid JSON: {exc}\nRaw: {raw_content[:500]}")
        return MatchResponse(
            matches=[],
            summary="Sorry, I had trouble processing property matches. Please try again.",
        )

    # ── Validate and build MatchResult objects ─────────────────────────────
    raw_matches = parsed.get("matches", [])
    summary = parsed.get("summary", "Here are the best matching properties for you.")

    valid_matches: list[MatchResult] = []
    for item in raw_matches[:MAX_MATCHES_RETURNED]:
        try:
            match = MatchResult(
                listing_id=int(item["listing_id"]),
                title=str(item.get("title", "")),
                city=str(item.get("city", "")),
                county=str(item.get("county", "")),
                price_per_month=float(item.get("price_per_month", 0)),
                bedrooms=int(item.get("bedrooms", 0)),
                bathrooms=int(item.get("bathrooms", 1)),
                photos=item.get("photos") or [],
                explanation=str(item.get("explanation", "Good match for your needs.")),
                match_score=float(item.get("match_score", 0.5)),
            )
            valid_matches.append(match)
        except Exception as exc:
            logger.warning(f"Skipping malformed match result from GPT-4o: {exc} | item={item}")
            continue

    logger.info(
        f"Matcher returned {len(valid_matches)} matches "
        f"from {len(candidate_listings)} candidates."
    )

    return MatchResponse(matches=valid_matches, summary=summary)