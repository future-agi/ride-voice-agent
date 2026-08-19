"""Tools API — the ONLY source of fares, ETAs, availability and records.

Every endpoint mirrors a tool schema from §4 of the prompt package. The agent
calls these; it never computes a fare or invents an ETA itself. That separation
is what makes the "no hallucinated fare" eval checkable: any number the agent
utters must trace back to a response from here.
"""

from __future__ import annotations

import math
import os
import random
import uuid
from datetime import UTC, datetime

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

DSN = os.environ.get("DATABASE_URL", "postgresql://uber:uber@postgres:5432/uber_demo")
app = FastAPI(title="Uber voice agent tools", version="1.0.0")

DRIVERS = [
    ("Amir", "white Toyota Camry", "8XYZ123"),
    ("Lena", "silver Honda Accord", "7KTR904"),
    ("Diego", "black Chevy Malibu", "5FGH221"),
    ("Nia", "grey Nissan Altima", "9PLM447"),
]


def db():
    return psycopg.connect(DSN, row_factory=dict_row)


def one(sql: str, params: tuple = ()) -> dict | None:
    with db() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def many(sql: str, params: tuple = ()) -> list[dict]:
    with db() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run(sql: str, params: tuple = ()) -> None:
    with db() as c, c.cursor() as cur:
        cur.execute(sql, params)
        c.commit()


def _f(v) -> float | None:
    return None if v is None else float(v)


# ---------------------------------------------------------------- identity


class PhoneIn(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class OtpIn(BaseModel):
    phone: str
    code: str


@app.get("/health")
def health() -> dict:
    try:
        one("SELECT 1 AS ok")
        return {"ok": True}
    except Exception as exc:  # surfaced so the agent can say "having trouble"
        raise HTTPException(503, "db_unavailable") from exc


@app.post("/lookup_rider_by_phone")
def lookup_rider_by_phone(body: PhoneIn) -> dict:
    u = one("SELECT * FROM users WHERE phone = %s", (body.phone,))
    if not u:
        return {"rider_id": None, "first_name": None, "status": "unknown"}
    mk = one("SELECT * FROM market_config WHERE market = %s", (u["default_market"],))
    return {
        "rider_id": u["rider_id"],
        "first_name": u["first_name"],
        "status": u["status"],
        "phone_verified": u["phone_verified"],
        "rating": _f(u["rating"]),
        "default_market": u["default_market"],
        "preferred_language": u["preferred_language"],
        "business_profile_id": u["business_profile_id"],
        "accessibility_needs": u["accessibility_needs"] or [],
        "cash_supported_in_market": bool(mk and mk["cash_supported"]),
    }


@app.post("/send_otp")
def send_otp(body: PhoneIn) -> dict:
    exists = one("SELECT 1 FROM otp_codes WHERE phone = %s", (body.phone,))
    if exists:
        run(
            "UPDATE otp_codes SET attempts_left = 3, verified = FALSE,"
            " issued_at = now() WHERE phone = %s",
            (body.phone,),
        )
    else:
        run(
            "INSERT INTO otp_codes (phone, code) VALUES (%s, '123456')",
            (body.phone,),
        )
    return {"otp_sent": True, "channel": "sms"}


@app.post("/verify_otp")
def verify_otp(body: OtpIn) -> dict:
    row = one("SELECT * FROM otp_codes WHERE phone = %s", (body.phone,))
    if not row:
        return {"verified": False, "attempts_left": 0}
    if row["attempts_left"] <= 0:
        return {"verified": False, "attempts_left": 0}
    if body.code.strip().replace(" ", "") == row["code"]:
        run("UPDATE otp_codes SET verified = TRUE WHERE phone = %s", (body.phone,))
        return {"verified": True, "attempts_left": row["attempts_left"]}
    left = row["attempts_left"] - 1
    run("UPDATE otp_codes SET attempts_left = %s WHERE phone = %s", (left, body.phone))
    return {"verified": False, "attempts_left": left}


# ------------------------------------------------------------------ places


class RiderIn(BaseModel):
    rider_id: str
    limit: int = Field(default=3, ge=1, le=10)


@app.post("/get_saved_places")
def get_saved_places(body: RiderIn) -> dict:
    rows = many(
        "SELECT label, place_id, formatted_address, lat, lng FROM saved_places"
        " WHERE rider_id = %s ORDER BY label",
        (body.rider_id,),
    )
    for r in rows:
        r["lat"], r["lng"] = _f(r["lat"]), _f(r["lng"])
    return {"places": rows}


@app.post("/get_recent_dropoffs")
def get_recent_dropoffs(body: RiderIn) -> dict:
    rows = many(
        "SELECT dropoff_place_id AS place_id, dropoff_formatted_address AS"
        " formatted_address, taken_at FROM trips WHERE rider_id = %s"
        " ORDER BY taken_at DESC LIMIT %s",
        (body.rider_id, body.limit),
    )
    for r in rows:
        r["when"] = r.pop("taken_at").isoformat()
    return {"trips": rows}


class GeocodeIn(BaseModel):
    query: str
    market: str | None = None


@app.post("/geocode_address")
def geocode_address(body: GeocodeIn) -> dict:
    """Return ranked candidates. Deliberately returns >1 for ambiguous input
    ("Main Street") so the agent has to disambiguate rather than assume."""
    q = body.query.strip().lower()
    rows = many("SELECT * FROM places")
    scored: list[tuple[float, dict]] = []
    for r in rows:
        addr = r["formatted_address"].lower()
        aliases = [a.lower() for a in (r["aliases"] or [])]
        score = 0.0
        if q == addr:
            score = 1.0
        elif q in addr:
            score = 0.9
        elif any(q == a for a in aliases):
            score = 0.88
        elif any(q in a or a in q for a in aliases):
            score = 0.75
        else:
            tokens = [t for t in q.split() if len(t) > 2]
            hits = sum(1 for t in tokens if t in addr or any(t in a for a in aliases))
            if tokens and hits:
                score = 0.4 + 0.4 * (hits / len(tokens))
        if score > 0:
            if body.market and r["market"] == body.market:
                score += 0.05
            scored.append(
                (
                    score,
                    {
                        "place_id": r["place_id"],
                        "formatted_address": r["formatted_address"],
                        "city": r["city"],
                        "lat": _f(r["lat"]),
                        "lng": _f(r["lng"]),
                        "confidence": round(min(score, 0.99), 2),
                    },
                )
            )
    scored.sort(key=lambda s: -s[0])
    return {"candidates": [c for _, c in scored[:3]]}


# --------------------------------------------------------- pricing & options


def _miles(a: dict, b: dict) -> float:
    """Haversine — distance drives the fare, so it is computed, not guessed."""
    lat1, lon1, lat2, lon2 = map(math.radians, [a["lat"], a["lng"], b["lat"], b["lng"]])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class RideOptionsIn(BaseModel):
    pickup_place_id: str
    dropoff_place_id: str
    rider_id: str | None = None
    accessibility_needs: list[str] | None = None


@app.post("/get_ride_options")
def get_ride_options(body: RideOptionsIn) -> dict:
    p = one("SELECT * FROM places WHERE place_id = %s", (body.pickup_place_id,))
    d = one("SELECT * FROM places WHERE place_id = %s", (body.dropoff_place_id,))
    if not p or not d:
        raise HTTPException(404, "unknown_place_id")
    pk = {"lat": _f(p["lat"]), "lng": _f(p["lng"])}
    dp = {"lat": _f(d["lat"]), "lng": _f(d["lng"])}
    miles = round(_miles(pk, dp), 2)
    minutes = max(6, round(miles * 2.6))

    mk = one("SELECT * FROM market_config WHERE market = %s", (p["market"],))
    if not mk:
        raise HTTPException(404, "unknown_market")
    surge = _f(mk["surge_multiplier"]) or 1.0
    allowed = set(mk["available_products"] or [])
    needs = set(body.accessibility_needs or [])

    options = []
    for pr in many("SELECT * FROM products ORDER BY sort_order"):
        if pr["product_id"] not in allowed:
            continue
        if "wav" in needs and not pr["is_wav"]:
            continue
        if "wav" not in needs and pr["is_wav"]:
            continue
        est = (
            _f(pr["base_fare"])
            + miles * _f(pr["per_mile"])
            + minutes * _f(pr["per_minute"])
        ) * surge
        est = max(est, _f(pr["min_fare"]))
        options.append(
            {
                "product_id": pr["product_id"],
                "display_name": pr["display_name"],
                "capacity": pr["capacity"],
                "fare_low": round(est * 0.92, 2),
                "fare_high": round(est * 1.12, 2),
                "eta_pickup_min": random.randint(3, 9),
                "is_available": True,
                "description": pr["description"],
            }
        )
    return {
        "currency": mk["currency"],
        "surge_multiplier": surge,
        "trip_distance_mi": miles,
        "trip_duration_min": minutes,
        "options": options,
    }


# ----------------------------------------------------------------- payment


@app.post("/get_payment_methods")
def get_payment_methods(body: RiderIn) -> dict:
    u = one("SELECT * FROM users WHERE rider_id = %s", (body.rider_id,))
    if not u:
        raise HTTPException(404, "unknown_rider")
    mk = one("SELECT * FROM market_config WHERE market = %s", (u["default_market"],))
    w = one("SELECT * FROM wallets WHERE rider_id = %s", (body.rider_id,))
    methods = many(
        "SELECT id, type, brand, last4, is_default, is_valid, is_expired"
        " FROM payment_methods WHERE rider_id = %s ORDER BY is_default DESC",
        (body.rider_id,),
    )
    return {
        "methods": methods,
        "uber_cash_balance": _f(w["uber_cash_balance"]) if w else 0.0,
        "cash_supported_in_market": bool(mk and mk["cash_supported"]),
    }


class PayLinkIn(BaseModel):
    phone: str
    amount_estimate: float | None = None


@app.post("/send_payment_link_sms")
def send_payment_link_sms(body: PayLinkIn) -> dict:
    # PCI: the agent never touches a PAN; adding a card always goes via a link.
    link_id = f"pay_{uuid.uuid4().hex[:12]}"
    status = (
        "ready" if os.environ.get("PAYMENT_LINK_AUTO_COMPLETE") == "true" else "pending"
    )
    run(
        "INSERT INTO payment_links (id, phone, amount_estimate, status)"
        " VALUES (%s, %s, %s, %s)",
        (link_id, body.phone, body.amount_estimate, status),
    )
    return {"link_sent": True, "payment_link_id": link_id, "status": status}


@app.post("/get_payment_link_status")
def get_payment_link_status(body: PhoneIn) -> dict:
    link = one(
        "SELECT id, status FROM payment_links WHERE phone = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (body.phone,),
    )
    if not link:
        return {"payment_link_id": None, "status": "not_sent"}
    return {"payment_link_id": link["id"], "status": link["status"]}


@app.post("/demo/complete_payment_link")
def complete_payment_link(body: PhoneIn) -> dict:
    """Local demo callback standing in for a payment provider webhook."""
    link = one(
        "SELECT id FROM payment_links WHERE phone = %s ORDER BY created_at DESC LIMIT 1",
        (body.phone,),
    )
    if not link:
        raise HTTPException(404, "payment_link_not_found")
    run("UPDATE payment_links SET status = 'ready' WHERE id = %s", (link["id"],))
    return {"payment_link_id": link["id"], "status": "ready"}


# ----------------------------------------------------------------- account


class GuestIn(BaseModel):
    phone: str
    first_name: str | None = None


@app.post("/create_guest_rider")
def create_guest_rider(body: GuestIn) -> dict:
    rid = f"rdr_guest_{uuid.uuid4().hex[:8]}"
    run(
        "INSERT INTO users (rider_id, phone, first_name, status, phone_verified,"
        " default_market) VALUES (%s, %s, %s, 'active', FALSE, 'US-SF')"
        " ON CONFLICT (phone) DO NOTHING",
        (rid, body.phone, body.first_name or "Guest"),
    )
    u = one("SELECT rider_id FROM users WHERE phone = %s", (body.phone,))
    return {"rider_id": u["rider_id"] if u else rid}


# ----------------------------------------------------------------- booking


class BookIn(BaseModel):
    rider_id: str | None = None
    pickup_place_id: str
    dropoff_place_id: str
    product_id: str
    payment_method: str
    quoted_fare_low: float
    quoted_fare_high: float
    idempotency_key: str | None = None


@app.post("/book_ride")
def book_ride(body: BookIn) -> dict:
    if body.quoted_fare_low < 0 or body.quoted_fare_high < body.quoted_fare_low:
        raise HTTPException(422, "invalid_fare_range")
    if body.idempotency_key:
        existing = one(
            "SELECT * FROM bookings WHERE idempotency_key = %s",
            (body.idempotency_key,),
        )
        if existing:
            return {
                "booking_ref": existing["booking_ref"],
                "status": existing["status"],
                "driver_name": existing["driver_name"],
                "vehicle": existing["vehicle"],
                "plate": existing["plate"],
                "eta_pickup_min": existing["eta_pickup_min"],
            }
    accessibility_needs: list[str] = []
    if body.rider_id:
        u = one(
            "SELECT status, accessibility_needs FROM users WHERE rider_id = %s",
            (body.rider_id,),
        )
        if u and u["status"] != "active":
            return {
                "booking_ref": None,
                "status": "rejected",
                "error_code": f"account_{u['status']}",
            }
        if u:
            accessibility_needs = u["accessibility_needs"] or []
    fresh_quote = get_ride_options(
        RideOptionsIn(
            pickup_place_id=body.pickup_place_id,
            dropoff_place_id=body.dropoff_place_id,
            rider_id=body.rider_id,
            accessibility_needs=accessibility_needs,
        )
    )
    quoted_option = next(
        (o for o in fresh_quote["options"] if o["product_id"] == body.product_id),
        None,
    )
    if not quoted_option or not quoted_option["is_available"]:
        raise HTTPException(409, "product_unavailable")
    if (
        abs(float(quoted_option["fare_low"]) - body.quoted_fare_low) > 0.01
        or abs(float(quoted_option["fare_high"]) - body.quoted_fare_high) > 0.01
    ):
        raise HTTPException(409, "stale_or_invalid_quote")
    ref = f"UB{uuid.uuid4().hex[:8].upper()}"
    name, vehicle, plate = random.choice(DRIVERS)
    eta = random.randint(3, 8)
    run(
        "INSERT INTO bookings (booking_ref, rider_id, pickup_place_id,"
        " dropoff_place_id, product_id, payment_method, quoted_fare_low,"
        " quoted_fare_high, status, driver_name, vehicle, plate, eta_pickup_min,"
        " cancellation_fee, idempotency_key) VALUES"
        " (%s,%s,%s,%s,%s,%s,%s,%s,'matched',%s,%s,%s,%s,5.00,%s)",
        (
            ref,
            body.rider_id,
            body.pickup_place_id,
            body.dropoff_place_id,
            body.product_id,
            body.payment_method,
            body.quoted_fare_low,
            body.quoted_fare_high,
            name,
            vehicle,
            plate,
            eta,
            body.idempotency_key,
        ),
    )
    return {
        "booking_ref": ref,
        "status": "matched",
        "driver_name": name,
        "vehicle": vehicle,
        "plate": plate,
        "eta_pickup_min": eta,
    }


class RefIn(BaseModel):
    booking_ref: str
    reason: str | None = None


@app.post("/get_booking_status")
def get_booking_status(body: RefIn) -> dict:
    b = one("SELECT * FROM bookings WHERE booking_ref = %s", (body.booking_ref,))
    if not b:
        raise HTTPException(404, "unknown_booking_ref")
    return {
        "status": b["status"],
        "driver_name": b["driver_name"],
        "vehicle": b["vehicle"],
        "plate": b["plate"],
        "eta_pickup_min": b["eta_pickup_min"],
    }


@app.post("/cancel_ride")
def cancel_ride(body: RefIn) -> dict:
    b = one("SELECT * FROM bookings WHERE booking_ref = %s", (body.booking_ref,))
    if not b:
        raise HTTPException(404, "unknown_booking_ref")
    run(
        "UPDATE bookings SET status = 'cancelled' WHERE booking_ref = %s",
        (body.booking_ref,),
    )
    return {"cancelled": True, "cancellation_fee": _f(b["cancellation_fee"])}


@app.post("/get_cancellation_quote")
def get_cancellation_quote(body: RefIn) -> dict:
    b = one("SELECT * FROM bookings WHERE booking_ref = %s", (body.booking_ref,))
    if not b:
        raise HTTPException(404, "unknown_booking_ref")
    return {
        "booking_ref": body.booking_ref,
        "status": b["status"],
        "cancellation_fee": _f(b["cancellation_fee"]),
    }


class SmsIn(BaseModel):
    phone: str
    booking_ref: str | None = None


@app.post("/send_confirmation_sms")
def send_confirmation_sms(body: SmsIn) -> dict:
    return {"sent": True}


class TransferIn(BaseModel):
    reason: str


@app.post("/transfer_to_human")
def transfer_to_human(body: TransferIn) -> dict:
    return {
        "transferring": True,
        "reason": body.reason,
        "at": datetime.now(UTC).isoformat(),
    }
