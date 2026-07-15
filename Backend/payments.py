"""
payments.py — Makao Rental Platform
======================================
All payment processing logic lives here:
  - M-Pesa Daraja API (STK Push + callback confirmation)
  - Stripe (card payments + PayPal via Stripe)
  - Booster purchases (all payment methods)
  - Listing activation after confirmed payment
  - Webhook and callback signature verification

FastAPI router defined at the bottom — register in main.py:
    from payments import router as payments_router
    app.include_router(payments_router, prefix="/api/payments", tags=["Payments"])

Endpoints exposed:
    POST /api/payments/mpesa/initiate           — trigger STK push (protected)
    POST /api/payments/mpesa/callback           — Daraja callback (public, Safaricom only)
    GET  /api/payments/mpesa/status/{id}        — poll payment status (protected)
    POST /api/payments/stripe/create-intent     — create Stripe PaymentIntent (protected)
    POST /api/payments/stripe/webhook           — Stripe webhook (public, Stripe signed)
    GET  /api/payments/history                  — host billing history (protected)

PAYMENT RULES (enforced here, not just on frontend):
  - A listing is NEVER activated before payment status = 'completed'
  - M-Pesa confirmation comes via Daraja callback URL
  - Stripe confirmation comes via signed webhook event
  - All amounts are stored and processed in KES
  - All payment records are written to the payments table via database.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import stripe
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

import database as db
from auth import get_current_host
from config import BOOSTERS, PRICING
from listings import (
    activate_listing,
    bump_listing,
    feature_listing,
)
from models import (
    BoosterPurchaseRequest,
    BoosterResponse,
    MpesaCallbackPayload,
    MpesaInitiateResponse,
    MpesaPaymentRequest,
    OKResponse,
    PaymentResponse,
    PaymentStatusResponse,
    StripePaymentRequest,
    UserResponse,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ── CONFIGURATION — all values from .env, never hardcoded ───────────────────
# ---------------------------------------------------------------------------

# Stripe
STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# M-Pesa Daraja
MPESA_CONSUMER_KEY: str = os.getenv("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET: str = os.getenv("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE: str = os.getenv("MPESA_SHORTCODE", "")
MPESA_PASSKEY: str = os.getenv("MPESA_PASSKEY", "")
MPESA_CALLBACK_URL: str = os.getenv("MPESA_CALLBACK_URL", "")

# Daraja endpoints (sandbox → production swap via env var)
MPESA_ENV: str = os.getenv("MPESA_ENV", "sandbox")  # 'sandbox' | 'production'
_DARAJA_BASE = (
    "https://api.safaricom.co.ke"
    if MPESA_ENV == "production"
    else "https://sandbox.safaricom.co.ke"
)
DARAJA_AUTH_URL = f"{_DARAJA_BASE}/oauth/v1/generate?grant_type=client_credentials"
DARAJA_STK_URL = f"{_DARAJA_BASE}/mpesa/stkpush/v1/processrequest"
DARAJA_QUERY_URL = f"{_DARAJA_BASE}/mpesa/stkpushquery/v1/query"

# Stripe initialisation
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.warning("STRIPE_SECRET_KEY not set — Stripe payments will not work.")


# ===========================================================================
# ── HELPERS ──────────────────────────────────────────────────────────────────
# ===========================================================================

def _package_price(package_type: str) -> float:
    """
    Read the price for a listing package or booster from config.py.
    Returns 0 if the key is not found (owner sets real values in config.py).
    """
    if package_type in PRICING:
        return float(PRICING[package_type].get("price", 0))
    if package_type in BOOSTERS:
        return float(BOOSTERS[package_type].get("price", 0))
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown package or booster type: '{package_type}'",
    )


def _booster_end_date(booster_type: str) -> Optional[datetime]:
    """
    Calculate the end datetime for a booster based on its duration in config.py.
    Returns None for boosters with no expiry (e.g. chatbot_priority).
    """
    config = BOOSTERS.get(booster_type, {})
    duration_days = config.get("duration_days")
    if duration_days:
        return datetime.now(tz=timezone.utc) + timedelta(days=duration_days)
    return None


def _row_to_payment_response(row: dict) -> PaymentResponse:
    """Convert a raw DB payment row → PaymentResponse Pydantic model."""
    return PaymentResponse(
        id=row["id"],
        payment_method=row["payment_method"],
        transaction_id=row.get("transaction_id"),
        amount=float(row["amount"]),
        currency=row.get("currency", "KES"),
        package_type=row["package_type"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def _activate_after_payment(payment_row: dict) -> None:
    """
    Central post-payment activation hook.
    Called by BOTH the M-Pesa callback and the Stripe webhook after
    a payment reaches 'completed' status.

    Handles:
      - Listing package payments  → activate the listing
      - Booster purchases         → activate the booster side-effects
    """
    package_type = payment_row.get("package_type", "")
    payment_id = payment_row["id"]
    host_id = payment_row["host_id"]

    # ── Listing package (landlord / agency) ──────────────────────────────
    if package_type in ("landlord", "agency"):
        # Find the most recent inactive listing for this host + package type
        host_listings = await db.get_listings_by_host(host_id)
        pending = [
            l for l in host_listings
            if not l["is_available"] and l["package_type"] == package_type
        ]
        if pending:
            # Activate the most recently created inactive listing
            pending.sort(key=lambda x: x["created_at"], reverse=True)
            target = pending[0]
            await activate_listing(target["id"])
            logger.info(
                f"Listing {target['id']} activated after {package_type} payment "
                f"(payment_id={payment_id})."
            )
        else:
            logger.warning(
                f"Payment {payment_id} completed but no pending listing found "
                f"for host {host_id} / package {package_type}."
            )
        return

    # ── Booster purchase ──────────────────────────────────────────────────
    if package_type in BOOSTERS:
        # The listing_id is stored in the payment metadata (Stripe) or
        # looked up via the most recently created booster record (M-Pesa).
        # Here we look for a pending (is_active=False) booster for this payment.
        # The booster row was created by the initiate endpoint before payment.
        pending_boosters = await db.get_pending_booster_by_payment(payment_id)
        if not pending_boosters:
            logger.warning(f"No pending booster found for payment_id={payment_id}.")
            return

        for booster in pending_boosters:
            listing_id = booster["listing_id"]
            booster_type = booster["booster_type"]
            end_date = _booster_end_date(booster_type)

            # Activate booster record in DB
            await db.activate_booster(booster["id"], end_date)

            # Apply booster side-effects to the listing
            if booster_type == "homepage_feature":
                await feature_listing(listing_id)

            elif booster_type == "refresh_listing":
                await bump_listing(listing_id)

            elif booster_type == "extend_visibility":
                # extend_visibility prolongs highlighted status — bump rank
                await db.bump_visibility_rank(listing_id, increment=50)

            elif booster_type == "chatbot_priority":
                # No listing field change needed — the DB query in
                # search_listings_for_ai() checks the boosters table directly
                pass

            logger.info(
                f"Booster '{booster_type}' activated on listing {listing_id} "
                f"(payment_id={payment_id})."
            )


# ===========================================================================
# ── M-PESA DARAJA ────────────────────────────────────────────────────────────
# ===========================================================================

async def _daraja_get_token() -> str:
    """
    Fetch a short-lived OAuth access token from the Safaricom Daraja API.
    Token is valid for 1 hour — for production, cache it to avoid rate limits.
    """
    credentials = base64.b64encode(
        f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            DARAJA_AUTH_URL,
            headers={"Authorization": f"Basic {credentials}"},
        )

    if response.status_code != 200:
        logger.error(f"Daraja auth failed: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to M-Pesa. Please try again.",
        )

    return response.json().get("access_token", "")


def _daraja_password() -> tuple[str, str]:
    """
    Generate the Daraja STK push password and timestamp.
    Password = Base64(ShortCode + Passkey + Timestamp)
    Returns (password, timestamp) both as strings.
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


async def initiate_mpesa_payment(
    host_id: int,
    phone: str,
    package_type: str,
    listing_id: Optional[int] = None,
) -> MpesaInitiateResponse:
    """
    Trigger an M-Pesa STK Push to the host's phone.

    Steps:
      1. Read price from config.py
      2. Create a pending payment record in DB
      3. Get Daraja OAuth token
      4. Send STK push request to Daraja
      5. Store CheckoutRequestID for status polling
      6. Return checkout_request_id to frontend for polling

    The listing is NOT activated here — only after Daraja callback confirms.
    """
    amount = _package_price(package_type)

    # 1. Create pending payment record
    payment = await db.create_payment(
        host_id=host_id,
        payment_method="mpesa",
        amount=amount,
        package_type=package_type,
        transaction_id=None,   # filled in after STK push response
    )

    # If this is a booster, pre-create an inactive booster record now
    # so _activate_after_payment() can find it via payment_id later
    if package_type in BOOSTERS and listing_id:
        await db.create_booster(
            listing_id=listing_id,
            booster_type=package_type,
            payment_id=payment["id"],
            end_date=None,        # set on activation
            is_active=False,      # inactive until payment confirmed
        )

    # 2. Get Daraja token
    token = await _daraja_get_token()
    password, timestamp = _daraja_password()

    # 3. Build STK push payload
    # Amount must be a whole integer for M-Pesa
    mpesa_amount = max(1, int(round(amount)))

    stk_payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": mpesa_amount,
        "PartyA": phone,               # customer phone: 2547XXXXXXXX
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": f"Makao-{payment['id']}",
        "TransactionDesc": f"Makao {package_type} listing payment",
    }

    # 4. Send STK push
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            DARAJA_STK_URL,
            json=stk_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    if response.status_code != 200:
        logger.error(f"STK push failed: {response.text}")
        await db.update_payment_status_by_id(payment["id"], "failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="M-Pesa payment request failed. Please try again.",
        )

    stk_data = response.json()
    response_code = stk_data.get("ResponseCode", "")

    if response_code != "0":
        logger.error(f"STK push rejected by Daraja: {stk_data}")
        await db.update_payment_status_by_id(payment["id"], "failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=stk_data.get(
                "ResponseDescription",
                "M-Pesa request was rejected. Please check the phone number.",
            ),
        )

    # 5. Store CheckoutRequestID as transaction_id for callback matching
    checkout_request_id: str = stk_data.get("CheckoutRequestID", "")
    await db.set_payment_transaction_id(payment["id"], checkout_request_id)

    logger.info(
        f"STK push sent — host={host_id}, phone={phone}, "
        f"package={package_type}, checkout_id={checkout_request_id}"
    )

    return MpesaInitiateResponse(
        ok=True,
        checkout_request_id=checkout_request_id,
        message="Check your phone and enter your M-Pesa PIN to complete payment.",
    )


async def handle_mpesa_callback(payload: dict) -> OKResponse:
    """
    Process the Daraja STK push callback from Safaricom.

    Safaricom POSTs to MPESA_CALLBACK_URL after the customer
    enters their PIN (or the prompt times out / is rejected).

    Payload structure (Daraja v1):
    {
      "Body": {
        "stkCallback": {
          "MerchantRequestID": "...",
          "CheckoutRequestID": "...",
          "ResultCode": 0,         ← 0 = success
          "ResultDesc": "...",
          "CallbackMetadata": {
            "Item": [...]          ← present only on success
          }
        }
      }
    }

    CRITICAL: The listing is activated HERE and only here — after
    we confirm ResultCode = 0 (payment successful).
    """
    try:
        callback = payload["Body"]["stkCallback"]
        checkout_id: str = callback["CheckoutRequestID"]
        result_code: int = int(callback["ResultCode"])
        result_desc: str = callback.get("ResultDesc", "")
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(f"Malformed M-Pesa callback payload: {exc}")
        # Always return 200 to Safaricom — they retry on non-200
        return OKResponse(message="Callback received.")

    # Look up the pending payment by CheckoutRequestID
    payment = await db.get_payment_by_transaction(checkout_id)
    if not payment:
        logger.warning(f"No payment found for checkout_id={checkout_id}")
        return OKResponse(message="Callback received.")

    if result_code == 0:
        # ── Payment successful ─────────────────────────────────────────
        new_status = "completed"
        logger.info(f"M-Pesa payment confirmed: checkout_id={checkout_id}")
    else:
        # ── Payment failed / cancelled by user ─────────────────────────
        new_status = "failed"
        logger.warning(
            f"M-Pesa payment failed: checkout_id={checkout_id}, "
            f"code={result_code}, desc={result_desc}"
        )

    # Update payment status in DB
    updated = await db.update_payment_status(checkout_id, new_status)

    # Activate listing / booster only on confirmed success
    if new_status == "completed" and updated:
        await _activate_after_payment(updated)

    # Always acknowledge Safaricom with 200
    return OKResponse(message="Callback processed.")


async def get_mpesa_payment_status(
    checkout_request_id: str,
    host_id: int,
) -> PaymentStatusResponse:
    """
    Check payment status for a given CheckoutRequestID.
    Called by the frontend polling loop (payment-mpesa.html).

    Checks the local DB first. If still pending, optionally queries
    the Daraja STK push query endpoint to sync status.
    """
    payment = await db.get_payment_by_transaction(checkout_request_id)

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment record not found.",
        )

    # Ownership check — host can only query their own payments
    if payment["host_id"] != host_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    current_status = payment["status"]

    if current_status == "completed":
        return PaymentStatusResponse(
            status="completed",
            message="Payment confirmed! Your listing is now live.",
        )

    if current_status == "failed":
        return PaymentStatusResponse(
            status="failed",
            message="Payment was not completed. Please try again.",
        )

    # Still pending — return pending; Daraja callback will update asynchronously
    return PaymentStatusResponse(
        status="pending",
        message="Waiting for M-Pesa confirmation. Please check your phone.",
    )


# ===========================================================================
# ── STRIPE (Card + PayPal) ───────────────────────────────────────────────────
# ===========================================================================

async def create_stripe_payment_intent(
    host_id: int,
    package_type: str,
    payment_method_type: str,   # 'card' | 'paypal'
    listing_id: Optional[int] = None,
) -> dict:
    """
    Create a Stripe PaymentIntent for card or PayPal payment.

    Steps:
      1. Read price from config.py
      2. Create or retrieve Stripe customer for this host
      3. Create a Stripe PaymentIntent in KES
      4. Create pending payment record in DB
      5. Return client_secret to frontend for Stripe.js confirmation

    The listing is NOT activated here — only after Stripe webhook confirms.
    """
    amount_kes = _package_price(package_type)

    # Stripe amounts are in the smallest currency unit.
    # KES is a zero-decimal currency — 1 KES = 1 unit (no paise/cents).
    amount_units = int(round(amount_kes))

    # Get or create Stripe customer for this host
    host = await db.get_user_by_id(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found.")

    stripe_customer_id = host.get("stripe_customer_id")
    if not stripe_customer_id:
        try:
            customer = stripe.Customer.create(
                email=host["email"],
                name=host["full_name"],
                metadata={"makao_host_id": str(host_id)},
            )
            stripe_customer_id = customer.id
            await db.update_user(host_id, {"stripe_customer_id": stripe_customer_id})
        except stripe.StripeError as exc:
            logger.error(f"Stripe customer creation failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not initialise payment. Please try again.",
            )

    # Build PaymentIntent kwargs
    pi_kwargs: dict[str, Any] = {
        "amount": amount_units,
        "currency": "kes",             # KES — zero-decimal currency in Stripe
        "customer": stripe_customer_id,
        "payment_method_types": [payment_method_type],
        "metadata": {
            "makao_host_id": str(host_id),
            "package_type": package_type,
            "listing_id": str(listing_id) if listing_id else "",
        },
        "description": f"Makao — {package_type} ({host['email']})",
    }

    try:
        intent = stripe.PaymentIntent.create(**pi_kwargs)
    except stripe.StripeError as exc:
        logger.error(f"Stripe PaymentIntent creation failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment initialisation failed. Please try again.",
        )

    # Create pending payment record in DB (transaction_id = Stripe PI id)
    payment = await db.create_payment(
        host_id=host_id,
        payment_method=f"stripe_{payment_method_type}",
        amount=amount_kes,
        package_type=package_type,
        transaction_id=intent.id,
    )

    # Pre-create inactive booster record if applicable
    if package_type in BOOSTERS and listing_id:
        await db.create_booster(
            listing_id=listing_id,
            booster_type=package_type,
            payment_id=payment["id"],
            end_date=None,
            is_active=False,
        )

    logger.info(
        f"Stripe PaymentIntent created — host={host_id}, "
        f"pi={intent.id}, package={package_type}"
    )

    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "amount": amount_kes,
        "currency": "KES",
    }


async def handle_stripe_webhook(raw_body: bytes, stripe_signature: str) -> OKResponse:
    """
    Process a Stripe webhook event.

    Stripe sends signed events to /api/payments/stripe/webhook.
    The signature is verified using STRIPE_WEBHOOK_SECRET from .env.

    We handle:
      - payment_intent.succeeded  → activate listing / booster
      - payment_intent.payment_failed → mark payment failed

    CRITICAL: Signature verification MUST happen before any processing.
    Never trust webhook payload without verifying the Stripe-Signature header.
    """
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook secret not configured.",
        )

    # ── Verify Stripe signature ────────────────────────────────────────────
    try:
        event = stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.errors.SignatureVerificationError as exc:
        logger.warning(f"Stripe webhook signature verification failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )
    except Exception as exc:
        logger.error(f"Stripe webhook parse error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not parse webhook payload.",
        )

    event_type: str = event["type"]
    event_data: dict = event["data"]["object"]

    logger.info(f"Stripe webhook received: {event_type} | id={event.get('id')}")

    # ── Handle payment_intent.succeeded ───────────────────────────────────
    if event_type == "payment_intent.succeeded":
        pi_id: str = event_data.get("id", "")
        updated = await db.update_payment_status(pi_id, "completed")
        if updated:
            await _activate_after_payment(updated)
            logger.info(f"Stripe payment confirmed and listing activated: pi={pi_id}")
        else:
            logger.warning(f"Stripe webhook: no payment record found for pi={pi_id}")

    # ── Handle payment_intent.payment_failed ──────────────────────────────
    elif event_type == "payment_intent.payment_failed":
        pi_id = event_data.get("id", "")
        await db.update_payment_status(pi_id, "failed")
        logger.warning(f"Stripe payment failed: pi={pi_id}")

    # All other event types are acknowledged but not acted on
    return OKResponse(message=f"Webhook event '{event_type}' received.")


# ===========================================================================
# ── BOOSTER PURCHASE FLOW ────────────────────────────────────────────────────
# ===========================================================================

async def purchase_booster(
    host_id: int,
    data: BoosterPurchaseRequest,
) -> dict:
    """
    Unified booster purchase entry point — delegates to M-Pesa or Stripe.

    Validates:
      - The listing exists and belongs to this host
      - The booster type is valid (checked by Pydantic model pattern)

    Returns the appropriate initiation response for the chosen payment method.
    """
    # Confirm the listing belongs to this host
    listing_rows = await db.get_listings_by_host(host_id)
    listing_ids = {l["id"] for l in listing_rows}
    if data.listing_id not in listing_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Listing not found or does not belong to your account.",
        )

    if data.payment_method == "mpesa":
        return (await initiate_mpesa_payment(
            host_id=host_id,
            phone=data.phone,
            package_type=data.booster_type,
            listing_id=data.listing_id,
        )).model_dump()

    elif data.payment_method in ("stripe_card", "stripe_paypal"):
        method_type = "card" if data.payment_method == "stripe_card" else "paypal"
        return await create_stripe_payment_intent(
            host_id=host_id,
            package_type=data.booster_type,
            payment_method_type=method_type,
            listing_id=data.listing_id,
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported payment method: {data.payment_method}",
    )


# ===========================================================================
# ── FASTAPI ROUTER ────────────────────────────────────────────────────────────
# ===========================================================================

router = APIRouter()


# ---------------------------------------------------------------------------
# M-Pesa endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/mpesa/initiate",
    response_model=MpesaInitiateResponse,
    summary="Initiate M-Pesa STK Push",
    description=(
        "Sends an STK push to the host's M-Pesa phone number. "
        "Returns a checkout_request_id for status polling. "
        "Listing activates only after Daraja callback confirms payment."
    ),
)
async def mpesa_initiate(
    body: MpesaPaymentRequest,
    host: UserResponse = Depends(get_current_host),
) -> MpesaInitiateResponse:
    return await initiate_mpesa_payment(
        host_id=host.id,
        phone=body.phone,
        package_type=body.package_type,
        listing_id=body.listing_id,
    )


@router.post(
    "/mpesa/callback",
    response_model=OKResponse,
    summary="M-Pesa Daraja callback (Safaricom → Makao)",
    description=(
        "Receives payment confirmation from Safaricom Daraja. "
        "This endpoint is public — Safaricom posts here directly. "
        "Always returns 200 to prevent Safaricom retries."
    ),
    include_in_schema=False,   # hide from public API docs
)
async def mpesa_callback(request: Request) -> OKResponse:
    """
    Safaricom calls this URL after the customer enters their M-Pesa PIN.
    We always return 200 — Safaricom retries on any other status code.
    """
    try:
        payload = await request.json()
    except Exception:
        # Still return 200 to prevent endless Safaricom retries
        logger.error("Could not parse M-Pesa callback body.")
        return OKResponse(message="Callback received.")

    return await handle_mpesa_callback(payload)


@router.get(
    "/mpesa/status/{checkout_request_id}",
    response_model=PaymentStatusResponse,
    summary="Poll M-Pesa payment status",
    description=(
        "Frontend polls this endpoint after STK push to check if the "
        "customer has completed payment. Returns: pending | completed | failed."
    ),
)
async def mpesa_status(
    checkout_request_id: str,
    host: UserResponse = Depends(get_current_host),
) -> PaymentStatusResponse:
    return await get_mpesa_payment_status(checkout_request_id, host.id)


# ---------------------------------------------------------------------------
# Stripe endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/stripe/create-intent",
    summary="Create Stripe PaymentIntent (card or PayPal)",
    description=(
        "Creates a Stripe PaymentIntent and returns the client_secret "
        "for Stripe.js to complete the payment on the frontend. "
        "Listing activates only after Stripe webhook confirms payment."
    ),
)
async def stripe_create_intent(
    body: StripePaymentRequest,
    host: UserResponse = Depends(get_current_host),
) -> dict:
    return await create_stripe_payment_intent(
        host_id=host.id,
        package_type=body.package_type,
        payment_method_type=body.payment_method_type,
        listing_id=body.listing_id,
    )


@router.post(
    "/stripe/webhook",
    response_model=OKResponse,
    summary="Stripe webhook receiver",
    description=(
        "Stripe posts signed events here after payment success or failure. "
        "Signature is verified using STRIPE_WEBHOOK_SECRET from .env. "
        "This endpoint is public — verification happens inside the handler."
    ),
    include_in_schema=False,   # hide from public API docs
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(alias="stripe-signature", default=""),
) -> OKResponse:
    """
    IMPORTANT: We must read the raw bytes here — NOT request.json().
    Stripe's signature verification requires the exact raw body bytes.
    Parsing JSON first would break the HMAC check.
    """
    raw_body = await request.body()
    return await handle_stripe_webhook(raw_body, stripe_signature)


# ---------------------------------------------------------------------------
# Booster purchase endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/boosters/purchase",
    summary="Purchase a listing booster",
    description=(
        "Host selects a booster (homepage_feature, chatbot_priority, "
        "refresh_listing, extend_visibility) and pays via M-Pesa or Stripe. "
        "Returns the payment initiation response for the chosen method."
    ),
)
async def buy_booster(
    body: BoosterPurchaseRequest,
    host: UserResponse = Depends(get_current_host),
) -> dict:
    return await purchase_booster(host_id=host.id, data=body)


# ---------------------------------------------------------------------------
# Billing history endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/history",
    response_model=list[PaymentResponse],
    summary="Host billing history",
    description="Returns all payment records for the authenticated host, newest first.",
)
async def payment_history(
    host: UserResponse = Depends(get_current_host),
) -> list[PaymentResponse]:
    rows = await db.get_payments_by_host(host.id)
    return [_row_to_payment_response(r) for r in rows]