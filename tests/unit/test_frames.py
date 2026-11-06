import math

import numpy as np
import pytest

from hermes_duplex.audio.frames import FrameCarry, bytes_to_ms, frame_bytes, rms_dbfs, silent_frame


def _sine(rate: int, seconds: float, amplitude: float, hz: float = 440.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype("<i2").tobytes()


@pytest.mark.parametrize(
    ("rate", "channels", "expected"), [(48000, 2, 3840), (24000, 1, 960), (16000, 1, 640)]
)
def test_frame_bytes_is_20_ms_of_pcm16(rate: int, channels: int, expected: int) -> None:
    assert frame_bytes(rate, channels) == expected
    assert bytes_to_ms(expected, rate, channels) == 20


def test_silent_frame_is_zeroed_48k_stereo_frame() -> None:
    frame = silent_frame(48000, channels=2)

    assert len(frame) == 3840
    assert frame == bytes(3840)


def test_carry_rechunks_odd_sizes_into_whole_frames_without_loss() -> None:
    audio = bytes(range(256)) * 40  # 10240 bytes, not a whole number of 960-byte frames
    carry = FrameCarry(24000)
    frames: list[bytes] = []
    position = 0
    for size in [1, 7, 959, 961, 333, 2000, 3, 4999]:
        frames += carry.push(audio[position : position + size])
        position += size
    frames += carry.push(audio[position:])

    assert all(len(frame) == 960 for frame in frames)
    assert len(frames) == len(audio) // 960
    assert carry.carried_bytes == len(audio) % 960
    assert b"".join(frames) == audio[: len(frames) * 960]


def test_carry_returns_nothing_until_a_frame_fills_and_clear_discards_carry() -> None:
    carry = FrameCarry(24000)

    assert carry.push(bytes(500)) == []
    carry.clear()
    assert carry.push(bytes(500)) == []
    assert carry.carried_bytes == 500


def test_rms_of_full_scale_sine_is_minus_3_dbfs() -> None:
    assert rms_dbfs(_sine(24000, 1.0, 32767)) == pytest.approx(-3.01, abs=0.02)


def test_rms_of_half_scale_sine_is_6_db_lower() -> None:
    assert rms_dbfs(_sine(24000, 1.0, 16384)) == pytest.approx(-9.03, abs=0.02)


def test_rms_of_silence_and_empty_input_is_minus_infinity() -> None:
    assert rms_dbfs(silent_frame(24000)) == -math.inf
    assert rms_dbfs(b"") == -math.inf
