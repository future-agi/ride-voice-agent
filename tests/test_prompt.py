from uber_voice_agent.prompt import build_instructions


def test_prompt_contains_non_negotiable_voice_and_booking_rules() -> None:
    prompt = build_instructions(
        {
            "caller_ani": "+14155550101",
            "rider_id": "rdr_dana",
            "first_name": "Dana",
            "status": "active",
            "default_market": "US-SF",
            "cash_supported": False,
        }
    )
    for required in (
        "one question at a time",
        "Never invent",
        "Never ask for or accept a full card number",
        "prepare_booking_confirmation",
        "explicit yes",
        "transfer_to_human",
    ):
        assert required.lower() in prompt.lower()


def test_prompt_does_not_expose_unknown_saved_data() -> None:
    prompt = build_instructions({"caller_ani": "+19995550123"})
    assert "Account on file: no" in prompt
    assert "Saved places: unavailable" in prompt
    assert "Default payment: unavailable" in prompt
