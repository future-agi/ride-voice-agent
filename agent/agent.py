from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

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
from uber_voice_agent.config import build_deepgram_stt, google_llm_kwargs
from uber_voice_agent.ride_agent import RideBookingAgent
from uber_voice_agent.state import BookingState
from uber_voice_agent.tools_client import ToolsClient

load_dotenv(".env.local")
logger = logging.getLogger("uber-voice-agent")

_LOCAL_STATE_TOOLS = {
    "confirm_address",
    "select_ride_option",
    "select_payment_method",
    "prepare_booking_confirmation",
}


async def load_caller_context(client: ToolsClient, state: BookingState) -> dict:
    identity = await client.call("lookup_rider_by_phone", phone=state.caller_ani)
    state.set_identity(identity)
    context = {**identity, "caller_ani": state.caller_ani}
    if not state.rider_id:
        return context

    # The generated world owns scenario state. Eagerly fetching places and
    # payment methods here would be recorded as agent actions before the caller
    # has asked for anything, so hydrate only identity and let conversational
    # tools retrieve the rest when they are actually needed.
    if client.harness_mode:
        return context

    places = await client.call("get_saved_places", rider_id=state.rider_id)
    payments = await client.call("get_payment_methods", rider_id=state.rider_id)
    state.payment_methods = payments.get("methods", [])
    state.uber_cash_balance = float(payments.get("uber_cash_balance", 0))
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
        uber_cash_summary=f"{state.uber_cash_balance:.2f}",
        cash_supported=state.cash_supported,
    )
    return context


server = AgentServer()


def build_audio_input_options() -> room_io.AudioInputOptions:
    """Use AI-coustics when available, with an opt-out for test projects."""
    if os.environ.get("DISABLE_AI_COUSTICS", "").lower() in {"1", "true", "yes"}:
        return room_io.AudioInputOptions()
    return room_io.AudioInputOptions(
        noise_cancellation=ai_coustics.audio_enhancement(
            model=ai_coustics.EnhancerModel.QUAIL_VF_S
        )
    )


def enable_harness_local_tool_trace(session: AgentSession) -> None:
    """Trace state-only tools; HTTP-backed tools are traced by ToolsClient."""
    destination = os.environ.get("HARNESS_TOOL_TRACE", "").strip()
    if not destination:
        return
    path = Path(destination)

    def record(event) -> None:
        records = []
        for call, output in event.zipped():
            if call.name not in _LOCAL_STATE_TOOLS:
                continue
            records.append(
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "output": output.output if output is not None else "",
                    "is_error": bool(output and output.is_error),
                }
            )
        if records:
            try:
                with path.open("a", encoding="utf-8") as trace:
                    for one in records:
                        trace.write(json.dumps(one, default=str) + "\n")
            except OSError:
                # Observability is best-effort and must never affect the call under test.
                return

    session.on("function_tools_executed", record)


@server.rtc_session(
    agent_name=os.environ.get("LIVEKIT_AGENT_NAME", "uber-voice-booking")
)
async def entrypoint(ctx: JobContext) -> None:
    identity_prefix = os.environ.get("HARNESS_CALLER_IDENTITY_PREFIX", "").strip()
    if identity_prefix:
        await ctx.connect()
        deadline = asyncio.get_running_loop().time() + 60
        participant = None
        while asyncio.get_running_loop().time() < deadline:
            participant = next(
                (
                    one
                    for one in ctx.room.remote_participants.values()
                    if str(one.identity).startswith(identity_prefix)
                ),
                None,
            )
            if participant is not None:
                break
            await asyncio.sleep(0.1)
        if participant is None:
            raise RuntimeError(
                f"caller participant with prefix {identity_prefix!r} did not join"
            )
    else:
        participant = await ctx.wait_for_participant()
    is_sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    # LiveKit may publish custom participant attributes a fraction after the join
    # event. The simulator also puts the caller number in participant metadata so
    # a harness call never silently falls back to the demo rider.
    caller_ani = None
    metadata: dict = {}
    for _ in range(20):
        caller_ani = participant.attributes.get("sip.phoneNumber")
        caller_ani = caller_ani or participant.attributes.get("harness.callerPhone")
        try:
            metadata = json.loads(participant.metadata or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        caller_ani = caller_ani or metadata.get("caller_phone")
        identity_match = re.match(
            r"^fagi-simulator-phone-(\d+)-", str(participant.identity)
        )
        if not caller_ani and identity_match:
            caller_ani = "+" + identity_match.group(1)
        if caller_ani:
            break
        await asyncio.sleep(0.1)
    caller_ani = caller_ani or os.environ.get("DEMO_CALLER_ANI", "+14155550101")
    logger.info(
        "resolved caller context",
        extra={
            "participant_identity": participant.identity,
            "caller_ani": caller_ani,
            "used_harness_identity": bool(
                participant.attributes.get("harness.callerPhone")
                or metadata.get("caller_phone")
            ),
        },
    )
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
    # Context hydration is setup, not part of the scenario. Trace from the first
    # conversational action onward, including deterministic account handoffs.
    client.enable_trace()

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
            interruption={
                "enabled": os.environ.get("AGENT_ALLOW_INTERRUPTION", "1").lower()
                not in {"0", "false", "no"},
                "discard_audio_if_uninterruptible": True,
            },
            preemptive_generation={
                "enabled": os.environ.get("AGENT_PREEMPTIVE_GENERATION", "1").lower()
                not in {"0", "false", "no"}
            },
        ),
        max_tool_steps=int(os.environ.get("AGENT_MAX_TOOL_STEPS", "12")),
        vad=silero.VAD.load(),
    )
    enable_harness_local_tool_trace(session)
    await session.start(
        agent=RideBookingAgent(state, client, context),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=build_audio_input_options(),
            participant_identity=participant.identity,
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
