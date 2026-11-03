"""A loudness gate that tells when an allowed person starts talking, for local barge-in detection.

Providers that send no speech-started event (GPT-Live) leave barge-in to the session: when someone
starts talking over the model, it flushes the playback backlog. The gate decides only that someone
started talking; the provider still decides whether the model stops.
"""

import math
from collections.abc import Mapping

from .frames import FRAME_MS, rms_dbfs

DEFAULT_HANGOVER_MS = 300
"""How long a person counts as still talking after their last loud tick, in ms."""


class LoudnessGate:
    """Per-person RMS gate with a hangover, fed one 20 ms tick at a time.

    A person opens the gate with a tick louder than ``threshold_dbfs``; it closes once ``hangover_ms``
    of their ticks in a row are quiet, so the pauses between words do not close it.
    Each person has their own gate: one person talking does not hide another one starting.
    """

    def __init__(self, threshold_dbfs: float, hangover_ms: int = DEFAULT_HANGOVER_MS) -> None:
        """``threshold_dbfs`` is the RMS level above which a tick is loud; ``hangover_ms`` is rounded
        up to whole ticks."""
        self._threshold_dbfs = threshold_dbfs
        self._hangover_ticks = math.ceil(hangover_ms / FRAME_MS)
        self._open: dict[str, int] = {}
        """Each person whose gate is open, mapped to the quiet ticks left before it closes."""

    def trip(self, frames: Mapping[str, bytes]) -> bool:
        """Feed one tick and return True when someone's gate opened on it (a rising edge).

        ``frames`` maps each person heard in this tick to their PCM16 audio for it; people absent
        from it are quiet. Returns False while every open gate stays open, so one person talking
        trips the gate once, not on every tick.
        """
        loud = {person for person, pcm in frames.items() if rms_dbfs(pcm) > self._threshold_dbfs}
        opened = not loud <= self._open.keys()
        for person in list(self._open):
            if person not in loud:
                self._open[person] -= 1
                if self._open[person] <= 0:
                    del self._open[person]
        for person in loud:
            self._open[person] = self._hangover_ticks
        return opened
