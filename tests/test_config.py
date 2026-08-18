from livekit.plugins import deepgram
from ride_voice_agent.config import build_deepgram_stt


def test_flux_uses_deepgram_v2() -> None:
    assert isinstance(build_deepgram_stt("test-key", "flux-general-en"), deepgram.STTv2)


def test_phone_nova_uses_deepgram_v1() -> None:
    assert isinstance(build_deepgram_stt("test-key", "nova-2-phonecall"), deepgram.STT)
