"""Discord playback: an ``AudioSource`` fed from the event loop and read by discord.py's player thread.

discord.py's player thread calls ``read`` every 20 ms and stops playing for good the first time it
gets ``b""``. So this source never runs dry: when nothing is queued it plays silence.
"""

import logging
import threading
from collections import deque

import discord

from ...audio.frames import FRAME_MS, FrameCarry, silent_frame

logger = logging.getLogger(__name__)

RATE = 48000
"""Sample rate of Discord voice audio, in Hz."""

CHANNELS = 2
"""Discord voice audio is interleaved stereo."""


class DuplexAudioSource(discord.AudioSource):
    """Plays 48 kHz stereo PCM16 queued with ``feed``, and silence whenever the queue is empty.

    The queue holds at most ``queue_s`` of audio; feeding more drops the oldest frames.

    ``feed`` and ``flush`` run on the event loop while discord.py calls ``read`` on its player
    thread; every method is safe to call from either thread.
    """

    def __init__(self, queue_s: float = 5.0) -> None:
        """``queue_s`` is the most audio the queue holds before it drops the oldest, in seconds."""
        self._silence = silent_frame(RATE, CHANNELS)
        self._carry = FrameCarry(RATE, CHANNELS)
        self._frames: deque[bytes] = deque(maxlen=max(1, round(queue_s * 1000 / FRAME_MS)))
        self._lock = threading.Lock()

    def is_opus(self) -> bool:
        """Always False: the source yields raw PCM, which discord.py Opus-encodes itself."""
        return False

    def feed(self, pcm48_stereo: bytes) -> None:
        """Queue interleaved 48 kHz stereo PCM16 of any length behind the audio already queued.

        Bytes that do not fill a 20 ms frame wait for the next ``feed``. When the queue is full, the
        oldest frames are dropped to make room.
        """
        with self._lock:
            frames = self._carry.push(pcm48_stereo)
            overflow = len(self._frames) + len(frames) - (self._frames.maxlen or 0)
            self._frames.extend(frames)
        if overflow > 0:
            logger.debug("playback queue full; dropped the oldest %d frames", overflow)

    def read(self) -> bytes:
        """Return the next 20 ms frame (3840 bytes), or a silent frame when nothing is queued."""
        with self._lock:
            return self._frames.popleft() if self._frames else self._silence

    def flush(self) -> None:
        """Discard all queued audio; the next ``read`` returns silence, so playback stops within one
        20 ms tick."""
        with self._lock:
            self._frames.clear()
            self._carry.clear()
