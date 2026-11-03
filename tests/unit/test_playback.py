"""DuplexAudioSource: silence on underrun, re-chunking, drop-oldest overflow, flush, thread safety."""

import threading
import time

import discord

from hermes_duplex.transports.discord.playback import DuplexAudioSource

FRAME_BYTES = 3840  # 20 ms of 48 kHz stereo PCM16


def _frame(fill: int) -> bytes:
    """One 20 ms frame whose bytes all equal ``fill``, so tests can tell frames apart."""
    return bytes([fill]) * FRAME_BYTES


def test_is_a_pcm_audio_source() -> None:
    source = DuplexAudioSource()

    assert isinstance(source, discord.AudioSource)
    assert source.is_opus() is False


def test_underrun_returns_one_silent_frame() -> None:
    assert DuplexAudioSource().read() == bytes(FRAME_BYTES)


def test_never_returns_empty_bytes_across_many_reads() -> None:
    source = DuplexAudioSource()
    source.feed(_frame(1) * 3 + _frame(2)[:100])

    reads = [source.read() for _ in range(1000)]

    assert all(len(pcm) == FRAME_BYTES for pcm in reads)
    assert reads[:3] == [_frame(1)] * 3
    assert reads[3:] == [bytes(FRAME_BYTES)] * 997


def test_feed_rechunks_odd_chunks_into_whole_frames() -> None:
    audio = b"".join(_frame(n) for n in range(1, 4))
    source = DuplexAudioSource()

    for start in range(0, len(audio), 1000):
        source.feed(audio[start : start + 1000])

    assert [source.read() for _ in range(4)] == [_frame(1), _frame(2), _frame(3), bytes(FRAME_BYTES)]


def test_partial_frame_waits_for_the_next_feed() -> None:
    source = DuplexAudioSource()

    source.feed(_frame(7)[:1000])
    assert source.read() == bytes(FRAME_BYTES)

    source.feed(_frame(7)[1000:])
    assert source.read() == _frame(7)


def test_queue_holds_five_seconds_by_default_and_drops_the_oldest() -> None:
    source = DuplexAudioSource()

    source.feed(b"".join(_frame(n % 256) for n in range(260)))
    reads = [source.read() for _ in range(251)]

    assert reads[0] == _frame(10)
    assert reads[249] == _frame(259 % 256)
    assert reads[250] == bytes(FRAME_BYTES)


def test_queue_size_follows_queue_s() -> None:
    source = DuplexAudioSource(queue_s=0.1)

    source.feed(b"".join(_frame(n) for n in range(1, 8)))

    assert [source.read() for _ in range(6)] == [_frame(n) for n in range(3, 8)] + [bytes(FRAME_BYTES)]


def test_flush_empties_the_queue_so_the_next_read_is_silence() -> None:
    source = DuplexAudioSource()
    source.feed(_frame(1) * 50)
    for _ in range(40):
        source.read()

    source.flush()

    assert source.read() == bytes(FRAME_BYTES)


def test_flush_discards_the_carried_partial_frame() -> None:
    source = DuplexAudioSource()
    source.feed(_frame(1)[:1000])

    source.flush()
    source.feed(_frame(2))

    assert source.read() == _frame(2)


def test_feed_and_read_from_different_threads_lose_and_duplicate_nothing() -> None:
    frame_count = 2000
    source = DuplexAudioSource(queue_s=frame_count * 0.02)
    audio = [_frame(n % 250 + 1) for n in range(frame_count)]
    played: list[bytes] = []
    deadline = time.monotonic() + 10

    def player() -> None:
        while len(played) < frame_count and time.monotonic() < deadline:
            pcm = source.read()
            if pcm != bytes(FRAME_BYTES):
                played.append(pcm)

    thread = threading.Thread(target=player)
    thread.start()
    for start in range(0, frame_count, 7):
        source.feed(b"".join(audio[start : start + 7]))
    thread.join()

    assert played == audio
