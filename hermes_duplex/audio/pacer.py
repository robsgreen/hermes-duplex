"""Output pacing: hold provider audio and release it to the transport just ahead of real time.

Providers send output audio faster than it plays. If all of it went straight to the transport, a
barge-in flush would have to discard seconds of queued audio somewhere the session cannot see. The
pacer keeps that backlog itself and lets at most ``lead_ms`` of unplayed audio reach the transport.
"""

import time
from collections import deque
from collections.abc import Callable

from .frames import SAMPLE_BYTES, bytes_to_ms


class OutputPacer:
    """Holds mono PCM16 output audio and releases it no more than ``lead_ms`` ahead of real time.

    The pacer assumes the transport starts playing released audio as soon as it receives it, at real
    time, and models the play position from its clock. Call ``release`` on every tick and pass what
    it returns to the transport. On barge-in, flush the transport and call ``drop`` together.

    It also counts the audio released for the current output item, so the session can tell the
    provider how much of an interrupted item the user heard. An item starts when audio with a new
    ``item_id`` is released, or with the first audio released after a ``drop``.

    Not thread-safe; use it from one event loop.
    """

    def __init__(self, rate: int, lead_ms: int = 200, clock: Callable[[], float] = time.monotonic) -> None:
        """``rate`` is the audio sample rate in Hz; ``clock`` returns seconds and must not go back."""
        self._rate = rate
        self._lead_ms = lead_ms
        self._clock = clock
        self._held: deque[tuple[str | None, bytearray]] = deque()
        self._held_bytes = 0
        self._play_end_s = clock()
        self._item_id: str | None = None
        self._item_open = False
        self._item_released_bytes = 0

    @property
    def held_ms(self) -> float:
        """Audio waiting in the pacer that has not been released yet, in ms."""
        return bytes_to_ms(self._held_bytes, self._rate)

    @property
    def ahead_ms(self) -> float:
        """Released audio the transport has not finished playing yet, in ms (at most ``lead_ms``)."""
        return max(0.0, self._play_end_s - self._clock()) * 1000

    @property
    def item_id(self) -> str | None:
        """Id of the item the most recently released audio belongs to (None when unknown or none)."""
        return self._item_id

    @property
    def item_released_ms(self) -> float:
        """Audio of the current item released to the transport so far, in ms."""
        return bytes_to_ms(self._item_released_bytes, self._rate)

    @property
    def item_played_ms(self) -> float:
        """Audio of the current item the user has heard so far, in ms: released minus still ahead."""
        return max(0.0, self.item_released_ms - self.ahead_ms)

    def push(self, pcm16: bytes, item_id: str | None = None) -> None:
        """Hold ``pcm16`` (whole samples) behind any audio already held; ``item_id`` names its item."""
        if len(pcm16) % SAMPLE_BYTES:
            raise ValueError(f"PCM16 audio must hold whole samples, got {len(pcm16)} bytes")
        if not pcm16:
            return
        if self._held and self._held[-1][0] == item_id:
            self._held[-1][1].extend(pcm16)
        else:
            self._held.append((item_id, bytearray(pcm16)))
        self._held_bytes += len(pcm16)

    def release(self) -> bytes:
        """Return the held audio that may go to the transport now, oldest first (maybe empty).

        Releases as much as keeps the unplayed audio at the transport within ``lead_ms``.
        """
        now = self._clock()
        self._play_end_s = max(self._play_end_s, now)
        room_ms = self._lead_ms - (self._play_end_s - now) * 1000
        budget = round(room_ms * self._rate / 1000) * SAMPLE_BYTES
        out = bytearray()
        while self._held and budget > 0:
            item_id, pcm = self._held[0]
            if not self._item_open or item_id != self._item_id:
                self._item_id = item_id
                self._item_open = True
                self._item_released_bytes = 0
            take = min(budget, len(pcm))
            out += pcm[:take]
            del pcm[:take]
            if not pcm:
                self._held.popleft()
            budget -= take
            self._item_released_bytes += take
        self._held_bytes -= len(out)
        self._play_end_s += bytes_to_ms(len(out), self._rate) / 1000
        return bytes(out)

    def drop(self) -> float:
        """Discard all held audio and return how much was discarded, in ms.

        Also forgets the released audio not yet played, on the assumption that the transport flushed
        its playback at the same time: the current item's released count shrinks to what was heard,
        and the next audio is released at once and starts a new item.
        """
        dropped_ms = self.held_ms
        unplayed_bytes = round(self.ahead_ms * self._rate / 1000) * SAMPLE_BYTES
        self._item_released_bytes -= min(unplayed_bytes, self._item_released_bytes)
        self._held.clear()
        self._held_bytes = 0
        self._play_end_s = self._clock()
        self._item_open = False
        return dropped_ms
