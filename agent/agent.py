from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    room_io,
)
from livekit.plugins import ai_coustics, deepgram, google, silero
from ride_voice_agent.config import build_deepgram_stt, google_llm_kwargs
from ride_voice_agent.ride_agent import RideBookingAgent
from ride_voice_agent.state import BookingState
from ride_voice_agent.tools_client import ToolsClient

load_dotenv(".env.local")
logger = logging.getLogger("ride-voice-agent")


async def load_caller_context(client: ToolsClient, state: BookingState) -> dict:
    identity = await client.call("lookup_rider_by_phone", phone=state.caller_ani)
    state.set_identity(identity)
    context = {**identity, "caller_ani": state.caller_ani}
    if not state.rider_id:
        return context

    places = await client.call("get_saved_places", rider_id=state.rider_id)
    payments = await client.call("get_payment_methods", rider_id=state.rider_id)
    state.payment_methods = payments.get("methods", [])
    state.ride_cash_balance = float(payments.get("ride_cash_balance", 0))
    state.cash_supported = bool(payments.get("cash_supported_in_market"))
    labels = [p.get("label", "place") for p in places.get("places", [])]
    default = next((m for m in state.payment_methods if m.get("is_default")), None)
    context.update(
        saved_places_summary=", ".join(labels) or "none",
        default_payment_summary=(
            f"{default.get('brand', default.get('type', 'card'))} ending "
            f"{default.get('last4', 'unknown')}"
            if default
            else "none"
        ),
        ride_cash_summary=f"{state.ride_cash_balance:.2f}",
        cash_supported=state.cash_supported,
    )
    return context


server = AgentServer()


@server.rtc_session(agent_name="rideco-voice-booking")
async def entrypoint(ctx: JobContext) -> None:
    participant = await ctx.wait_for_participant()
    is_sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    caller_ani = participant.attributes.get("sip.phoneNumber") if is_sip else None
    caller_ani = caller_ani or os.environ.get("DEMO_CALLER_ANI", "+14155550101")
    session_id = ctx.room.name or participant.identity
    state = BookingState(
        caller_ani=caller_ani,
        session_id=session_id,
        max_fare_without_otp=float(os.environ["MAX_FARE_WITHOUT_OTP"])
        if os.environ.get("MAX_FARE_WITHOUT_OTP")
        else None,
    )
    client = ToolsClient(
        os.environ.get("TOOLS_API_URL", "http://localhost:18090"),
        session_id=session_id,
        timeout=float(os.environ.get("TOOLS_TIMEOUT_SECONDS", "5")),
    )
    context = await load_caller_context(client, state)

    deepgram_key = os.environ["DEEPGRAM_API_KEY"]
    stt_model = os.environ.get(
        "AGENT_STT_MODEL_PHONE" if is_sip else "AGENT_STT_MODEL",
        "nova-2-phonecall" if is_sip else "flux-general-en",
    )
    session = AgentSession(
        stt=build_deepgram_stt(deepgram_key, stt_model),
        llm=google.LLM(
            model=os.environ.get("AGENT_LLM_MODEL", "gemini-2.5-flash-lite"),
            temperature=float(os.environ.get("AGENT_LLM_TEMPERATURE", "0.2")),
            **google_llm_kwargs(),
        ),
        tts=deepgram.TTS(
            api_key=deepgram_key,
            model=os.environ.get("AGENT_TTS_MODEL", "aura-2-andromeda-en"),
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection="stt",
            preemptive_generation={"enabled": True},
        ),
        vad=silero.VAD.load(),
    )
    await session.start(
        agent=RideBookingAgent(state, client, context),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                )
            )
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
