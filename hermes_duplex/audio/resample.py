"""Streaming PCM16 sample-rate and channel conversion between transports and providers.

Discord carries 48 kHz stereo; providers take and return mono at their own rate (24 kHz or 16 kHz).
"""

import numpy as np
import soxr

from .frames import SAMPLE_BYTES


class StreamResampler:
    """Converts a continuous PCM16 stream from one rate and channel count to another, chunk by chunk.

    The resampler keeps its filter state and any partial sample between ``push`` calls, so a stream
    split into chunks of any size (even odd byte counts) comes out the same as if it were converted
    in one piece, with no clicks at chunk joins. Stereo input is downmixed to mono by averaging the
    channels; mono output going to stereo has its one channel duplicated. The filter holds back a
    few tens of milliseconds of audio, which comes out with later chunks. Use one instance per
    continuous stream.
    """

    def __init__(self, in_rate: int, out_rate: int, in_channels: int = 1, out_channels: int = 1) -> None:
        """Rates are in Hz; each channel count must be 1 (mono) or 2 (interleaved stereo)."""
        if in_channels not in (1, 2) or out_channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {in_channels} in and {out_channels} out")
        self._in_channels = in_channels
        self._out_channels = out_channels
        # Stereo-to-stereo resamples both channels; any other pairing resamples one mono channel.
        self._resampled_channels = 2 if in_channels == out_channels == 2 else 1
        self._stream = soxr.ResampleStream(in_rate, out_rate, self._resampled_channels, dtype="int16")
        self._carry = bytearray()

    def push(self, pcm16: bytes) -> bytes:
        """Add ``pcm16`` (interleaved at the input rate and channels) and return the converted audio
        now available, possibly empty. Bytes that do not complete a sample on every input channel
        are carried to the next call."""
        self._carry += pcm16
        in_frame_bytes = SAMPLE_BYTES * self._in_channels
        whole = len(self._carry) - len(self._carry) % in_frame_bytes
        samples = np.frombuffer(self._carry[:whole], dtype="<i2").reshape(-1, self._in_channels)
        del self._carry[:whole]

        if self._resampled_channels == 1 and self._in_channels == 2:
            samples = ((samples[:, 0].astype(np.int32) + samples[:, 1]) // 2).astype(np.int16)
        elif self._resampled_channels == 1:
            samples = samples[:, 0]
        resampled = self._stream.resample_chunk(np.ascontiguousarray(samples))

        if resampled.ndim == 1 and self._out_channels == 2:
            resampled = np.repeat(resampled, 2)
        return resampled.astype("<i2").tobytes()
