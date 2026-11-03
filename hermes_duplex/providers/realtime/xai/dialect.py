"""The xAI Grok Voice dialect: its endpoint, defaults, capabilities, prompter, and session shape.

Grok Voice speaks the Realtime GA event names with a flatter session shape and has no truncate or
out-of-band responses.
"""

from typing import Any

from ....config import ConfigError
from ..adapter import SAMPLE_RATE, Dialect, SessionSettings
from .prompt import GrokPrompter

XAI_VOICE_PREFIX = "xai_"
"""The prefix xAI voice ids carry on the wire (``xai_ara``); config names the voice without it."""


def _xai_session(settings: SessionSettings) -> dict[str, Any]:
    """The xAI Grok Voice session shape: voice and turn detection at the top, audio formats only.

    Raises ``ConfigError`` on ``provider.turn_detection.type`` for anything but ``server_vad``, the
    only automatic turn detection xAI offers.
    """
    turn_detection = settings.turn_detection or {"type": "server_vad"}
    if turn_detection.get("type") != "server_vad":
        raise ConfigError("provider.turn_detection.type", "xai-realtime supports only server_vad")
    voice = settings.voice
    pcm = {"type": "audio/pcm", "rate": SAMPLE_RATE}
    return {
        "voice": voice if voice.startswith(XAI_VOICE_PREFIX) else XAI_VOICE_PREFIX + voice,
        "instructions": settings.instructions,
        "turn_detection": dict(turn_detection),
        "audio": {
            "input": {"format": pcm, "transcription": {"model": settings.transcription_model}},
            "output": {"format": pcm},
        },
        "tools": list(settings.tools),
    }


XAI = Dialect(
    provider_name="xai-realtime",
    url="wss://api.x.ai/v1/realtime",
    default_model="grok-voice-latest",
    default_voice="ara",
    default_transcription_model="grok-transcribe",
    capabilities={
        "input_rate": SAMPLE_RATE,
        "output_rate": SAMPLE_RATE,
        "supports_truncate": False,
        "supports_out_of_band_response": False,
        "emits_speech_events": True,
        "delegation_style": "tool",
        "max_session_s": None,
    },
    prompter=GrokPrompter(),
    session_config=_xai_session,
)
