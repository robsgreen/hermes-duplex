import numpy as np
import pytest

from hermes_duplex.audio.frames import frame_bytes, silent_frame
from hermes_duplex.audio.mixer import mix_allowed

THRESHOLD_DBFS = -35.0
SAMPLES_PER_FRAME = frame_bytes(48000, 2) // 2


def _frame(value: int) -> bytes:
    """A 48 kHz stereo frame with every sample set to ``value``."""
    return np.full(SAMPLES_PER_FRAME, value, dtype="<i2").tobytes()


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2")


def test_unknown_discord_user_frames_are_absent_from_mix() -> None:
    frames = {"rob": _frame(1000), "stranger": _frame(5000)}

    mixed = mix_allowed(frames, allowed={"rob"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert mixed.pcm16_stereo == _frame(1000)
    assert mixed.voiced_speakers == frozenset({"rob"})


def test_only_unknown_speakers_mix_to_silence() -> None:
    mixed = mix_allowed({"stranger": _frame(5000)}, allowed={"rob"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert mixed.pcm16_stereo == silent_frame(48000, channels=2)
    assert mixed.voiced_speakers == frozenset()


def test_allowed_frames_are_summed() -> None:
    frames = {"rob": _frame(1000), "alex": _frame(-300)}

    mixed = mix_allowed(frames, allowed={"rob", "alex"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert np.all(_samples(mixed.pcm16_stereo) == 700)


def test_sum_clips_to_int16_range() -> None:
    loud = {"rob": _frame(30000), "alex": _frame(20000)}
    loud_negative = {"rob": _frame(-30000), "alex": _frame(-20000)}

    high = mix_allowed(loud, allowed={"rob", "alex"}, voiced_threshold_dbfs=THRESHOLD_DBFS)
    low = mix_allowed(loud_negative, allowed={"rob", "alex"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert np.all(_samples(high.pcm16_stereo) == 32767)
    assert np.all(_samples(low.pcm16_stereo) == -32768)


def test_voiced_set_holds_only_allowed_people_above_threshold() -> None:
    frames = {
        "rob": _frame(3000),  # about -21 dBFS
        "alex": _frame(10),  # about -70 dBFS
        "sam": silent_frame(48000, channels=2),
        "stranger": _frame(3000),
    }

    mixed = mix_allowed(frames, allowed={"rob", "alex", "sam"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert mixed.voiced_speakers == frozenset({"rob"})


def test_no_frames_mix_to_silence() -> None:
    mixed = mix_allowed({}, allowed={"rob"}, voiced_threshold_dbfs=THRESHOLD_DBFS)

    assert mixed.pcm16_stereo == silent_frame(48000, channels=2)
    assert mixed.voiced_speakers == frozenset()


def test_rejects_allowed_frame_of_wrong_length() -> None:
    with pytest.raises(ValueError, match="rob"):
        mix_allowed({"rob": bytes(100)}, allowed={"rob"}, voiced_threshold_dbfs=THRESHOLD_DBFS)
