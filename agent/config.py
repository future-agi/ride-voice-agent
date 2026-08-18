from __future__ import annotations

import os

from livekit.plugins import deepgram


def build_deepgram_stt(api_key: str, model: str):
    """Flux uses Deepgram's v2 API; Nova models use the v1 API."""
    if model.startswith("flux-"):
        return deepgram.STTv2(api_key=api_key, model=model)
    return deepgram.STT(api_key=api_key, model=model)


def google_llm_kwargs() -> dict:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if project and credentials:
        return {
            "vertexai": True,
            "project": project,
            "location": os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        }
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return {"vertexai": False, "api_key": api_key}
    raise ValueError(
        "Set GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_CLOUD_PROJECT for Vertex, "
        "or set GEMINI_API_KEY for Gemini."
    )
