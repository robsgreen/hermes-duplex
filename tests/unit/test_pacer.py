import pytest

from hermes_duplex.audio.pacer import OutputPacer

RATE = 24000
BYTES_PER_MS = RATE * 2 // 1000  # 48


class FakeClock:
    def __init__(self) -> None:
        self.now_s = 100.0

    def __call__(self) -> float:
        return self.now_s

    def advance_ms(self, ms: float) -> None:
        self.now_s += ms / 1000


def _audio_ms(ms: int) -> bytes:
    return bytes(ms * BYTES_PER_MS)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pacer(clock: FakeClock) -> OutputPacer:
    return OutputPacer(RATE, lead_ms=200, clock=clock)


def test_releases_at_most_lead_ms_at_once(pacer: OutputPacer) -> None:
    pacer.push(_audio_ms(1000), "item-1")

    released = pacer.release()

    assert len(released) == 200 * BYTES_PER_MS
    assert pacer.ahead_ms == pytest.approx(200)
    assert pacer.held_ms == pytest.approx(800)
    assert pacer.release() == b""


def test_releases_only_what_played_since_last_release(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(1000), "item-1")
    pacer.release()

    clock.advance_ms(20)
    assert len(pacer.release()) == 20 * BYTES_PER_MS
    clock.advance_ms(50)
    assert len(pacer.release()) == 50 * BYTES_PER_MS
    assert pacer.ahead_ms == pytest.approx(200)


def test_unplayed_audio_never_exceeds_lead_over_a_whole_stream(pacer: OutputPacer, clock: FakeClock) -> None:
    total = bytearray()
    for _ in range(100):
        pacer.push(_audio_ms(37), "item-1")
        total += pacer.release()
        assert pacer.ahead_ms <= 200 + 1e-6
        clock.advance_ms(20)
    while pacer.held_ms:
        total += pacer.release()
        clock.advance_ms(20)

    assert len(total) == 3700 * BYTES_PER_MS


def test_idle_transport_restarts_the_lead_from_now(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(100), "item-1")
    pacer.release()
    clock.advance_ms(5000)

    pacer.push(_audio_ms(1000), "item-2")

    assert len(pacer.release()) == 200 * BYTES_PER_MS


def test_drop_discards_backlog_and_returns_dropped_ms(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(1000), "item-1")
    pacer.release()
    clock.advance_ms(60)
    pacer.release()

    assert pacer.drop() == pytest.approx(740)
    assert pacer.held_ms == 0
    assert pacer.release() == b""


def test_after_drop_new_audio_is_released_at_once(pacer: OutputPacer) -> None:
    pacer.push(_audio_ms(1000), "item-1")
    pacer.release()
    pacer.drop()

    pacer.push(_audio_ms(500), "item-2")

    assert len(pacer.release()) == 200 * BYTES_PER_MS


def test_tracks_released_and_played_ms_of_current_item(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(100), "item-1")
    pacer.push(_audio_ms(1000), "item-2")
    pacer.release()  # 100 ms of item-1, 100 ms of item-2

    assert pacer.item_id == "item-2"
    assert pacer.item_released_ms == pytest.approx(100)
    assert pacer.item_played_ms == 0

    clock.advance_ms(150)
    assert pacer.item_played_ms == pytest.approx(50)
    pacer.release()
    assert pacer.item_released_ms == pytest.approx(250)
    assert pacer.item_played_ms == pytest.approx(50)


def test_drop_keeps_only_heard_audio_in_item_count(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(1000), "item-1")
    pacer.release()
    clock.advance_ms(80)

    pacer.drop()

    assert pacer.item_id == "item-1"
    assert pacer.item_released_ms == pytest.approx(80)
    assert pacer.item_played_ms == pytest.approx(80)


def test_audio_after_drop_starts_a_new_item_even_without_ids(pacer: OutputPacer, clock: FakeClock) -> None:
    pacer.push(_audio_ms(1000))
    pacer.release()
    clock.advance_ms(100)
    pacer.drop()

    pacer.push(_audio_ms(30))
    pacer.release()

    assert pacer.item_id is None
    assert pacer.item_released_ms == pytest.approx(30)


def test_rejects_partial_samples(pacer: OutputPacer) -> None:
    with pytest.raises(ValueError, match="whole samples"):
        pacer.push(b"\x00\x00\x00", "item-1")
