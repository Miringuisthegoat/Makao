"""
Phase 6 end-to-end test against a REAL PostgreSQL (migrated to head).
Run from Backend/:  pytest tests/integration_phase6.py -v
Skips automatically when the DB is unreachable. Uses its own throwaway rows.
"""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import actions_rules as rules
import auth as auth_logic
import database as db
import renter_actions
from renter_actions import router

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    try:
        pool = await db.get_pool()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"database unreachable: {exc}")
    tag = uuid.uuid4().hex[:8]
    async with pool.acquire() as c:
        host = await c.fetchrow(
            "INSERT INTO users (full_name,email,password_hash,phone,account_type,host_type) "
            "VALUES ($1,$2,$3,'+254711000111','landlord','landlord') RETURNING id",
            f"Host {tag}", f"host_{tag}@example.com", auth_logic.hash_password("hostpass123"))
    loc = await db.resolve_or_create_location(
        {"county": "Nairobi", "neighbourhood": "Kilimani", "latitude": -1.29, "longitude": 36.78})
    prop = await db.create_property({
        "property_type": "1_bedroom", "title": f"Test 1BR {tag}", "location_id": loc,
        "bedrooms": 1, "bathrooms": 1, "amenities": ["security", "borehole"], "photos": []})
    lid = await db.create_listing_row({"property_id": prop, "host_id": host["id"], "asking_price": 25000,
                                       "service_charge": 2000})
    await db.create_availability_confirmation(lid, host["id"], "available")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield {"c": client, "lid": lid, "prop": prop, "host_id": host["id"], "tag": tag,
               "host_email": f"host_{tag}@example.com"}
    await pool.close()
    db._pool = None


def future(hours=48):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


async def test_full_renter_journey(env):
    c, lid, prop, tag = env["c"], env["lid"], env["prop"], env["tag"]
    renter = {"full_name": "Amina W", "email": f"r_{tag}@example.com", "password": "renterpass1", "phone": "0712345678"}

    # signup + duplicate + login + me
    r = await c.post("/api/renter/signup", json=renter); assert r.status_code == 201, r.text
    assert r.json()["user"]["account_type"] == "renter"
    assert (await c.post("/api/renter/signup", json=renter)).status_code == 409
    assert (await c.post("/api/renter/login", json={"email": renter["email"], "password": "nope"})).status_code == 401
    tok = (await c.post("/api/renter/login", json={"email": renter["email"], "password": renter["password"]})).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    assert (await c.get("/api/renter/me", headers=H)).json()["account_type"] == "renter"
    assert (await c.get("/api/renter/me")).status_code == 401

    # favorites
    assert (await c.post("/api/favorites", json={"property_id": prop}, headers=H)).status_code == 201
    assert any(f["property_id"] == prop for f in (await c.get("/api/favorites", headers=H)).json())
    assert (await c.delete(f"/api/favorites/{prop}", headers=H)).status_code == 200
    assert (await c.post("/api/favorites", json={"property_id": prop})).status_code == 401

    # fit (score + explanation, AI off)
    fit = await c.post(f"/api/listings/{lid}/fit", json={
        "preferences": {"rent_budget": 25000, "bedrooms": 1, "preferred_property_type": "1_bedroom"},
        "estimated_monthly_cost": 33000, "commute_minutes": 40, "use_ai": False})
    assert fit.status_code == 200, fit.text
    body = fit.json()
    assert 0 <= body["score"]["overall_score"] <= 100 and body["explanation"]["reasons"]
    assert body["explanation"]["source"] == "deterministic"

    # viewing: validation, create, duplicate, host sees it, host confirms, renter cancels
    assert (await c.post(f"/api/listings/{lid}/viewing-requests", json={"requested_time": future(0)}, headers=H)).status_code == 422
    v = await c.post(f"/api/listings/{lid}/viewing-requests", json={"requested_time": future(), "message": "Evening?"}, headers=H)
    assert v.status_code == 201, v.text
    vid = v.json()["id"]
    assert (await c.post(f"/api/listings/{lid}/viewing-requests", json={"requested_time": future(72)}, headers=H)).status_code == 409
    assert [x["id"] for x in (await c.get("/api/viewing-requests/mine", headers=H)).json()] == [vid]

    host_tok = (await c.post("/api/renter/login", json={"email": env["host_email"], "password": "hostpass123"})).json()["access_token"]
    HH = {"Authorization": f"Bearer {host_tok}"}
    hv = await c.get("/api/host/viewing-requests", headers=HH)
    assert [x["id"] for x in hv.json()] == [vid] and hv.json()[0]["renter_name"] == "Amina W"
    assert (await c.get("/api/host/viewing-requests", headers=H)).json() == []          # renter is not the host
    assert (await c.patch(f"/api/host/viewing-requests/{vid}", json={"status": "confirmed"}, headers=H)).status_code == 404
    assert (await c.patch(f"/api/host/viewing-requests/{vid}", json={"status": "completed"}, headers=HH)).status_code == 409
    ok = await c.patch(f"/api/host/viewing-requests/{vid}", json={"status": "confirmed", "host_note": "See you"}, headers=HH)
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    assert (await c.patch(f"/api/viewing-requests/{vid}/cancel", headers=H)).json()["status"] == "cancelled"
    assert (await c.patch(f"/api/viewing-requests/{vid}/cancel", headers=H)).status_code == 409
    # host cannot request own listing
    assert (await c.post(f"/api/listings/{lid}/viewing-requests", json={"requested_time": future()}, headers=HH)).status_code == 400

    # contact: anonymous message, validation, call/whatsapp links, host leads
    assert (await c.post(f"/api/listings/{lid}/contact", json={"channel": "message", "name": "X"})).status_code == 422
    m = await c.post(f"/api/listings/{lid}/contact", json={"name": "Anon", "phone": "0722000111", "message": "Hi"})
    assert m.status_code == 200 and "tel_url" not in m.json()
    w = await c.post(f"/api/listings/{lid}/contact", json={"channel": "whatsapp"})
    assert w.json()["whatsapp_url"].startswith("https://wa.me/254711000111")
    leads = (await c.get("/api/host/leads", headers=HH)).json()
    assert {l["channel"] for l in leads} >= {"message", "whatsapp"}

    # share
    s = (await c.get(f"/api/listings/{lid}/share")).json()
    assert f"listing.html?id={lid}" in s["url"] and "wa.me" in s["whatsapp_url"]

    # inactive listing blocks viewing/contact (no payment involved anywhere)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE listings SET status='inactive' WHERE id=$1", lid)
    assert (await c.post(f"/api/listings/{lid}/contact", json={"channel": "call"})).status_code == 409
    assert (await c.get(f"/api/listings/999999/share")).status_code == 404
