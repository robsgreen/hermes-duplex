"""CompositeRouter: fast and main paths, deadline, circuit breaker, hint, sticky rule, fallbacks,
``_control``, the model table, and the decision log."""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

import pytest

from hermes_duplex.config import CONTROL_PROFILE, ControlExamples, ProfileConfig, RouterConfig
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.router.base import (
    BackendResult,
    ControlIntent,
    ControlSpec,
    Effort,
    ProfileSpec,
    RouteRequest,
)
from hermes_duplex.router.composite import CompositeRouter
from hermes_duplex.router.knn import KnnBackend
from tests.fakes.fake_embedder import fake_embed

FAST = ModelChoice(provider="openai", model="fast-1")
DEEP = ModelChoice(provider="anthropic", model="deep-1")
FALLBACK = ModelChoice(provider="openai", model="default-1")

PROFILES: Mapping[str, ProfileConfig] = {
    "stock-picker": ProfileConfig(
        description="Portfolio, holdings, stock research, trades",
        examples=("how's my portfolio", "should I buy NVDA", "what did TSLA do today"),
        models=MappingProxyType({"low": FAST, "high": DEEP}),
        default_model=FALLBACK,
    ),
    "general": ProfileConfig(description="Anything else", examples=("what's the weather", "set a reminder")),
}
CONFIG = RouterConfig(backend="knn", fast_path="knn")


class _Clock:
    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


class _StubBackend:
    """Answers each ``classify`` with the next queued result (or raises it), recording requests.

    With a ``clock``, each ``classify`` advances it by ``latency_s``. With ``delay_s``, each
    ``classify`` really sleeps that long first; ``cancelled`` records a call cancelled mid-sleep.
    """

    def __init__(
        self,
        clock: _Clock | None = None,
        latency_s: float = 0.0,
        *,
        name: str = "knn",
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self.results: list[BackendResult | Exception] = []
        self.requests: list[RouteRequest] = []
        self.deadlines_ms: list[int] = []
        self.configured: Sequence[ProfileSpec] = ()
        self.cancelled = False
        self._clock = clock
        self._latency_s = latency_s
        self.delay_s = delay_s

    def answer(
        self,
        profile_probs: Mapping[str, float],
        *,
        followup_prob: float | None = None,
        effort_probs: Mapping[Effort, float] | None = None,
        control_intent: ControlIntent | None = None,
    ) -> None:
        self.results.append(
            BackendResult(
                profile_probs=profile_probs,
                effort_probs=effort_probs,
                followup_prob=followup_prob,
                latency_ms=1,
                backend=self.name,
                control_intent=control_intent,
            )
        )

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        self.configured = profiles

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        self.requests.append(req)
        self.deadlines_ms.append(deadline_ms)
        if self.delay_s:
            try:
                await asyncio.sleep(self.delay_s)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self._clock is not None:
            self._clock.now_s += self._latency_s
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        pass


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def stub() -> _StubBackend:
    return _StubBackend()


@pytest.fixture
def router(stub: _StubBackend, clock: _Clock) -> CompositeRouter:
    return CompositeRouter(CONFIG, PROFILES, [stub], clock=clock)


async def _start_on_stock_picker(router: CompositeRouter, stub: _StubBackend) -> None:
    stub.answer({"stock-picker": 0.9, "general": 0.1})
    assert (await router.route("how's my portfolio", "rob")).profile == "stock-picker"


async def test_scenario_portfolio_then_sticky_follow_up_then_topic_switch(clock: _Clock) -> None:
    router = CompositeRouter(CONFIG, PROFILES, [KnnBackend(fake_embed)], clock=clock)
    await router.configure()

    first = await router.route("how is my portfolio doing", "rob")
    clock.now_s += 30
    follow_up = await router.route("and what about Tesla", "rob")
    clock.now_s += 30
    switch = await router.route("what's the weather tomorrow", "rob")

    assert (first.profile, first.source, first.sticky) == ("stock-picker", "knn", False)
    assert (follow_up.profile, follow_up.sticky) == ("stock-picker", True)
    assert (switch.profile, switch.source, switch.sticky) == ("general", "knn", False)


async def test_choices_always_include_every_profile_and_control(stub: _StubBackend) -> None:
    router = CompositeRouter(
        RouterConfig(backend="knn", control_examples=ControlExamples(status=("progress report",))),
        PROFILES,
        [stub],
    )

    await router.configure()

    assert [spec.name for spec in stub.configured] == ["stock-picker", "general", CONTROL_PROFILE]
    control = stub.configured[-1]
    assert isinstance(control, ControlSpec)
    assert control.description == "status or cancel of running voice tasks"
    assert control.examples == (
        "what are you working on",
        "any updates",
        "how's that task going",
        "progress report",
        "cancel that",
        "stop that task",
        "never mind",
    )
    assert control.intents["progress report"] == "status"
    assert control.intents["never mind"] == "cancel"


def test_rejects_a_default_profile_that_is_not_configured(stub: _StubBackend) -> None:
    with pytest.raises(ValueError, match="nowhere"):
        CompositeRouter(RouterConfig(backend="knn", default_profile="nowhere"), PROFILES, [stub])


async def test_backends_see_the_speakers_active_profile(router: CompositeRouter, stub: _StubBackend) -> None:
    await _start_on_stock_picker(router, stub)
    stub.answer({"general": 0.9, "stock-picker": 0.1})

    await router.route("set a reminder", "rob", history=("earlier line",), hint="general")

    assert stub.requests[0].active_profile is None
    assert stub.requests[1] == RouteRequest(
        text="set a reminder",
        speaker="rob",
        history=("earlier line",),
        active_profile="stock-picker",
        hint="general",
    )


async def test_a_profile_the_speaker_may_not_use_does_not_become_their_active_profile(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    def general_only(profile: str) -> bool:
        return profile == "general"

    stub.answer({"general": 0.9, "stock-picker": 0.1})
    await router.route("set a reminder", "alex", permitted=general_only)
    stub.answer({"stock-picker": 0.9, "general": 0.1})
    refused = await router.route("how's my portfolio", "alex", permitted=general_only)
    stub.answer({"general": 0.4, "stock-picker": 0.35})
    after = await router.route("hmm", "alex", permitted=general_only)

    assert refused.profile == "stock-picker", "the router still sees every profile"
    assert [request.active_profile for request in stub.requests] == [None, "general", "general"]
    assert (after.profile, after.sticky) == ("general", True)


async def test_active_profile_is_per_speaker(router: CompositeRouter, stub: _StubBackend) -> None:
    await _start_on_stock_picker(router, stub)
    stub.answer({"general": 0.4, "stock-picker": 0.35})

    decision = await router.route("hmm", "alex")

    assert (decision.profile, decision.sticky, decision.source) == ("general", False, "fallback")


async def test_sticky_profile_expires_after_idle(
    router: CompositeRouter, stub: _StubBackend, clock: _Clock
) -> None:
    await _start_on_stock_picker(router, stub)

    clock.now_s += 599
    stub.answer({"general": 0.4, "stock-picker": 0.3})
    before = await router.route("hmm", "rob")
    clock.now_s += 600
    stub.answer({"general": 0.4, "stock-picker": 0.3})
    after = await router.route("hmm", "rob")

    assert (before.profile, before.sticky, before.source) == ("stock-picker", True, "fallback")
    assert stub.requests[2].active_profile is None
    assert (after.profile, after.sticky, after.source) == ("general", False, "fallback")


async def test_sticky_idle_is_configurable(stub: _StubBackend, clock: _Clock) -> None:
    router = CompositeRouter(RouterConfig(backend="knn", sticky_idle_s=60), PROFILES, [stub], clock=clock)
    await _start_on_stock_picker(router, stub)

    clock.now_s += 60
    stub.answer({"general": 0.4, "stock-picker": 0.3})

    assert (await router.route("hmm", "rob")).profile == "general"


@pytest.mark.parametrize(
    ("followup_prob", "general", "stock_picker", "expected"),
    [
        (0.4, 0.5, 0.3, "general"),  # margin 0.2, not a follow-up: leaves
        (0.4, 0.5, 0.31, "stock-picker"),  # margin 0.19: stays
        (0.5, 0.75, 0.25, "stock-picker"),  # a follow-up: stays whatever the margin
        (None, 0.5, 0.2, "general"),  # no follow-up judgement, margin 0.3: leaves
        (None, 0.5, 0.21, "stock-picker"),  # margin 0.29: stays
    ],
)
async def test_sticky_thresholds(
    router: CompositeRouter,
    stub: _StubBackend,
    followup_prob: float | None,
    general: float,
    stock_picker: float,
    expected: str,
) -> None:
    await _start_on_stock_picker(router, stub)
    stub.answer({"general": general, "stock-picker": stock_picker}, followup_prob=followup_prob)

    decision = await router.route("next", "rob")

    assert decision.profile == expected
    if expected == "stock-picker":
        assert (decision.sticky, decision.source, decision.confidence) == (True, "sticky", stock_picker)
    else:
        assert (decision.sticky, decision.source, decision.confidence) == (False, "knn", general)
    assert decision.followup_prob == followup_prob


async def test_unconfident_backend_falls_back_to_the_default_profile(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({"stock-picker": 0.49, "general": 0.3})

    decision = await router.route("hmm", "rob")

    assert (decision.profile, decision.source, decision.sticky, decision.effort) == (
        "general",
        "fallback",
        False,
        "medium",
    )


async def test_failing_backend_falls_back_and_logs(
    router: CompositeRouter, stub: _StubBackend, caplog: pytest.LogCaptureFixture
) -> None:
    await _start_on_stock_picker(router, stub)
    stub.results.append(RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="hermes_duplex.router.composite"):
        decision = await router.route("next", "rob")

    assert (decision.profile, decision.source, decision.sticky) == ("stock-picker", "fallback", True)
    assert (decision.confidence, decision.probabilities, decision.effort) == (0.0, {}, "medium")
    assert "router backend knn failed" in caplog.text


async def test_deadline_is_passed_to_backends(stub: _StubBackend) -> None:
    stub.answer({"general": 1.0})

    await CompositeRouter(RouterConfig(backend="knn", deadline_ms=123), PROFILES, [stub]).route("x", "rob")

    assert stub.deadlines_ms == [123]


@pytest.mark.parametrize(("text", "intent"), [("any updates", "status"), ("cancel that", "cancel")])
async def test_control_requests_carry_their_intent_and_keep_the_active_profile(
    clock: _Clock, text: str, intent: str
) -> None:
    router = CompositeRouter(CONFIG, PROFILES, [KnnBackend(fake_embed)], clock=clock)
    await router.configure()
    await router.route("how is my portfolio doing", "rob")

    decision = await router.route(text, "rob")
    after = await router.route("zxq", "rob")

    assert (decision.profile, decision.control_intent, decision.model) == (CONTROL_PROFILE, intent, None)
    assert (decision.source, decision.sticky) == ("knn", False)
    assert (after.profile, after.sticky, after.control_intent) == ("stock-picker", True, None)


async def test_control_bypasses_the_sticky_rule(router: CompositeRouter, stub: _StubBackend) -> None:
    await _start_on_stock_picker(router, stub)
    stub.answer({CONTROL_PROFILE: 0.55, "stock-picker": 0.45}, control_intent="cancel")

    decision = await router.route("stop it", "rob")

    assert (decision.profile, decision.control_intent) == (CONTROL_PROFILE, "cancel")


async def test_control_without_an_intent_is_a_status_request(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({CONTROL_PROFILE: 0.9, "general": 0.1})

    decision = await router.route("what's happening", "rob")

    assert (decision.profile, decision.control_intent) == (CONTROL_PROFILE, "status")


@pytest.mark.parametrize(
    ("effort_probs", "effort", "model"),
    [
        ({"low": 0.1, "medium": 0.2, "high": 0.7}, "high", DEEP),  # table cell
        ({"low": 0.6, "medium": 0.3, "high": 0.1}, "low", FAST),  # table cell
        ({"low": 0.1, "medium": 0.8, "high": 0.1}, "medium", FALLBACK),  # missing cell → default_model
        (None, "medium", FALLBACK),  # no effort judgement → medium
    ],
)
async def test_model_table(
    router: CompositeRouter,
    stub: _StubBackend,
    effort_probs: Mapping[Effort, float] | None,
    effort: Effort,
    model: ModelChoice,
) -> None:
    stub.answer({"stock-picker": 0.9, "general": 0.1}, effort_probs=effort_probs)

    decision = await router.route("should I buy NVDA", "rob")

    assert (decision.effort, decision.model) == (effort, model)


async def test_profile_without_models_uses_the_hermes_profile_default(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({"general": 0.9, "stock-picker": 0.1}, effort_probs={"low": 0.0, "medium": 0.0, "high": 1.0})

    decision = await router.route("set a reminder", "rob")

    assert (decision.profile, decision.effort, decision.model) == ("general", "high", None)


async def test_logs_one_structured_line_per_decision(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    stub = _StubBackend(clock, latency_s=0.012)
    router = CompositeRouter(CONFIG, PROFILES, [stub], clock=clock)
    stub.answer(
        {"stock-picker": 0.7, "general": 0.2, CONTROL_PROFILE: 0.1},
        effort_probs={"low": 0.0, "medium": 0.0, "high": 1.0},
    )

    with caplog.at_level(logging.INFO, logger="hermes_duplex.router.composite"):
        decision = await router.route("should I buy NVDA", "rob")

    [record] = [r for r in caplog.records if r.name == "hermes_duplex.router.composite"]
    assert record.__dict__["route_decision"] == {
        "speaker": "rob",
        "profile": "stock-picker",
        "effort": "high",
        "model": "anthropic/deep-1",
        "confidence": 0.7,
        "top3": {"stock-picker": 0.7, "general": 0.2, CONTROL_PROFILE: 0.1},
        "source": "knn",
        "latency_ms": 12,
        "sticky": False,
    }
    assert decision.latency_ms == 12
    message = record.getMessage()
    for fragment in (
        "speaker=rob",
        "profile=stock-picker",
        "model=anthropic/deep-1",
        "latency_ms=12",
        "sticky=False",
    ):
        assert fragment in message


# The main path (a decision model) and the knn fast path, run together.

TWO_PATHS = RouterConfig(backend="decisions", fast_path="knn")
SHORT_DEADLINE = RouterConfig(backend="decisions", fast_path="knn", deadline_ms=20)
"""For tests that only need the main path to miss its deadline, not to measure it."""
SLOW_S = 1.0
"""How long a main path that misses the deadline takes."""


def _main(*, delay_s: float = 0.0) -> _StubBackend:
    return _StubBackend(name="decisions", delay_s=delay_s)


def _two_path_router(
    main: _StubBackend,
    fast: _StubBackend,
    config: RouterConfig = TWO_PATHS,
    clock: Callable[[], float] = time.monotonic,
) -> CompositeRouter:
    return CompositeRouter(config, PROFILES, [main, fast], clock=clock)


def test_rejects_a_configured_backend_that_was_not_given(stub: _StubBackend) -> None:
    with pytest.raises(ValueError, match="'decisions'"):
        CompositeRouter(TWO_PATHS, PROFILES, [stub])


async def test_configures_both_paths() -> None:
    main, fast = _main(), _StubBackend()

    await _two_path_router(main, fast).configure()

    assert [spec.name for spec in main.configured] == ["stock-picker", "general", CONTROL_PROFILE]
    assert main.configured == fast.configured


async def test_main_path_in_time_decides_with_its_effort() -> None:
    main, fast = _main(delay_s=0.02), _StubBackend()
    main.answer({"stock-picker": 0.8, "general": 0.2}, effort_probs={"low": 0.1, "medium": 0.1, "high": 0.8})
    fast.answer({"general": 0.7, "stock-picker": 0.3})

    decision = await _two_path_router(main, fast).route("should I buy NVDA", "rob")

    assert (decision.profile, decision.source, decision.effort, decision.model) == (
        "stock-picker",
        "decisions",
        "high",
        DEEP,
    )
    assert main.requests == fast.requests
    assert main.deadlines_ms == fast.deadlines_ms == [300]


async def test_decisions_timeout_uses_knn_within_the_deadline(caplog: pytest.LogCaptureFixture) -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"stock-picker": 0.7, "general": 0.3})  # confident, but not enough to skip the main path
    router = _two_path_router(main, fast)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="hermes_duplex.router.composite"):
        decision = await router.route("how's my portfolio", "rob")
    elapsed_ms = (time.monotonic() - started) * 1000

    assert (decision.profile, decision.source, decision.effort) == ("stock-picker", "knn", "medium")
    assert 290 <= decision.latency_ms < 350
    assert elapsed_ms < 350
    await asyncio.sleep(0)  # let the cancelled call unwind
    assert main.cancelled
    assert "router backend decisions missed the 300 ms deadline" in caplog.text


async def test_deadline_is_configurable() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"stock-picker": 0.7, "general": 0.3})

    decision = await _two_path_router(
        main, fast, RouterConfig(backend="decisions", fast_path="knn", deadline_ms=50)
    ).route("how's my portfolio", "rob")

    assert 45 <= decision.latency_ms < 150
    assert main.deadlines_ms == [50]


@pytest.mark.parametrize("winner", ["stock-picker", CONTROL_PROFILE])
async def test_sure_fast_path_is_accepted_without_waiting(winner: str) -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    other = "general"
    fast.answer({winner: 0.9, other: 0.1})

    decision = await _two_path_router(main, fast).route("how's my portfolio", "rob")

    assert (decision.profile, decision.source) == (winner, "knn")
    assert decision.latency_ms < 100
    await asyncio.sleep(0)  # let the cancelled call unwind
    assert main.cancelled


async def test_sure_fast_path_agreeing_with_the_active_profile_is_accepted_early() -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast)
    main.answer({"stock-picker": 0.9, "general": 0.1})
    fast.answer({"stock-picker": 0.9, "general": 0.1})
    await router.route("how's my portfolio", "rob")
    main.delay_s = SLOW_S
    fast.answer({"stock-picker": 0.9, "general": 0.1})

    decision = await router.route("and Tesla", "rob")

    assert (decision.profile, decision.source, decision.sticky) == ("stock-picker", "knn", True)
    assert decision.latency_ms < 100
    await asyncio.sleep(0)  # let the cancelled call unwind
    assert main.cancelled


@pytest.mark.parametrize(
    "fast_probs",
    [
        {"stock-picker": 0.84, "general": 0.16},  # not sure enough
        {"general": 0.9, "stock-picker": 0.1},  # sure, but leaves the active profile
    ],
)
async def test_fast_path_not_accepted_early_waits_for_the_main_path(fast_probs: Mapping[str, float]) -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast)
    main.answer({"stock-picker": 0.9, "general": 0.1})
    fast.answer({"stock-picker": 0.9, "general": 0.1})
    await router.route("how's my portfolio", "rob")
    main.delay_s = 0.05
    main.answer({"stock-picker": 0.8, "general": 0.2}, followup_prob=0.9)
    fast.answer(fast_probs)

    decision = await router.route("and that other one", "rob")

    assert (decision.profile, decision.source) == ("stock-picker", "decisions")
    assert not main.cancelled


async def test_unconfident_main_path_falls_to_knn_but_keeps_its_effort_and_follow_up() -> None:
    main, fast = _main(), _StubBackend()
    main.answer(
        {"stock-picker": 0.45, "general": 0.4},
        effort_probs={"low": 0.9, "medium": 0.05, "high": 0.05},
        followup_prob=0.2,
    )
    fast.answer({"stock-picker": 0.7, "general": 0.3})

    decision = await _two_path_router(main, fast).route("should I buy NVDA", "rob")

    assert (decision.profile, decision.source, decision.effort, decision.model) == (
        "stock-picker",
        "knn",
        "low",
        FAST,
    )
    assert (decision.followup_prob, decision.confidence) == (0.2, 0.7)


async def test_hint_is_used_when_the_main_path_times_out_and_knn_is_not_confident() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"general": 0.45, "stock-picker": 0.4})

    decision = await _two_path_router(main, fast, SHORT_DEADLINE).route("hmm", "rob", hint="stock-picker")

    assert (decision.profile, decision.source, decision.effort, decision.sticky) == (
        "stock-picker",
        "hint",
        "medium",
        False,
    )
    assert decision.confidence == 0.4


async def test_hint_is_ignored_when_knn_is_confident() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"general": 0.7, "stock-picker": 0.3})

    decision = await _two_path_router(main, fast, SHORT_DEADLINE).route("hmm", "rob", hint="stock-picker")

    assert (decision.profile, decision.source) == ("general", "knn")


async def test_hint_is_ignored_when_the_main_path_answers() -> None:
    main, fast = _main(), _StubBackend()
    main.answer({"general": 0.8, "stock-picker": 0.2})
    fast.answer({"general": 0.45, "stock-picker": 0.4})

    decision = await _two_path_router(main, fast).route("hmm", "rob", hint="stock-picker")

    assert (decision.profile, decision.source) == ("general", "decisions")


async def test_hint_that_is_not_a_profile_is_ignored() -> None:
    main, fast = _main(), _StubBackend()
    main.results.append(RuntimeError("boom"))
    fast.answer({"general": 0.45, "stock-picker": 0.4})

    decision = await _two_path_router(main, fast).route("hmm", "rob", hint="nowhere")

    assert (decision.profile, decision.source) == ("general", "fallback")


async def test_all_paths_failing_fall_back_to_the_active_profile_else_the_default() -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast)
    main.results.append(RuntimeError("boom"))
    fast.results.append(RuntimeError("boom"))
    first = await router.route("hmm", "rob")
    main.answer({"stock-picker": 0.9, "general": 0.1})
    fast.answer({"stock-picker": 0.9, "general": 0.1})
    await router.route("how's my portfolio", "rob")
    main.results.append(RuntimeError("boom"))
    fast.results.append(RuntimeError("boom"))

    second = await router.route("hmm", "rob")

    assert (first.profile, first.source, first.effort, first.sticky) == (
        "general",
        "fallback",
        "medium",
        False,
    )
    assert (second.profile, second.source, second.effort, second.sticky) == (
        "stock-picker",
        "fallback",
        "medium",
        True,
    )
    assert (second.confidence, second.probabilities) == (0.0, {})


async def test_main_path_follow_up_judges_a_confident_knn_switch() -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast)
    for speaker in ("rob", "alex"):
        main.answer({"stock-picker": 0.9, "general": 0.1})
        fast.answer({"stock-picker": 0.9, "general": 0.1})
        await router.route("how's my portfolio", speaker)

    # knn alone (no follow-up judgement) would need a 0.3 margin to leave. With the main path's
    # follow-up probability, 0.25 is enough for a new topic, and no margin is enough for a follow-up.
    main.answer({"general": 0.45, "stock-picker": 0.4}, followup_prob=0.1)
    fast.answer({"general": 0.6, "stock-picker": 0.35})
    leaves = await router.route("what's the weather", "rob")
    main.answer({"general": 0.45, "stock-picker": 0.4}, followup_prob=0.9)
    fast.answer({"general": 0.6, "stock-picker": 0.35})
    stays = await router.route("and the weather there", "alex")

    assert (leaves.profile, leaves.source, leaves.sticky, leaves.followup_prob) == (
        "general",
        "knn",
        False,
        0.1,
    )
    assert (stays.profile, stays.source, stays.sticky, stays.followup_prob) == (
        "stock-picker",
        "sticky",
        True,
        0.9,
    )


async def test_circuit_breaker_skips_the_main_path_for_30_s_after_3_failures(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast, clock=clock)
    main.results.extend([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    for _ in range(4):
        fast.answer({"general": 0.7, "stock-picker": 0.3})

    with caplog.at_level(logging.INFO, logger="hermes_duplex.router.composite"):
        for _ in range(3):
            await router.route("hmm", "rob")
        assert "circuit breaker open: skipping decisions for 30 s after 3 consecutive failures" in caplog.text

        clock.now_s += 29.9
        skipped = await router.route("hmm", "rob")
        assert len(main.requests) == 3
        assert (skipped.profile, skipped.source) == ("general", "knn")

        clock.now_s += 0.1
        main.answer({"stock-picker": 0.9, "general": 0.1})
        fast.answer({"general": 0.7, "stock-picker": 0.3})
        back = await router.route("how's my portfolio", "rob")

    assert len(main.requests) == 4
    assert (back.profile, back.source) == ("stock-picker", "decisions")
    assert "circuit breaker closed: asking decisions again" in caplog.text


async def test_circuit_breaker_counts_deadline_misses() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    router = _two_path_router(main, fast, SHORT_DEADLINE)
    for _ in range(4):
        fast.answer({"general": 0.7, "stock-picker": 0.3})

    for _ in range(4):
        await router.route("hmm", "rob")

    assert len(main.requests) == 3


async def test_circuit_breaker_needs_consecutive_failures() -> None:
    main, fast = _main(), _StubBackend()
    router = _two_path_router(main, fast)
    main.results.extend([RuntimeError("boom"), RuntimeError("boom")])
    main.answer({"general": 0.9, "stock-picker": 0.1})
    main.results.extend([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    for _ in range(6):
        fast.answer({"general": 0.7, "stock-picker": 0.3})

    for _ in range(6):
        await router.route("hmm", "rob")

    assert len(main.requests) == 6


async def test_early_accept_does_not_count_against_the_main_path() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    router = _two_path_router(main, fast)
    for _ in range(4):
        fast.answer({"general": 0.9, "stock-picker": 0.1})

    for _ in range(4):
        await router.route("set a reminder", "rob")

    assert len(main.requests) == 4
