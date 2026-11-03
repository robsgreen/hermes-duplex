import numpy as np

from hermes_duplex.audio.frames import frame_samples, silent_frame
from hermes_duplex.audio.gate import LoudnessGate

THRESHOLD_DBFS = -35.0
RATE = 24000
LOUD = np.full(frame_samples(RATE), 1000, dtype="<i2").tobytes()  # about -30 dBFS
QUIET = np.full(frame_samples(RATE), 300, dtype="<i2").tobytes()  # about -41 dBFS
SILENT = silent_frame(RATE)


def _trips(gate: LoudnessGate, ticks: list[dict[str, bytes]]) -> list[bool]:
    return [gate.trip(frames) for frames in ticks]


def test_trips_on_the_first_tick_above_the_threshold() -> None:
    gate = LoudnessGate(THRESHOLD_DBFS)

    assert _trips(gate, [{"rob": QUIET}, {"rob": SILENT}, {"rob": LOUD}]) == [False, False, True]


def test_trips_once_while_the_speaker_keeps_talking() -> None:
    gate = LoudnessGate(THRESHOLD_DBFS)

    assert _trips(gate, [{"rob": LOUD}] * 10) == [True] + [False] * 9


def test_threshold_comes_from_the_caller() -> None:
    sensitive = LoudnessGate(-45.0)
    strict = LoudnessGate(-25.0)

    assert sensitive.trip({"rob": QUIET})
    assert not strict.trip({"rob": LOUD})


def test_pauses_shorter_than_the_hangover_keep_the_gate_open() -> None:
    gate = LoudnessGate(THRESHOLD_DBFS, hangover_ms=100)  # 5 ticks

    pause = [{"rob": SILENT}] * 4
    assert _trips(gate, [{"rob": LOUD}, *pause, {"rob": LOUD}]) == [True] + [False] * 5


def test_a_pause_as_long_as_the_hangover_closes_the_gate() -> None:
    gate = LoudnessGate(THRESHOLD_DBFS, hangover_ms=100)  # 5 ticks

    pause = [{}] * 5  # a person absent from a tick is quiet
    assert _trips(gate, [{"rob": LOUD}, *pause, {"rob": LOUD}]) == [True] + [False] * 5 + [True]


def test_each_speaker_has_their_own_gate() -> None:
    gate = LoudnessGate(THRESHOLD_DBFS)

    assert _trips(gate, [{"rob": LOUD}, {"rob": LOUD, "alex": LOUD}, {"rob": LOUD, "alex": LOUD}]) == [
        True,
        True,
        False,
    ]
