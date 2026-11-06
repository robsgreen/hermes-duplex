from hermes_duplex.audio.mixer import AttributionRing


def _record_span(ring: AttributionRing, start_ms: int, end_ms: int, *people: str) -> None:
    for t_ms in range(start_ms, end_ms, 20):
        ring.record(t_ms, frozenset(people))


def test_credits_person_with_most_voiced_ticks_in_window() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 1000, "rob")
    _record_span(ring, 1000, 1400, "alex")

    assert ring.credit(0, 2000) == "rob"
    assert ring.credit(900, 2000) == "alex"


def test_window_bounds_are_inclusive() -> None:
    ring = AttributionRing()
    ring.record(1000, frozenset({"rob"}))

    assert ring.credit(1000, 1000) == "rob"
    assert ring.credit(1001, 2000) is None
    assert ring.credit(0, 999) is None


def test_tie_goes_to_most_recently_voiced() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 200, "alex")
    _record_span(ring, 200, 400, "rob")

    assert ring.credit(0, 1000) == "rob"


def test_tie_with_same_last_tick_goes_to_lowest_id() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 200, "rob", "alex")

    assert ring.credit(0, 1000) == "alex"


def test_empty_window_credits_nobody() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 1000)

    assert ring.credit(0, 1000) is None
    assert AttributionRing().credit(0, 1000) is None


def test_clear_forgets_history_for_a_new_provider_session() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 4000, "rob")

    ring.clear()
    _record_span(ring, 0, 200, "alex")

    assert ring.credit(-4000, 500) == "alex"


def test_keeps_at_least_60_s_of_history() -> None:
    ring = AttributionRing()
    _record_span(ring, 0, 1000, "alex")
    _record_span(ring, 1000, 61_000, "rob")

    assert ring.credit(0, 999) == "alex"

    _record_span(ring, 61_000, 62_000, "rob")
    assert ring.credit(0, 999) is None
