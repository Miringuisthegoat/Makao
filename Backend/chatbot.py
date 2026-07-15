"""
chatbot.py — Makao Rental Platform
=====================================
All chatbot business logic lives here:
  - Groq (LLaMA 3) drives the conversation with visitors
  - OpenAI GPT-4o (via matcher.py) ranks candidate listings
  - Conversation history is persisted per session via database.py
  - Visitor preferences are extracted from the chat as structured data
  - Agency / booster listings are prioritised in results (via DB query order)

FastAPI router defined at the bottom — registered in main.py:
    from chatbot import router as chatbot_router
    app.include_router(chatbot_router, prefix="/api", tags=["Chatbot"])

Endpoints exposed:
    POST /api/chat/message              — visitor sends a message
    GET  /api/chat/history/{session_id} — restore a session (page reload)
    DELETE /api/chat/clear/{session_id} — visitor starts a fresh conversation
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, status
from groq import AsyncGroq

import database as db
from matcher import match_listings  # OpenAI GPT-4o ranking
from models import (
    ChatRequest,
    ChatResponse,
    ConversationHistoryResponse,
    ChatMessage,
    ListingCardResponse,
    OKResponse,
    VisitorPreferences,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Groq client — LLaMA 3 for conversation
# ---------------------------------------------------------------------------
_groq_client: Optional[AsyncGroq] = None


def _get_groq() -> AsyncGroq:
    """Return the shared Groq async client, initialising on first call."""
    global _groq_client
    if _groq_client is None:
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set in .env")
        _groq_client = AsyncGroq(api_key=api_key)
    return _groq_client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Groq model — LLaMA 3 70B is the best-performing open model on Groq
GROQ_MODEL = "llama3-70b-8192"

# How many previous turns to send for context (keeps token usage reasonable)
MAX_HISTORY_TURNS = 12

# How many DB candidate listings to pass to GPT-4o for ranking
MATCHER_CANDIDATE_LIMIT = 30

# ---------------------------------------------------------------------------
# System prompt — controls Groq's persona and conversation flow
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are Makao AI, a friendly and knowledgeable property search assistant for Makao — Kenya's premier house rental platform.

Your job is to help visitors find their ideal rental property through natural, warm conversation — like a helpful friend who knows the Kenyan property market well.

PERSONALITY:
- Friendly, approachable, and professional
- Knowledgeable about Kenyan counties, cities, neighbourhoods, and rental market
- Patient — ask one question at a time, do not overwhelm the visitor
- Use simple, clear English (avoid jargon)
- Occasionally use light Kenyan expressions naturally (e.g. "sawa!", "pole pole") but don't overdo it

YOUR GOAL:
Collect the visitor's rental preferences through conversation so you can find matching properties. You need to gather:
1. Preferred location (county and/or city — e.g. Nairobi, Westlands; Mombasa, Nyali)
2. Budget — monthly rent range in KES
3. Number of bedrooms (0 = bedsitter/studio, 1, 2, 3, 4+)
4. Must-have amenities (parking, wifi, security, garden, gym, swimming pool, etc.)
5. Move-in urgency (immediately, this month, flexible)

RULES:
- Ask one or two questions at a time — do not fire a long list at once
- Always start by warmly greeting the visitor and asking what they are looking for
- Once you have location + budget + bedrooms, you have enough to search. Amenities and urgency are bonus context.
- When you have enough preferences, output a special JSON block (see below) BEFORE your friendly message so the backend can extract it. Do not show the JSON block to the visitor.
- Do not invent or describe specific properties — the database will provide real listings
- Do not discuss or reveal listing prices, package costs, or platform fees
- If the visitor asks something unrelated to property search, gently redirect them
- If the visitor is rude or asks inappropriate things, remain polite and professional
- Always respond in the same language the visitor uses (English or Swahili)

PREFERENCE EXTRACTION:
When you have collected enough information (at minimum: location + budget + bedrooms), output this block at the very start of your reply, before any other text:

<<<PREFERENCES>>>
{
  "county": "Nairobi",
  "city": "Westlands",
  "min_budget": 25000,
  "max_budget": 45000,
  "bedrooms": 2,
  "bathrooms": 1,
  "amenities": ["parking", "wifi"],
  "house_type": "apartment",
  "move_in_urgency": "this month"
}
<<<END_PREFERENCES>>>

Rules for the JSON:
- Use null for any field you do not yet know
- Budgets are in KES (numbers only, no commas or currency symbols)
- amenities must be from this list only: wifi, parking, security, garden, gym, swimming_pool, backup_power, water_storage, cctv, elevator, balcony, servant_quarter, furnished, air_conditioning, borehole, solar_power, fibre, pet_friendly, wheelchair_accessible
- Output the PREFERENCES block every time you reply AFTER you have enough info — the backend uses it to keep preferences up to date

AFTER MATCHING:
When the system provides you with matched property listings, present them warmly and naturally. Highlight 2-3 key reasons each property suits the visitor's needs. Invite them to click any listing for full details. If no listings match, empathise and suggest adjusting their search criteria."""


# ===========================================================================
# ── PREFERENCE EXTRACTION ────────────────────────────────────────────────────
# ===========================================================================

def _extract_preferences(reply: str) -> tuple[str, Optional[VisitorPreferences]]:
    """
    Parse the <<<PREFERENCES>>>...<<<END_PREFERENCES>>> block from Groq's reply.

    Returns:
        clean_reply  — the reply text with the JSON block stripped out
        preferences  — a VisitorPreferences instance, or None if block absent
    """
    pattern = r"<<<PREFERENCES>>>(.*?)<<<END_PREFERENCES>>>"
    match = re.search(pattern, reply, re.DOTALL)

    if not match:
        return reply.strip(), None

    # Remove the block from the visible reply
    clean_reply = re.sub(pattern, "", reply, flags=re.DOTALL).strip()

    try:
        raw = json.loads(match.group(1).strip())
        prefs = VisitorPreferences(
            county=raw.get("county"),
            city=raw.get("city"),
            min_budget=raw.get("min_budget"),
            max_budget=raw.get("max_budget"),
            bedrooms=raw.get("bedrooms"),
            bathrooms=raw.get("bathrooms"),
            amenities=raw.get("amenities") or [],
            house_type=raw.get("house_type"),
            move_in_urgency=raw.get("move_in_urgency"),
        )
        return clean_reply, prefs
    except Exception as exc:
        logger.warning(f"Failed to parse preferences JSON from Groq reply: {exc}")
        return clean_reply, None


# ===========================================================================
# ── HISTORY HELPERS ──────────────────────────────────────────────────────────
# ===========================================================================

async def _load_history(session_id: str) -> list[dict[str, str]]:
    """
    Load the last MAX_HISTORY_TURNS messages from DB and format them
    as the message list Groq expects: [{"role": ..., "content": ...}, ...]
    """
    rows = await db.get_conversation_history(session_id, limit=MAX_HISTORY_TURNS)
    return [{"role": r["role"], "content": r["message"]} for r in rows]


async def _save_turn(session_id: str, user_msg: str, assistant_reply: str) -> None:
    """Persist both sides of a conversation turn to the DB."""
    await db.save_message(session_id, "user", user_msg)
    await db.save_message(session_id, "assistant", assistant_reply)


# ===========================================================================
# ── GROQ CALL ────────────────────────────────────────────────────────────────
# ===========================================================================

async def _call_groq(messages: list[dict[str, Any]]) -> str:
    """
    Send a message list to Groq (LLaMA 3) and return the assistant's reply text.
    The system prompt is always prepended; history + new user message follow.
    """
    groq = _get_groq()

    full_messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + messages

    completion = await groq.chat.completions.create(
        model=GROQ_MODEL,
        messages=full_messages,
        temperature=0.65,      # slightly creative but grounded
        max_tokens=1024,
        top_p=0.9,
    )

    reply = completion.choices[0].message.content or ""
    return reply.strip()


# ===========================================================================
# ── LISTING INJECTION PROMPT ─────────────────────────────────────────────────
# ===========================================================================

def _build_listings_context(matches: list[dict[str, Any]]) -> str:
    """
    Format ranked listing matches from GPT-4o into a system-style context
    string that is injected into the Groq message list so LLaMA 3 can
    present them naturally to the visitor.
    """
    if not matches:
        return (
            "[SYSTEM: No listings matched the visitor's preferences. "
            "Empathise and suggest they broaden their search criteria — "
            "try a different county, higher budget, or fewer required amenities.]"
        )

    lines = ["[SYSTEM: Present these matched properties to the visitor warmly.\n"]
    for i, m in enumerate(matches, start=1):
        photos_note = f"{len(m.get('photos', []))} photo(s)" if m.get("photos") else "no photos yet"
        lines.append(
            f"  {i}. {m['title']} — {m['city']}, {m['county']}\n"
            f"     Bedrooms: {m['bedrooms']} | Bathrooms: {m['bathrooms']}\n"
            f"     Monthly Rent: KES {m['price_per_month']:,.0f}\n"
            f"     Why it matches: {m.get('explanation', 'Fits the visitor search criteria')}\n"
            f"     Match score: {m.get('match_score', 0):.0%} | Photos: {photos_note}\n"
            f"     Listing ID: {m['listing_id']}\n"
        )
    lines.append(
        "\nFor each property tell the visitor 2-3 personalised reasons it suits them. "
        "Invite them to click any listing to see full details and photos.]"
    )
    return "".join(lines)


# ===========================================================================
# ── CORE CHAT HANDLER ────────────────────────────────────────────────────────
# ===========================================================================

async def handle_chat_message(session_id: str, user_message: str) -> ChatResponse:
    """
    Process a single visitor message and return the assistant's reply.

    Flow:
      1. Load conversation history from DB
      2. Append the new user message
      3. Send to Groq (LLaMA 3) for a response
      4. Extract <<<PREFERENCES>>> block if present
      5. If preferences found → query DB → rank with GPT-4o (matcher.py)
      6. If listings found → inject into context → get final Groq reply
      7. Persist both turns to DB
      8. Return ChatResponse with reply + optional listing cards
    """
    # ── 1. Load history ────────────────────────────────────────────────────
    history = await _load_history(session_id)

    # ── 2. Append new user message ─────────────────────────────────────────
    history.append({"role": "user", "content": user_message})

    # ── 3. First Groq call — get response + possibly extract preferences ───
    groq_reply_raw = await _call_groq(history)
    clean_reply, preferences = _extract_preferences(groq_reply_raw)

    matched_listings: list[dict[str, Any]] = []
    listing_cards: list[ListingCardResponse] = []
    final_reply = clean_reply

    # ── 4 & 5. If preferences extracted → fetch DB candidates → GPT-4o rank ─
    if preferences:
        try:
            # Build filter dict for DB query — only pass fields Groq has filled in
            db_filters: dict[str, Any] = {}
            if preferences.county:
                db_filters["county"] = preferences.county
            if preferences.city:
                db_filters["city"] = preferences.city
            if preferences.max_budget is not None:
                db_filters["max_budget"] = preferences.max_budget
            if preferences.bedrooms is not None:
                db_filters["bedrooms"] = preferences.bedrooms
            if preferences.amenities:
                db_filters["amenities"] = preferences.amenities

            # Fetch broad candidate set (ordered by chatbot_priority, agency, rank)
            candidates = await db.search_listings_for_ai(db_filters)

            if candidates:
                # GPT-4o ranks and explains the matches
                match_response = await match_listings(
                    preferences=preferences,
                    candidate_listings=candidates,
                )
                matched_listings = [m.model_dump() for m in match_response.matches]

                # Build listing cards for the frontend to render as clickable tiles
                listing_cards = [
                    ListingCardResponse(
                        id=m["listing_id"],
                        title=m["title"],
                        city=m["city"],
                        county=m["county"],
                        price_per_month=m["price_per_month"],
                        bedrooms=m["bedrooms"],
                        bathrooms=m["bathrooms"],
                        photos=m.get("photos", []),
                        is_featured=False,      # cards don't need featured flag
                        package_type="",        # not surfaced in chat UI
                        host_name=None,
                        host_account_type=None,
                    )
                    for m in matched_listings
                ]

        except Exception as exc:
            # Matching failure is non-fatal — log and continue without listings
            logger.error(f"Matcher error for session {session_id}: {exc}")

    # ── 6. If we have listings, inject context and get a natural presentation ─
    if matched_listings:
        listings_context = _build_listings_context(matched_listings)

        # Append listings context as a system-style turn so Groq presents them
        history_with_context = history + [
            {"role": "assistant", "content": clean_reply},
            {"role": "user",      "content": listings_context},
        ]
        try:
            final_reply = await _call_groq(history_with_context)
            # Strip any stray PREFERENCES block from the presentation reply
            final_reply, _ = _extract_preferences(final_reply)
        except Exception as exc:
            logger.error(f"Groq listing presentation error: {exc}")
            # Fall back to the first clean reply if the second call fails
            final_reply = clean_reply

    # ── 7. Persist both turns ──────────────────────────────────────────────
    # We save the original user message and the final assistant reply
    await _save_turn(session_id, user_message, final_reply)

    # ── 8. Return response ─────────────────────────────────────────────────
    return ChatResponse(
        session_id=session_id,
        reply=final_reply,
        listings=listing_cards if listing_cards else None,
    )


# ===========================================================================
# ── FASTAPI ROUTER ────────────────────────────────────────────────────────────
# ===========================================================================

router = APIRouter()


@router.post(
    "/chat/message",
    response_model=ChatResponse,
    summary="Send a message to the Makao AI chatbot",
    description=(
        "Visitor sends a message. Groq (LLaMA 3) responds. "
        "If preferences are collected, GPT-4o matches and ranks listings. "
        "session_id must be a UUID v4 generated and stored client-side."
    ),
)
async def chat_message(body: ChatRequest) -> ChatResponse:
    """
    Main chatbot endpoint — called by chatbot.js on every visitor message.

    The session_id is generated client-side (UUID v4) and stored in
    localStorage so the conversation persists across page navigations.
    """
    if not body.session_id or not body.message.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="session_id and message are required.",
        )

    return await handle_chat_message(
        session_id=body.session_id,
        user_message=body.message.strip(),
    )


@router.get(
    "/chat/history/{session_id}",
    response_model=ConversationHistoryResponse,
    summary="Restore a visitor's chat history",
    description=(
        "Returns the message history for a session. "
        "Called by chatbot.js when the chat widget is reopened "
        "and a session_id already exists in localStorage."
    ),
)
async def get_chat_history(session_id: str) -> ConversationHistoryResponse:
    """
    Restore a previous chat session.
    Returns an empty message list if the session does not exist yet.
    """
    rows = await db.get_conversation_history(session_id, limit=MAX_HISTORY_TURNS)

    messages = [
        ChatMessage(role=r["role"], message=r["message"])
        for r in rows
    ]

    return ConversationHistoryResponse(
        session_id=session_id,
        messages=messages,
    )


@router.delete(
    "/chat/clear/{session_id}",
    response_model=OKResponse,
    summary="Clear a visitor's chat history",
    description=(
        "Deletes all messages for a session. "
        "Called when the visitor clicks 'Start over' in the chat widget."
    ),
)
async def clear_chat(session_id: str) -> OKResponse:
    """
    Delete all conversation history for a session.
    The visitor can start a fresh search after this call.
    """
    await db.clear_conversation(session_id)
    return OKResponse(message="Conversation cleared. Ready for a new search!")