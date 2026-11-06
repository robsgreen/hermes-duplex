"""CompositeRouter: fast and main paths, deadline, circuit breaker, fallbacks, ``_control``, the effort
and model of the active profile, switch suggestions, and the decision log."""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

import pytest

from hermes_duplex.config import CONTROL_PROFILE, SWITCH_PROFILE, ControlExamples, ProfileConfig, RouterConfig
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.router.base import (
    BackendResult,
    ControlIntent,
    ControlSpec,
    Effort,
    ProfileSpec,
    RouteDecision,
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
        effort_probs: Mapping[Effort, float] | None = None,
        control_intent: ControlIntent | None = None,
        control_similarity: float | None = None,
    ) -> None:
        self.results.append(
            BackendResult(
                profile_probs=profile_probs,
                effort_probs=effort_probs,
                latency_ms=1,
                backend=self.name,
                control_intent=control_intent,
                control_similarity=control_similarity,
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


async def _route(
    router: CompositeRouter,
    text: str = "hmm",
    speaker: str = "rob",
    *,
    active: str = "stock-picker",
    permitted: Callable[[str], bool] | None = None,
    history: Sequence[str] = (),
) -> RouteDecision:
    return await router.route(text, speaker, active_profile=active, permitted=permitted, history=history)


async def test_choices_always_include_every_profile_control_and_switch(stub: _StubBackend) -> None:
    router = CompositeRouter(
        RouterConfig(backend="knn", control_examples=ControlExamples(status=("progress report",))),
        PROFILES,
        [stub],
    )

    await router.configure()

    assert [spec.name for spec in stub.configured] == [
        "stock-picker",
        "general",
        CONTROL_PROFILE,
        SWITCH_PROFILE,
    ]
    control = stub.configured[-2]
    assert isinstance(control, ControlSpec)
    assert control.description == "progress of a task already running, or cancelling it"
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
    switch = stub.configured[-1]
    assert switch.description == "changing which agent or profile the user talks to"
    assert "go to the stock picker profile" in switch.examples, "templates name each profile"
    assert "can I talk to the general agent" in switch.examples
    assert "switch agents" in switch.examples


async def test_backends_see_the_speakers_active_profile(router: CompositeRouter, stub: _StubBackend) -> None:
    stub.answer({"general": 0.9, "stock-picker": 0.1})

    await _route(router, "set a reminder", history=("earlier line",), active="stock-picker")

    assert stub.requests == [
        RouteRequest(
            text="set a reminder", speaker="rob", history=("earlier line",), active_profile="stock-picker"
        )
    ]


@pytest.mark.parametrize(
    ("winner", "effort_probs", "effort", "model"),
    [
        ("stock-picker", {"low": 0.1, "medium": 0.2, "high": 0.7}, "high", DEEP),  # table cell
        ("stock-picker", {"low": 0.6, "medium": 0.3, "high": 0.1}, "low", FAST),  # table cell
        ("stock-picker", {"low": 0.1, "medium": 0.8, "high": 0.1}, "medium", FALLBACK),  # → default_model
        ("stock-picker", None, "medium", FALLBACK),  # no effort judgement → medium
        ("general", {"low": 0.1, "medium": 0.2, "high": 0.7}, "high", DEEP),  # the active profile's table
    ],
)
async def test_model_comes_from_the_active_profiles_table(
    router: CompositeRouter,
    stub: _StubBackend,
    winner: str,
    effort_probs: Mapping[Effort, float] | None,
    effort: Effort,
    model: ModelChoice,
) -> None:
    stub.answer(
        {winner: 0.6, "stock-picker" if winner == "general" else "general": 0.4}, effort_probs=effort_probs
    )

    decision = await _route(router, "should I buy NVDA", active="stock-picker")

    assert (decision.effort, decision.model, decision.source) == (effort, model, "knn")


async def test_active_profile_without_models_uses_the_hermes_profile_default(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({"general": 0.9, "stock-picker": 0.1}, effort_probs={"low": 0.0, "medium": 0.0, "high": 1.0})

    decision = await _route(router, "set a reminder", active="general")

    assert (decision.effort, decision.model, decision.suggestion) == ("high", None, None)


@pytest.mark.parametrize(
    ("probs", "suggestion"),
    [
        ({"general": 0.9, "stock-picker": 0.1}, "general"),  # confident other profile
        ({"general": 0.85, "stock-picker": 0.15}, "general"),  # exactly the threshold
        ({"general": 0.84, "stock-picker": 0.16}, None),  # below the threshold
        ({"general": 0.9, "stock-picker": 0.7}, "general"),  # beats the active profile by 0.2
        ({"general": 0.9, "stock-picker": 0.71}, None),  # beats it by less than 0.2
        ({"stock-picker": 0.95, "general": 0.05}, None),  # the active profile itself
    ],
)
async def test_suggests_another_profile_only_when_clearly_better(
    router: CompositeRouter, stub: _StubBackend, probs: Mapping[str, float], suggestion: str | None
) -> None:
    stub.answer(probs)

    decision = await _route(router, "what's the weather", active="stock-picker")

    assert decision.suggestion == suggestion
    assert decision.model == FALLBACK, "the request still runs on the active profile"


async def test_suggestions_can_be_turned_off(stub: _StubBackend) -> None:
    router = CompositeRouter(RouterConfig(backend="knn", suggest_switch=False), PROFILES, [stub])
    stub.answer({"general": 0.99, "stock-picker": 0.01})

    assert (await _route(router, "what's the weather")).suggestion is None


async def test_suggest_threshold_is_configurable(stub: _StubBackend) -> None:
    router = CompositeRouter(RouterConfig(backend="knn", suggest_threshold=0.6), PROFILES, [stub])
    stub.answer({"general": 0.65, "stock-picker": 0.35})

    assert (await _route(router, "what's the weather")).suggestion == "general"


async def test_never_suggests_a_profile_the_speaker_may_not_use(stub: _StubBackend) -> None:
    profiles = {**PROFILES, "news": ProfileConfig(description="Headlines")}
    router = CompositeRouter(RouterConfig(backend="knn", suggest_threshold=0.5), profiles, [stub])

    def no_stocks(profile: str) -> bool:
        return profile != "stock-picker"

    stub.answer({"stock-picker": 0.9, "news": 0.05, "general": 0.05})
    refused = await _route(router, "how's my portfolio", "alex", active="general", permitted=no_stocks)
    stub.answer({"stock-picker": 0.35, "news": 0.6, "general": 0.05})
    next_best = await _route(router, "any headlines", "alex", active="general", permitted=no_stocks)

    assert refused.suggestion is None
    assert next_best.suggestion == "news"


@pytest.mark.parametrize(
    "probs",
    [{"general": 0.49, "stock-picker": 0.3}, {CONTROL_PROFILE: 0.49, "general": 0.3}],
    ids=["profile", "control"],
)
async def test_unconfident_backend_falls_back_without_a_suggestion(
    router: CompositeRouter, stub: _StubBackend, probs: Mapping[str, float]
) -> None:
    stub.answer(probs)

    decision = await _route(router, active="stock-picker")

    assert (
        decision.source,
        decision.effort,
        decision.model,
        decision.suggestion,
        decision.control_intent,
    ) == (
        "fallback",
        "medium",
        FALLBACK,
        None,
        None,
    )


async def test_failing_backend_falls_back_and_logs(
    router: CompositeRouter, stub: _StubBackend, caplog: pytest.LogCaptureFixture
) -> None:
    stub.results.append(RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="hermes_duplex.router.composite"):
        decision = await _route(router, "next")

    assert (decision.source, decision.probabilities, decision.effort, decision.model) == (
        "fallback",
        {},
        "medium",
        FALLBACK,
    )
    assert "router backend knn failed" in caplog.text


async def test_deadline_is_passed_to_backends(stub: _StubBackend) -> None:
    stub.answer({"general": 1.0})

    await _route(CompositeRouter(RouterConfig(backend="knn", deadline_ms=123), PROFILES, [stub]), "x")

    assert stub.deadlines_ms == [123]


@pytest.mark.parametrize(("text", "intent"), [("any updates", "status"), ("cancel that", "cancel")])
async def test_control_requests_carry_their_intent(clock: _Clock, text: str, intent: str) -> None:
    router = CompositeRouter(CONFIG, PROFILES, [KnnBackend(fake_embed)], clock=clock)
    await router.configure()

    decision = await _route(router, text, active="general")

    assert (decision.control_intent, decision.model, decision.suggestion, decision.source) == (
        intent,
        None,
        None,
        "knn",
    )


async def test_control_takes_the_intent_the_backend_tells(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({CONTROL_PROFILE: 0.996, "general": 0.004}, control_intent="cancel")

    decision = await _route(router, "stop it")

    assert (decision.control_intent, decision.suggestion) == ("cancel", None)


async def test_control_without_an_intent_is_a_status_request(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({CONTROL_PROFILE: 0.999, "general": 0.001})

    assert (await _route(router, "what's happening")).control_intent == "status"


@pytest.mark.parametrize(
    ("control_prob", "intent"),
    [
        (0.995, "cancel"),  # exactly the bar
        (0.994, None),  # just below it
        (0.9, None),  # the top choice, and confident, but not strong evidence
    ],
)
async def test_control_needs_strong_evidence_else_the_request_is_a_task(
    router: CompositeRouter, stub: _StubBackend, control_prob: float, intent: ControlIntent | None
) -> None:
    stub.answer(
        {CONTROL_PROFILE: control_prob, "general": 1 - control_prob, "stock-picker": 0.0},
        control_intent="cancel",
    )

    decision = await _route(router, "stop it", active="stock-picker")

    assert (decision.control_intent, decision.suggestion, decision.source) == (intent, None, "knn")
    assert decision.model == (None if intent is not None else FALLBACK)


@pytest.mark.parametrize(
    ("switch_prob", "switch_request"),
    [
        (0.95, True),  # exactly the bar
        (0.949, False),  # just below it
        (0.9, False),  # the top choice, and confident, but not strong evidence
    ],
)
async def test_a_switch_request_needs_strong_evidence_else_the_request_is_a_task(
    router: CompositeRouter, stub: _StubBackend, switch_prob: float, switch_request: bool
) -> None:
    stub.answer({SWITCH_PROFILE: switch_prob, "general": 1 - switch_prob, "stock-picker": 0.0})

    decision = await _route(router, "go to the stock trader profile", active="stock-picker")

    assert (decision.switch_request, decision.control_intent, decision.source) == (
        switch_request,
        None,
        "knn",
    )
    assert decision.suggestion is None
    assert decision.model == (None if switch_request else FALLBACK)


async def test_a_control_answer_strong_enough_wins_over_a_switch(
    router: CompositeRouter, stub: _StubBackend
) -> None:
    stub.answer({CONTROL_PROFILE: 0.999, SWITCH_PROFILE: 0.001}, control_intent="status")

    decision = await _route(router, "what are you working on", active="general")

    assert (decision.control_intent, decision.switch_request) == ("status", False)


async def test_fast_path_switch_waits_for_the_main_path() -> None:
    main, fast = _main(delay_s=0.05), _StubBackend()
    main.answer({SWITCH_PROFILE: 0.99, "general": 0.01})
    fast.answer({SWITCH_PROFILE: 0.999, "general": 0.001})

    decision = await _route(_two_path_router(main, fast), "use my portfolio agent", active="general")

    assert (decision.source, decision.switch_request) == ("decisions", True)
    assert not main.cancelled


async def test_profiles_with_only_a_description_still_run_tasks(clock: _Clock) -> None:
    """The live test on picard: with profiles that had no examples, every hand-off was answered as a
    status question. The main path leaned to ``_control`` for tasks; the ``knn`` fast path only knows
    each profile's description but matches the built-in status and cancel phrases."""
    profiles = {
        "general": ProfileConfig(description="Everyday help: questions and errands"),
        "stock-picker": ProfileConfig(description="Portfolio, holdings, stock research, trades"),
    }
    main = _main(delay_s=0.02)
    fast = KnnBackend(fake_embed)
    router = CompositeRouter(TWO_PATHS, profiles, [main, fast], clock=clock)
    await router.configure()
    # What a main path sees with descriptions alone: ``_control`` on top, but not sure of it.
    main.answer({CONTROL_PROFILE: 0.9, "general": 0.08, "stock-picker": 0.02})
    main.answer({CONTROL_PROFILE: 0.7, "general": 0.2, "stock-picker": 0.1})

    weather = await _route(router, "what's the weather tomorrow", active="general")
    reminder = await _route(router, "remind me to call mom", active="general")
    status = await _route(router, "what are you working on", active="general")
    cancel = await _route(router, "cancel that", active="general")

    assert (weather.control_intent, weather.source) == (None, "decisions")
    assert (reminder.control_intent, reminder.source) == (None, "decisions")
    # The two control phrases match built-in ones exactly, so the fast path settles them.
    assert (status.control_intent, status.source) == ("status", "knn")
    assert (cancel.control_intent, cancel.source) == ("cancel", "knn")
    assert len(main.requests) == 4 and not main.results


async def test_logs_one_structured_line_per_decision(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    stub = _StubBackend(clock, latency_s=0.012)
    router = CompositeRouter(CONFIG, PROFILES, [stub], clock=clock)
    stub.answer(
        {"general": 0.9, "stock-picker": 0.05, CONTROL_PROFILE: 0.05},
        effort_probs={"low": 0.0, "medium": 0.0, "high": 1.0},
    )

    with caplog.at_level(logging.INFO, logger="hermes_duplex.router.composite"):
        decision = await _route(router, "what's the weather", active="stock-picker")

    [record] = [r for r in caplog.records if r.name == "hermes_duplex.router.composite"]
    assert record.__dict__["route_decision"] == {
        "speaker": "rob",
        "profile": "stock-picker",
        "control": None,
        "switch": False,
        "effort": "high",
        "model": "anthropic/deep-1",
        "suggestion": "general",
        "top3": {"general": 0.9, "stock-picker": 0.05, CONTROL_PROFILE: 0.05},
        "source": "knn",
        "latency_ms": 12,
    }
    assert decision.latency_ms == 12
    message = record.getMessage()
    for fragment in (
        "speaker=rob",
        "profile=stock-picker",
        "model=anthropic/deep-1",
        "suggestion=general",
        "latency_ms=12",
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

    assert [spec.name for spec in main.configured] == [
        "stock-picker",
        "general",
        CONTROL_PROFILE,
        SWITCH_PROFILE,
    ]
    assert main.configured == fast.configured


async def test_main_path_in_time_decides_with_its_effort() -> None:
    main, fast = _main(delay_s=0.02), _StubBackend()
    main.answer({"stock-picker": 0.8, "general": 0.2}, effort_probs={"low": 0.1, "medium": 0.1, "high": 0.8})
    fast.answer({"general": 0.7, "stock-picker": 0.3})

    decision = await _route(_two_path_router(main, fast), "should I buy NVDA")

    assert (decision.source, decision.effort, decision.model, decision.suggestion) == (
        "decisions",
        "high",
        DEEP,
        None,
    )
    assert main.requests == fast.requests
    assert main.deadlines_ms == fast.deadlines_ms == [300]


async def test_decisions_timeout_uses_knn_within_the_deadline(caplog: pytest.LogCaptureFixture) -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"stock-picker": 0.7, "general": 0.3})  # confident, but not enough to skip the main path
    router = _two_path_router(main, fast)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="hermes_duplex.router.composite"):
        decision = await _route(router, "how's my portfolio")
    elapsed_ms = (time.monotonic() - started) * 1000

    assert (decision.source, decision.effort) == ("knn", "medium")
    assert 290 <= decision.latency_ms < 350
    assert elapsed_ms < 350
    await asyncio.sleep(0)  # let the cancelled call unwind
    assert main.cancelled
    assert "router backend decisions missed the 300 ms deadline" in caplog.text


async def test_deadline_is_configurable() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({"stock-picker": 0.7, "general": 0.3})

    decision = await _route(
        _two_path_router(main, fast, RouterConfig(backend="decisions", fast_path="knn", deadline_ms=50)),
        "how's my portfolio",
    )

    assert 45 <= decision.latency_ms < 150
    assert main.deadlines_ms == [50]


@pytest.mark.parametrize(
    ("fast_probs", "similarity", "intent"),
    [
        ({"stock-picker": 0.9, "general": 0.1}, None, None),  # sure of the active profile
        ({CONTROL_PROFILE: 0.999, "general": 0.001}, 0.95, "cancel"),  # a near-exact control phrase
        ({CONTROL_PROFILE: 0.999, "general": 0.001}, 0.9, "cancel"),  # exactly the similarity bar
    ],
)
async def test_sure_fast_path_for_the_active_profile_or_an_exact_control_phrase_is_accepted_without_waiting(
    fast_probs: Mapping[str, float], similarity: float | None, intent: ControlIntent | None
) -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer(fast_probs, control_intent="cancel", control_similarity=similarity)

    decision = await _route(_two_path_router(main, fast), "how's my portfolio", active="stock-picker")

    assert (decision.source, decision.suggestion, decision.control_intent) == ("knn", None, intent)
    assert decision.latency_ms < 100
    await asyncio.sleep(0)  # let the cancelled call unwind
    assert main.cancelled


@pytest.mark.parametrize(
    ("fast_probs", "similarity"),
    [
        ({CONTROL_PROFILE: 0.999, "general": 0.001}, 0.89),  # strong, but not a near-exact match
        ({CONTROL_PROFILE: 0.999, "general": 0.001}, None),  # a backend that measures no similarity
        ({CONTROL_PROFILE: 0.9, "general": 0.1}, 0.99),  # an exact match, but weak evidence
    ],
)
async def test_fast_path_control_waits_for_the_main_path(
    fast_probs: Mapping[str, float], similarity: float | None
) -> None:
    main, fast = _main(delay_s=0.05), _StubBackend()
    main.answer({"stock-picker": 0.8, "general": 0.2})
    fast.answer(fast_probs, control_similarity=similarity)

    decision = await _route(_two_path_router(main, fast), "is my portfolio done", active="stock-picker")

    assert (decision.source, decision.control_intent, decision.model) == ("decisions", None, FALLBACK)
    assert not main.cancelled


@pytest.mark.parametrize(("control_prob", "intent"), [(0.999, "status"), (0.9, None)])
async def test_after_a_main_path_timeout_only_a_strong_fast_path_control_answer_counts(
    control_prob: float, intent: ControlIntent | None
) -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    fast.answer({CONTROL_PROFILE: control_prob, "general": 1 - control_prob}, control_similarity=0.5)

    decision = await _route(_two_path_router(main, fast, SHORT_DEADLINE), "is it done", active="general")

    assert (decision.source, decision.control_intent) == ("knn", intent)


@pytest.mark.parametrize(
    "fast_probs",
    [
        {"stock-picker": 0.84, "general": 0.16},  # not sure enough
        {"general": 0.9, "stock-picker": 0.1},  # sure, but of another profile: a suggestion waits
    ],
)
async def test_fast_path_not_accepted_early_waits_for_the_main_path(fast_probs: Mapping[str, float]) -> None:
    main, fast = _main(delay_s=0.05), _StubBackend()
    main.answer({"stock-picker": 0.8, "general": 0.2})
    fast.answer(fast_probs)

    decision = await _route(_two_path_router(main, fast), "and that other one", active="stock-picker")

    assert (decision.source, decision.suggestion) == ("decisions", None)
    assert not main.cancelled


async def test_main_path_suggestion_after_a_sure_fast_path_for_another_profile() -> None:
    main, fast = _main(delay_s=0.02), _StubBackend()
    main.answer({"general": 0.9, "stock-picker": 0.1})
    fast.answer({"general": 0.9, "stock-picker": 0.1})

    decision = await _route(_two_path_router(main, fast), "what's the weather", active="stock-picker")

    assert (decision.source, decision.suggestion) == ("decisions", "general")


async def test_unconfident_main_path_falls_to_knn_but_keeps_its_effort() -> None:
    main, fast = _main(), _StubBackend()
    main.answer(
        {"stock-picker": 0.45, "general": 0.4}, effort_probs={"low": 0.9, "medium": 0.05, "high": 0.05}
    )
    fast.answer({"stock-picker": 0.7, "general": 0.3})

    decision = await _route(_two_path_router(main, fast), "should I buy NVDA")

    assert (decision.source, decision.effort, decision.model) == ("knn", "low", FAST)
    assert decision.probabilities == {"stock-picker": 0.7, "general": 0.3}


async def test_every_path_failing_gives_the_default_effort_on_the_active_profile() -> None:
    main, fast = _main(), _StubBackend()
    main.results.append(RuntimeError("boom"))
    fast.results.append(RuntimeError("boom"))

    decision = await _route(_two_path_router(main, fast), active="stock-picker")

    assert (decision.source, decision.effort, decision.model, decision.suggestion) == (
        "fallback",
        "medium",
        FALLBACK,
        None,
    )
    assert (decision.control_intent, decision.probabilities) == (None, {})


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
            await _route(router)
        assert "circuit breaker open: skipping decisions for 30 s after 3 consecutive failures" in caplog.text

        clock.now_s += 29.9
        skipped = await _route(router)
        assert len(main.requests) == 3
        assert skipped.source == "knn"

        clock.now_s += 0.1
        main.answer({"stock-picker": 0.9, "general": 0.1})
        fast.answer({"general": 0.7, "stock-picker": 0.3})
        back = await _route(router, "how's my portfolio")

    assert len(main.requests) == 4
    assert back.source == "decisions"
    assert "circuit breaker closed: asking decisions again" in caplog.text


async def test_circuit_breaker_counts_deadline_misses() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    router = _two_path_router(main, fast, SHORT_DEADLINE)
    for _ in range(4):
        fast.answer({"general": 0.7, "stock-picker": 0.3})

    for _ in range(4):
        await _route(router)

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
        await _route(router)

    assert len(main.requests) == 6


async def test_early_accept_does_not_count_against_the_main_path() -> None:
    main, fast = _main(delay_s=SLOW_S), _StubBackend()
    router = _two_path_router(main, fast)
    for _ in range(4):
        fast.answer({"general": 0.9, "stock-picker": 0.1})

    for _ in range(4):
        await _route(router, "set a reminder", active="general")

    assert len(main.requests) == 4


class _RaisesWhenCancelled(_StubBackend):
    """A main path whose own timeout fires as the router gives up: cancelling it ends the call with
    an error instead of a cancellation, as aiohttp's timeout turns Decisions' cancelled request into
    ``DecisionsError``."""

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        try:
            await asyncio.sleep(SLOW_S)
        except asyncio.CancelledError:
            raise RuntimeError("did not answer within the deadline") from None
        return await super().classify(req, deadline_ms)


async def test_a_main_path_that_errors_after_the_deadline_leaves_no_unretrieved_error() -> None:
    import gc

    unretrieved: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        main, fast = _RaisesWhenCancelled(name="decisions"), _StubBackend()
        fast.answer({"stock-picker": 0.7, "general": 0.3})
        router = _two_path_router(main, fast, SHORT_DEADLINE)

        await _route(router, "should I buy NVDA")
        for _ in range(5):  # let the abandoned call end and its done callbacks run
            await asyncio.sleep(0)
        gc.collect()

        assert not router._abandoned  # pyright: ignore[reportPrivateUsage]
        assert unretrieved == []
    finally:
        loop.set_exception_handler(None)
