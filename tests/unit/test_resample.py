import numpy as np
import pytest
import soxr

from hermes_duplex.audio.resample import StreamResampler

CONTINUITY_TOLERANCE = 4
"""Largest sample difference, in int16 steps, allowed between streamed and one-shot resampling."""

ODD_CHUNK_BYTES = [1, 7, 333, 961, 4999, 17, 2, 1501]


def _sine(rate: int, seconds: float, amplitude: float = 10000, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def _push_in_odd_chunks(resampler: StreamResampler, pcm: bytes) -> tuple[bytes, list[int]]:
    """Push ``pcm`` in odd-sized chunks; return the joined output and the output offset (in bytes)
    at which each non-empty chunk's output starts."""
    outputs: list[bytes] = []
    joins: list[int] = []
    position = 0
    produced = 0
    while position < len(pcm):
        for size in ODD_CHUNK_BYTES:
            out = resampler.push(pcm[position : position + size])
            position += size
            if out:
                joins.append(produced)
                outputs.append(out)
                produced += len(out)
    return b"".join(outputs), joins[1:]


@pytest.mark.parametrize("out_rate", [24000, 16000])
def test_resample_continuity_streamed_odd_chunks_match_one_shot(out_rate: int) -> None:
    mono = _sine(48000, 1.0)
    one_shot = soxr.resample(mono, 48000, out_rate)

    streamed_bytes, joins = _push_in_odd_chunks(StreamResampler(48000, out_rate), mono.tobytes())
    streamed = np.frombuffer(streamed_bytes, dtype="<i2").astype(np.int32)

    # The filter holds back a few tens of ms; everything it has released must match.
    assert len(streamed) > 0.9 * len(one_shot)
    difference = np.abs(streamed - one_shot[: len(streamed)].astype(np.int32))
    join_samples = [offset // 2 for offset in joins]
    max_join_difference = int(difference[join_samples].max())
    print(f"{out_rate} Hz: max difference at {len(join_samples)} chunk joins = {max_join_difference}")
    assert max_join_difference <= CONTINUITY_TOLERANCE
    assert int(difference.max()) <= CONTINUITY_TOLERANCE


def test_stereo_48k_to_mono_averages_channels() -> None:
    left = _sine(48000, 0.5, amplitude=12000)
    right = (left // 3).astype(np.int16)
    stereo = np.column_stack([left, right]).reshape(-1)
    expected = soxr.resample(((left.astype(np.int32) + right) // 2).astype(np.int16), 48000, 24000)

    out = np.frombuffer(StreamResampler(48000, 24000, in_channels=2).push(stereo.tobytes()), dtype="<i2")

    assert len(out) > 0
    assert np.abs(out.astype(np.int32) - expected[: len(out)]).max() <= 1


def test_mono_to_48k_stereo_duplicates_channel() -> None:
    mono = _sine(24000, 0.5)

    out = StreamResampler(24000, 48000, out_channels=2).push(mono.tobytes())
    stereo = np.frombuffer(out, dtype="<i2").reshape(-1, 2)

    assert len(stereo) > 0
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    expected = soxr.resample(mono, 24000, 48000)
    assert np.abs(stereo[:, 0].astype(np.int32) - expected[: len(stereo)]).max() <= CONTINUITY_TOLERANCE


def test_stereo_round_trip_keeps_both_channels() -> None:
    left = _sine(48000, 0.5, amplitude=12000)
    right = _sine(48000, 0.5, amplitude=6000, hz=300)
    stereo = np.column_stack([left, right]).reshape(-1)

    out = StreamResampler(48000, 48000, in_channels=2, out_channels=2).push(stereo.tobytes())
    channels = np.frombuffer(out, dtype="<i2").reshape(-1, 2)

    assert not np.array_equal(channels[:, 0], channels[:, 1])


def test_partial_samples_are_carried_to_the_next_push() -> None:
    stereo = _sine(48000, 0.2).repeat(2).tobytes()
    split = StreamResampler(48000, 24000, in_channels=2)

    assert split.push(stereo[:3]) == b""
    split_out = split.push(stereo[3:])

    assert split_out == StreamResampler(48000, 24000, in_channels=2).push(stereo)


def test_rejects_unsupported_channel_counts() -> None:
    with pytest.raises(ValueError, match="channels"):
        StreamResampler(48000, 24000, in_channels=6)
