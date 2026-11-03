"""Writes the live test's WAV fixtures to ``tests/fixtures/audio/``: 24 kHz mono PCM16, the format the
web transport takes.

The spoken files come from OpenAI's text-to-speech API, so this needs ``OPENAI_API_KEY``; the
silence file is generated locally. Run it once and commit the files::

    OPENAI_API_KEY=... uv run python -m tests.live.make_audio_fixtures
"""

import argparse
import json
import os
import sys
import urllib.request
import wave
from pathlib import Path

AUDIO_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "audio"

RATE = 24000
"""Sample rate of every fixture, in Hz; also the rate OpenAI's ``pcm`` speech format has."""

SPEECH = {
    "portfolio.wav": "How is my portfolio doing?",
    "tesla.wav": "And what about Tesla?",
}
"""Each spoken fixture's file name and the words in it."""

SILENCE = "silence.wav"

SILENCE_MS = 1500
"""Length of the silence fixture."""


def write_wav(path: Path, pcm16_mono: bytes) -> None:
    """Write little-endian PCM16 mono samples at ``RATE`` to ``path`` as a WAV file."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm16_mono)


def speak(text: str, *, api_key: str, base_url: str, model: str, voice: str) -> bytes:
    """``text`` spoken by OpenAI text-to-speech, as PCM16 mono samples at ``RATE``."""
    request = urllib.request.Request(
        f"{base_url}/audio/speech",
        data=json.dumps({"model": model, "voice": voice, "input": text, "response_format": "pcm"}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the live test's WAV fixtures.")
    parser.add_argument("--model", default="gpt-4o-mini-tts", help="OpenAI text-to-speech model")
    parser.add_argument("--voice", default="alloy", help="OpenAI text-to-speech voice")
    parser.add_argument("--base-url", default="https://api.openai.com/v1", help="OpenAI API base URL")
    args = parser.parse_args()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    write_wav(AUDIO_DIR / SILENCE, bytes(2 * RATE * SILENCE_MS // 1000))
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(f"wrote {SILENCE}; set OPENAI_API_KEY to generate {', '.join(SPEECH)}")
    for name, text in SPEECH.items():
        write_wav(
            AUDIO_DIR / name,
            speak(text, api_key=api_key, base_url=args.base_url, model=args.model, voice=args.voice),
        )
        print(f"wrote {AUDIO_DIR / name}: {text!r}")


if __name__ == "__main__":
    main()
