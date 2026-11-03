"""Per-tick input mixing across speakers, and which allowed people were voiced when."""

from collections import deque
from collections.abc import Container, Mapping
from dataclasses import dataclass

import numpy as np

from .frames import SAMPLE_BYTES, frame_bytes, rms_dbfs

MIX_RATE = 48000
"""Sample rate in Hz of the frames ``mix_allowed`` takes and returns (Discord's rate)."""

MIX_CHANNELS = 2
"""Channel count of the frames ``mix_allowed`` takes and returns (interleaved stereo)."""


@dataclass(frozen=True)
class MixedFrame:
    """One 20 ms tick of input mixed from the allowed speakers."""

    pcm16_stereo: bytes
    """48 kHz interleaved stereo PCM16; silence when no allowed speaker sent a frame."""

    voiced_speakers: frozenset[str]
    """Ids of the allowed people whose own frame was louder than the voiced threshold."""


def mix_allowed(
    frames: Mapping[str, bytes], allowed: Container[str], voiced_threshold_dbfs: float
) -> MixedFrame:
    """Mix one tick of per-person frames, keeping only people in ``allowed``.

    ``frames`` maps a person id to that person's 20 ms frame of 48 kHz stereo PCM16 for this tick;
    people who sent nothing are absent. Frames from people not in ``allowed`` are dropped before
    mixing, so they never reach the mix or the voiced set. The remaining frames are summed and
    clipped to the int16 range. A person is voiced when the RMS of their own frame is above
    ``voiced_threshold_dbfs``.

    Raises ValueError when an allowed person's frame is not exactly one 20 ms frame long.
    """
    expected_bytes = frame_bytes(MIX_RATE, MIX_CHANNELS)
    total = np.zeros(expected_bytes // SAMPLE_BYTES, dtype=np.int32)
    voiced: set[str] = set()
    for person, pcm in frames.items():
        if person not in allowed:
            continue
        if len(pcm) != expected_bytes:
            raise ValueError(f"frame from {person!r} is {len(pcm)} bytes, expected {expected_bytes}")
        total += np.frombuffer(pcm, dtype="<i2")
        if rms_dbfs(pcm) > voiced_threshold_dbfs:
            voiced.add(person)
    mixed = np.clip(total, -32768, 32767).astype("<i2").tobytes()
    return MixedFrame(pcm16_stereo=mixed, voiced_speakers=frozenset(voiced))


class AttributionRing:
    """Remembers which people were voiced in each 20 ms input tick, to credit requests to a person.

    The session keys ticks by its own input timeline: the ms of input it has sent to the current
    provider session, which starts at 0 at each connect (not ``InputFrame.t_ms``, which never
    restarts), so it calls ``clear`` whenever the provider (re)connects. Ticks older than ``capacity_ms`` before the newest
    recorded tick are forgotten.
    """

    def __init__(self, capacity_ms: int = 60_000) -> None:
        """``capacity_ms`` is how much history to keep, in ms."""
        self._capacity_ms = capacity_ms
        self._ticks: deque[tuple[int, frozenset[str]]] = deque()

    def record(self, t_ms: int, voiced_speakers: frozenset[str]) -> None:
        """Record the people voiced in the tick starting at ``t_ms``.

        ``t_ms`` must not be earlier than the previously recorded tick's. Ticks with nobody voiced
        need no entry and are ignored.
        """
        if not voiced_speakers:
            return
        self._ticks.append((t_ms, voiced_speakers))
        while self._ticks[0][0] < t_ms - self._capacity_ms:
            self._ticks.popleft()

    def credit(self, window_start_ms: int, window_end_ms: int) -> str | None:
        """Return the person voiced in the most ticks starting within [window_start_ms, window_end_ms].

        Ties go to the person voiced most recently in the window, then to the lowest id, so the
        answer is deterministic. Returns None when nobody was voiced in the window.
        """
        voiced_ticks: dict[str, int] = {}
        last_voiced_ms: dict[str, int] = {}
        for t_ms, speakers in self._ticks:
            if window_start_ms <= t_ms <= window_end_ms:
                for person in speakers:
                    voiced_ticks[person] = voiced_ticks.get(person, 0) + 1
                    last_voiced_ms[person] = t_ms
        if not voiced_ticks:
            return None
        return min(voiced_ticks, key=lambda person: (-voiced_ticks[person], -last_voiced_ms[person], person))

    def clear(self) -> None:
        """Forget every recorded tick."""
        self._ticks.clear()
