"""The OpenAI Realtime GA dialect: its endpoint, defaults, capabilities, prompter, and session shape."""

from typing import Any

from ..adapter import SAMPLE_RATE, Dialect, SessionSettings
from .prompt import OpenAiRealtimePrompter


def _openai_session(settings: SessionSettings) -> dict[str, Any]:
    """The OpenAI Realtime GA session shape, with audio settings nested under ``audio``."""
    turn_detection = {**(settings.turn_detection or {"type": "server_vad"}), "interrupt_response": True}
    pcm = {"type": "audio/pcm", "rate": SAMPLE_RATE}
    return {
        "type": "realtime",
        "instructions": settings.instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": pcm,
                "turn_detection": turn_detection,
                "transcription": {"model": settings.transcription_model},
            },
            "output": {"format": pcm, "voice": settings.voice},
        },
        "tools": list(settings.tools),
        "tool_choice": "auto",
    }


def _openai_instructions(instructions: str) -> dict[str, Any]:
    """An OpenAI Realtime GA session update holding only ``instructions`` (and the required ``type``)."""
    return {"type": "realtime", "instructions": instructions}


OPENAI = Dialect(
    provider_name="openai-realtime",
    url="wss://api.openai.com/v1/realtime",
    default_model="gpt-realtime-2.1",
    default_voice="marin",
    default_transcription_model="gpt-4o-transcribe",
    capabilities={
        "input_rate": SAMPLE_RATE,
        "output_rate": SAMPLE_RATE,
        "supports_truncate": True,
        "supports_out_of_band_response": True,
        "emits_speech_events": True,
        "delegation_style": "tool",
        "max_session_s": 3600,
        "replaces_instructions": True,
    },
    prompter=OpenAiRealtimePrompter(),
    session_config=_openai_session,
    instructions_config=_openai_instructions,
)
