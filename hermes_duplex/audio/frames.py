"""PCM16 frame helpers: 20 ms framing at any sample rate, re-chunking, silence, and loudness.

All audio is signed 16-bit little-endian PCM; multi-channel audio is interleaved.
"""

import math

import numpy as np

FRAME_MS = 20
"""Length of one audio frame (one mix tick) in milliseconds."""

SAMPLE_BYTES = 2
"""Bytes per PCM16 sample, per channel."""


def frame_samples(rate: int) -> int:
    """Return the number of samples per channel in one 20 ms frame at ``rate`` Hz."""
    return rate // 50


def frame_bytes(rate: int, channels: int = 1) -> int:
    """Return the size in bytes of one 20 ms PCM16 frame at ``rate`` Hz with ``channels`` channels.

    48 kHz stereo (Discord) is 3840 bytes; 24 kHz mono is 960 bytes.
    """
    return frame_samples(rate) * channels * SAMPLE_BYTES


def silent_frame(rate: int, channels: int = 1) -> bytes:
    """Return one 20 ms frame of silence at ``rate`` Hz with ``channels`` channels."""
    return bytes(frame_bytes(rate, channels))


def bytes_to_ms(byte_count: int, rate: int, channels: int = 1) -> float:
    """Return the duration in milliseconds of ``byte_count`` bytes of PCM16 audio."""
    return byte_count * 1000 / (rate * channels * SAMPLE_BYTES)


def rms_dbfs(pcm16: bytes) -> float:
    """Return the RMS level of PCM16 audio in dBFS, relative to a full-scale square wave.

    A full-scale sine reads about -3 dBFS. Silence and empty input return ``-math.inf``, so every
    threshold comparison treats them as quiet. ``pcm16`` must hold whole samples (an even length).
    """
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(samples * samples)))
    if rms == 0:
        return -math.inf
    return 20 * math.log10(rms / 32768)


class FrameCarry:
    """Re-chunks a stream of arbitrarily sized PCM16 chunks into whole 20 ms frames.

    Bytes that do not fill a frame are carried over to the next ``push``, so no audio is lost or
    duplicated at chunk boundaries.
    """

    def __init__(self, rate: int, channels: int = 1) -> None:
        self.frame_bytes = frame_bytes(rate, channels)
        self._carry = bytearray()

    @property
    def carried_bytes(self) -> int:
        """Bytes held back because they do not fill a whole frame yet."""
        return len(self._carry)

    def push(self, chunk: bytes) -> list[bytes]:
        """Add ``chunk`` and return every whole frame now available, oldest first (maybe none)."""
        self._carry += chunk
        whole = len(self._carry) - len(self._carry) % self.frame_bytes
        frames = [bytes(self._carry[i : i + self.frame_bytes]) for i in range(0, whole, self.frame_bytes)]
        del self._carry[:whole]
        return frames

    def clear(self) -> None:
        """Discard the carried partial frame."""
        self._carry.clear()
