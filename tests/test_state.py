import pytest
from ride_voice_agent.state import BookingState, GuardError


def ready_state() -> BookingState:
    state = BookingState(caller_ani="+14155550101", session_id="room-1")
    state.set_identity(
        {
            "rider_id": "rdr_dana",
            "first_name": "Dana",
            "status": "active",
            "default_market": "US-SF",
            "accessibility_needs": [],
            "cash_supported_in_market": False,
        }
    )
    state.remember_geocode(
        "pickup",
        [{"place_id": "pickup", "formatted_address": "One Main Street"}],
    )
    state.confirm_address("pickup", "pickup")
    state.remember_geocode(
        "dropoff",
        [{"place_id": "dropoff", "formatted_address": "SFO Terminal One"}],
    )
    state.confirm_address("dropoff", "dropoff")
    state.remember_quote(
        {
            "currency": "USD",
            "surge_multiplier": 1.0,
            "options": [
                {
                    "product_id": "ridex",
                    "display_name": "RideX",
                    "fare_low": 18.0,
                    "fare_high": 22.0,
                    "eta_pickup_min": 4,
                    "is_available": True,
                }
            ],
        }
    )
    state.select_product("ridex")
    return state


def test_booking_requires_confirmed_addresses() -> None:
    state = BookingState(caller_ani="+1", session_id="room")
    with pytest.raises(GuardError, match="pickup and destination"):
        state.remember_quote({"options": []})


def test_saved_card_requires_otp() -> None:
    state = ready_state()
    state.payment_methods = [
        {
            "id": "pm_visa",
            "type": "card",
            "is_valid": True,
            "is_expired": False,
        }
    ]
    with pytest.raises(GuardError, match="OTP"):
        state.select_payment("saved_card:pm_visa")

    state.auth_level = "otp_verified"
    state.select_payment("saved_card:pm_visa")
    assert state.payment_method_selected == "saved_card:pm_visa"


def test_ride_cash_must_cover_high_quote() -> None:
    state = ready_state()
    state.ride_cash_balance = 21.99
    with pytest.raises(GuardError, match="does not cover"):
        state.select_payment("ride_cash")


def test_payment_link_must_be_completed() -> None:
    state = ready_state()
    with pytest.raises(GuardError, match="still pending"):
        state.select_payment("pay_link")
    state.payment_link_ready = True
    state.select_payment("pay_link")
    assert state.payment_method_selected == "pay_link"


def test_destination_change_invalidates_quote_payment_and_consent() -> None:
    state = ready_state()
    state.ride_cash_balance = 100
    state.select_payment("ride_cash")
    token, _ = state.prepare_confirmation()

    state.remember_geocode(
        "dropoff",
        [{"place_id": "new-dropoff", "formatted_address": "Oakland Airport"}],
    )
    state.confirm_address("dropoff", "new-dropoff")

    assert state.selected_product_id is None
    assert state.payment_method_selected is None
    with pytest.raises(GuardError, match="confirmation"):
        state.authorize_booking(token, caller_explicitly_confirmed=True)


def test_confirmation_token_is_exact_and_one_time() -> None:
    state = ready_state()
    state.ride_cash_balance = 100
    state.select_payment("ride_cash")
    token, summary = state.prepare_confirmation()
    assert "RideX" in summary
    assert "18.00 to 22.00 USD" in summary

    with pytest.raises(GuardError, match="explicit yes"):
        state.authorize_booking(token, caller_explicitly_confirmed=False)
    with pytest.raises(GuardError, match="does not match"):
        state.authorize_booking("wrong", caller_explicitly_confirmed=True)

    state.authorize_booking(token, caller_explicitly_confirmed=True)
    with pytest.raises(GuardError, match="confirmation"):
        state.authorize_booking(token, caller_explicitly_confirmed=True)


def test_cash_respects_market_and_guest_cap() -> None:
    state = ready_state()
    state.cash_supported = True
    state.max_fare_without_otp = 20
    with pytest.raises(GuardError, match="fare cap"):
        state.select_payment("cash")

    state.max_fare_without_otp = 30
    state.select_payment("cash")
    assert state.payment_method_selected == "cash"
