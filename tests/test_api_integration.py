import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with docker compose services running",
)


def post(client: httpx.Client, name: str, body: dict) -> dict:
    response = client.post(f"/{name}", json=body)
    response.raise_for_status()
    return response.json()


def test_seeded_booking_flow_is_idempotent_and_cancellable() -> None:
    with httpx.Client(
        base_url=os.environ.get("TOOLS_API_URL", "http://127.0.0.1:18090"),
        timeout=5,
        trust_env=False,
    ) as client:
        for attempt in range(20):
            try:
                if client.get("/health").json() == {"ok": True}:
                    break
            except httpx.HTTPError:
                if attempt == 19:
                    raise
                time.sleep(0.25)
        else:
            pytest.fail("tools API did not become healthy")
        rider = post(client, "lookup_rider_by_phone", {"phone": "+14155550101"})
        assert rider["first_name"] == "Dana"
        assert rider["status"] == "active"

        pickup = post(
            client,
            "geocode_address",
            {"query": "Hilton Union Square", "market": "US-SF"},
        )["candidates"][0]
        dropoff = post(
            client,
            "geocode_address",
            {"query": "SFO international", "market": "US-SF"},
        )["candidates"][0]
        quote = post(
            client,
            "get_ride_options",
            {
                "pickup_place_id": pickup["place_id"],
                "dropoff_place_id": dropoff["place_id"],
                "rider_id": rider["rider_id"],
                "accessibility_needs": [],
            },
        )
        uberx = next(o for o in quote["options"] if o["product_id"] == "uberx")
        assert uberx["fare_low"] > 0
        assert uberx["fare_high"] >= uberx["fare_low"]

        post(client, "send_otp", {"phone": "+14155550101"})
        otp = post(
            client,
            "verify_otp",
            {"phone": "+14155550101", "code": "638204"},
        )
        assert otp["verified"] is True

        key = f"integration-{uuid.uuid4()}"
        booking_payload = {
            "rider_id": rider["rider_id"],
            "pickup_place_id": pickup["place_id"],
            "dropoff_place_id": dropoff["place_id"],
            "product_id": uberx["product_id"],
            "payment_method": "saved_card:pm_dana_visa",
            "quoted_fare_low": uberx["fare_low"],
            "quoted_fare_high": uberx["fare_high"],
            "idempotency_key": key,
        }
        first = post(client, "book_ride", booking_payload)
        second = post(client, "book_ride", booking_payload)
        assert first["booking_ref"] == second["booking_ref"]

        cancellation = post(
            client,
            "get_cancellation_quote",
            {"booking_ref": first["booking_ref"]},
        )
        assert cancellation["cancellation_fee"] == 5.0
        cancelled = post(
            client,
            "cancel_ride",
            {"booking_ref": first["booking_ref"], "reason": "integration test"},
        )
        assert cancelled["cancelled"] is True

        guest_phone = f"+1999{uuid.uuid4().int % 10_000_000:07d}"
        link = post(
            client,
            "send_payment_link_sms",
            {"phone": guest_phone, "amount_estimate": 25},
        )
        assert link["status"] == "pending"
        assert (
            post(client, "get_payment_link_status", {"phone": guest_phone})["status"]
            == "pending"
        )
        post(client, "demo/complete_payment_link", {"phone": guest_phone})
        assert (
            post(client, "get_payment_link_status", {"phone": guest_phone})["status"]
            == "ready"
        )
