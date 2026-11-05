"""VoiceSession switch suggestions: a request the router finds another profile better for is held while
the speaker is asked whether to switch, and their answer, a newer request, the timeout, or the end of
the session settles it. With the fake provider and the fake Hermes API."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex import service
from hermes_duplex.audio.frames import silent_frame
from hermes_duplex.config import Config, load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.prompts.notices import NO_QUESTION_PENDING, suggest_switch, switched_to
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.fake.adapter import FakeProvider, SentCall
from hermes_duplex.router.base import BackendResult, Effort, ProfileSpec, RouteDecision, RouteRequest
from hermes_duplex.router.composite import CompositeRouter
from hermes_duplex.session import QUESTION_TIMEOUT_S, Router, VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_hermes_api import FakeHermesApi, RecordedRequest

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
MODELS = {
    "general": ModelChoice(provider="openai", model="general-medium"),
    "stock-picker": ModelChoice(provider="openai", model="stocks-medium"),
}
"""Each profile's model for a medium-effort run, as the stub router looks it up."""

NVDA = "what did NVDA do today"
WEATHER = "what's the weather"
TICKERS = ("NVDA", "AMD")
"""The stub router suggests stock-picker for a request naming one of these (unless it is active)."""


class _MemoryConversation:
    """A ``Conversation`` that yields scripted frames, then waits until it is closed."""

    id = "memory-1"
    person: str | None = None

    def __init__(self, frames: Sequence[InputFrame]) -> None:
        self._frames = frames
        self._closed = asyncio.Event()

    async def frames(self) -> AsyncIterator[InputFrame]:
        for frame in self._frames:
            yield frame
        await self._closed.wait()

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        pass

    def present(self, person: str) -> bool:
        return True

    async def flush_playback(self) -> None:
        pass

    async def notify(self, event: dict[str, Any]) -> None:
        pass

    async def close(self, reason: str) -> None:
        self._closed.set()


class _SuggestingRouter:
    """Routes every request at medium effort, suggesting stock-picker for one that names ``TICKERS``, and records
    each request with the speaker's active profile. ``release``, when set, holds each answer until the
    test sets it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.release: asyncio.Event | None = None

    def model_for(self, profile: str, effort: Effort) -> ModelChoice | None:
        assert effort == "medium"
        return MODELS[profile]

    async def route(
        self,
        text: str,
        speaker: str,
        *,
        active_profile: str,
        permitted: Callable[[str], bool] | None = None,
        history: Sequence[str] = (),
    ) -> RouteDecision:
        self.requests.append((text, active_profile))
        if self.release is not None:
            await self.release.wait()
        suggested = active_profile != "stock-picker" and any(ticker in text for ticker in TICKERS)
        return RouteDecision(
            effort="medium",
            model=self.model_for(active_profile, "medium"),
            suggestion="stock-picker" if suggested else None,
            control_intent=None,
            probabilities={},
            source="knn",
            latency_ms=1,
        )


class _FixedBackend:
    """A router backend sure that every request is a stock-picker one."""

    name = "knn"

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        pass

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        probs = {"stock-picker": 0.95, "general": 0.04, "_control": 0.01}
        return BackendResult(profile_probs=probs, effort_probs=None, latency_ms=1, backend=self.name)

    async def close(self) -> None:
        pass


class _Sleep:
    """The session's ``sleep``: a question's timeout waits until ``expire_holds`` is set; any other
    sleep is real."""

    def __init__(self) -> None:
        self.expire_holds = asyncio.Event()
        self.requested_s: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.requested_s.append(seconds)
        if seconds == QUESTION_TIMEOUT_S:
            await self.expire_holds.wait()
        else:
            await asyncio.sleep(seconds)


def _frames(duration_ms: int, person: str) -> list[InputFrame]:
    """20 ms silent frames covering ``duration_ms``, each voiced by ``person``."""
    return [
        InputFrame(silent_frame(RATE), RATE, frozenset({person}), t_ms) for t_ms in range(0, duration_ms, 20)
    ]


def _call(call_id: str, target: str, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "delegation", "id": call_id, "target": target, "tool_args": args}


def _handoff(call_id: str, task: str) -> dict[str, Any]:
    """The first step: the model hands off ``task``."""
    return {"at_input_ms": 500, "event": _call(call_id, "delegate", {"task": task})}


def _after_the_question(event: dict[str, Any]) -> dict[str, Any]:
    """A step that answers once the session has said something: the switch question."""
    return {"on_sent": "say", "event": event}


def _config(tmp_path: Path, *, suggest_switch: bool = True) -> Config:
    """Rob may use every profile and starts on general, the router default; Alex may use only general."""
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn", "suggest_switch": suggest_switch},
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather"]},
            "stock-picker": {
                "description": "Portfolio, holdings, stock research, trades",
                "examples": ["how's my portfolio"],
            },
        },
        "people": {
            "rob": {"discord_id": "123", "profiles": ["*"]},
            "alex": {"discord_id": "456", "profiles": ["general"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={})


class _Harness:
    """A session on the fake provider, a router (the suggesting stub unless replaced), and the fake
    Hermes API."""

    def __init__(self, tmp_path: Path, api: FakeHermesApi) -> None:
        self.api = api
        self.config = _config(tmp_path)
        self.permissions = PermissionChecker(self.config)
        self.active_profiles = ActiveProfiles(
            tmp_path / "home" / "duplex" / "active_profiles.json", self.permissions, self.config
        )
        self.router: Router = _SuggestingRouter()
        self.sleep = _Sleep()
        self.tracker = TaskTracker(tmp_path / "home")
        self.provider: FakeProvider | None = None
        self._conversation: _MemoryConversation | None = None
        self._hermes: HermesApiClient | None = None
        self._run: asyncio.Task[None] | None = None

    def start(
        self,
        script: Iterable[Mapping[str, Any]],
        *,
        speaker: str = "rob",
        provider: FakeProvider | None = None,
    ) -> None:
        self.provider = provider or FakeProvider(script)
        self._conversation = _MemoryConversation(_frames(1000, speaker))
        self._hermes = HermesApiClient(self.api.api_base, KEYS.get)
        session = VoiceSession(
            self._conversation,
            self.provider,
            router=self.router,
            hermes=self._hermes,
            tracker=self.tracker,
            permissions=self.permissions,
            active_profiles=self.active_profiles,
            prompt_context_for=lambda profile: service.prompt_context(self.config, profile),
            default_profile=self.config.router.default_profile,
            sleep=self.sleep,
        )
        self._run = asyncio.create_task(session.run())

    async def end_session(self) -> None:
        """End the conversation and wait for the session to wind down."""
        if self._conversation is not None and self._run is not None:
            await self._conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
            self._run = None

    async def stop(self) -> None:
        await self.end_session()
        if self._hermes is not None:
            await self._hermes.close()
            self._hermes = None
        await self.tracker.close()

    def sent(self, method: str) -> list[SentCall]:
        assert self.provider is not None
        return [call for call in self.provider.calls if call.method == method]

    def said(self) -> list[str]:
        return [call.args["text"] for call in self.sent("say")]

    def outputs(self) -> dict[str, dict[str, Any]]:
        return {call.args["call_id"]: call.args["payload"] for call in self.sent("send_function_output")}

    def runs(self) -> list[RecordedRequest]:
        return self.api.requests_to("POST", "/runs")

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                if self._run is not None and self._run.done():
                    self._run.result()
                    raise AssertionError("the session ended before the condition held")
                await asyncio.sleep(0.01)

    async def wait_for_runs(self, count: int) -> list[RecordedRequest]:
        await self.wait_for(lambda: len(self.runs()) >= count)
        await asyncio.sleep(0.05)
        return self.runs()


def _assert_ran_on(run: RecordedRequest, profile: str, person: str = "rob") -> None:
    """``run`` started on ``profile`` with that profile's medium model."""
    assert run.path == f"/p/{profile}/v1/runs"
    assert run.body["session_id"] == f"duplex-{person}-{profile}"
    assert (run.body["provider"], run.body["model"]) == (MODELS[profile].provider, MODELS[profile].model)
    assert run.body["model_options"] == {"reasoning": {"effort": "medium"}}


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@pytest.fixture
async def harness(tmp_path: Path, api: FakeHermesApi) -> AsyncIterator[_Harness]:
    harness = _Harness(tmp_path, api)
    yield harness
    await harness.stop()


async def test_a_request_another_profile_fits_is_held_and_the_speaker_asked(harness: _Harness) -> None:
    router = _SuggestingRouter()
    router.release = asyncio.Event()
    harness.router = router
    harness.start([_handoff("call_1", NVDA)])

    await harness.wait_for(lambda: "call_1" in harness.outputs())
    assert harness.outputs() == {"call_1": {"status": "received"}}, "the receipt comes before routing ends"
    assert harness.said() == []
    router.release.set()
    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)

    assert harness.said() == [suggest_switch("stock-picker")]
    assert harness.active_profiles.get("rob") == "general"
    assert harness.runs() == [] and harness.tracker.active_for("rob") == []


@pytest.mark.parametrize("answer", ["yes", "Sure, go ahead."])
async def test_yes_switches_and_runs_the_held_request_on_the_new_profile(
    harness: _Harness, answer: str
) -> None:
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "delegate", {"task": answer}))]
    )

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "stock-picker")
    assert run.body["input"] == NVDA
    assert harness.active_profiles.get("rob") == "stock-picker"
    assert harness.outputs() == {"call_1": {"status": "received"}, "call_2": {"status": "received"}}
    await harness.wait_for(lambda: len(harness.said()) == 3)
    assert harness.said() == [
        suggest_switch("stock-picker"),
        switched_to("stock-picker"),
        f'Started "{NVDA}" on the stock-picker agent.',
    ]
    assert isinstance(harness.router, _SuggestingRouter)
    assert harness.router.requests == [(NVDA, "general")], "the answer is never routed"


@pytest.mark.parametrize("answer", ["no", "No thanks.", "stay here"])
async def test_no_runs_the_held_request_on_the_active_profile(harness: _Harness, answer: str) -> None:
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "delegate", {"task": answer}))]
    )

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "general")
    assert run.body["input"] == NVDA
    assert harness.active_profiles.get("rob") == "general"
    await harness.wait_for(lambda: len(harness.said()) == 2)
    assert harness.said() == [suggest_switch("stock-picker"), f'Started "{NVDA}" on the general agent.']
    assert isinstance(harness.router, _SuggestingRouter)
    assert harness.router.requests == [(NVDA, "general")]


async def test_confirm_switch_true_switches_and_runs_there(harness: _Harness) -> None:
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "confirm_switch", {"accept": True}))]
    )

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "stock-picker")
    assert harness.outputs()["call_2"] == {"status": "switched"}
    await harness.wait_for(lambda: len(harness.said()) == 3)
    assert harness.said()[1] == switched_to("stock-picker"), "the confirmation is its own message"


async def test_confirm_switch_false_runs_on_the_active_profile(harness: _Harness) -> None:
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "confirm_switch", {"accept": False}))]
    )

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "general")
    assert harness.outputs()["call_2"] == {"status": "declined"}
    assert harness.active_profiles.get("rob") == "general"


async def test_confirm_switch_with_no_question_pending_says_nothing_is_waiting(harness: _Harness) -> None:
    harness.start([{"at_input_ms": 500, "event": _call("call_1", "confirm_switch", {"accept": True})}])

    await harness.wait_for(lambda: "call_1" in harness.outputs())
    await asyncio.sleep(0.1)

    assert harness.outputs()["call_1"] == {"status": "nothing_pending", "message": NO_QUESTION_PENDING}
    assert harness.active_profiles.get("rob") == "general"
    assert harness.runs() == [] and harness.said() == []


class _SlowToSpeakProvider(FakeProvider):
    """A fake provider whose ``say`` returns only once ``spoken`` is set, as when the floor is taken."""

    def __init__(self, script: Iterable[Mapping[str, Any]]) -> None:
        super().__init__(script)
        self.spoken = asyncio.Event()

    async def say(self, text: str, *, expects_answer: bool = False) -> None:
        await super().say(text, expects_answer=expects_answer)
        await self.spoken.wait()


async def test_the_switch_question_is_said_to_be_answered_and_times_out_only_once_said(
    harness: _Harness,
) -> None:
    provider = _SlowToSpeakProvider([_handoff("call_1", NVDA)])
    harness.start([], provider=provider)
    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)

    assert [call.args["expects_answer"] for call in harness.sent("say")] == [True]
    assert QUESTION_TIMEOUT_S not in harness.sleep.requested_s, "not spoken yet"
    provider.spoken.set()
    await harness.wait_for(lambda: QUESTION_TIMEOUT_S in harness.sleep.requested_s)
    assert harness.runs() == []


@pytest.mark.parametrize("late", ["yes", "no thanks"])
async def test_a_second_answer_after_the_question_was_settled_is_not_run(
    harness: _Harness, late: str
) -> None:
    harness.start(
        [
            _handoff("call_1", NVDA),
            _after_the_question(_call("call_2", "delegate", {"task": "yes"})),
            _after_the_question(_call("call_3", "delegate", {"task": late})),  # after "You're now talking to"
        ]
    )

    (run,) = await harness.wait_for_runs(1)
    await harness.wait_for(lambda: NO_QUESTION_PENDING in harness.said())
    await asyncio.sleep(0.1)

    _assert_ran_on(run, "stock-picker")
    assert harness.runs() == [run]
    assert isinstance(harness.router, _SuggestingRouter)
    assert harness.router.requests == [(NVDA, "general")], "neither answer is routed"
    assert harness.active_profiles.get("rob") == "stock-picker"


async def test_an_answer_after_the_question_timed_out_is_not_run(harness: _Harness) -> None:
    harness.start(
        [
            _handoff("call_1", NVDA),
            _after_the_question({"type": "output_transcript", "delta": "Want me to switch?"}),
            _after_the_question(_call("call_2", "delegate", {"task": "yes"})),  # after the start notice
        ]
    )
    await harness.wait_for(lambda: bool(harness.said()))
    harness.sleep.expire_holds.set()

    (run,) = await harness.wait_for_runs(1)
    await harness.wait_for(lambda: NO_QUESTION_PENDING in harness.said())
    await asyncio.sleep(0.1)

    _assert_ran_on(run, "general")
    assert harness.runs() == [run]
    assert harness.active_profiles.get("rob") == "general", "a late yes switches nothing"


async def test_no_answer_within_the_timeout_runs_on_the_active_profile(harness: _Harness) -> None:
    harness.start([_handoff("call_1", NVDA)])
    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)
    assert harness.runs() == []
    assert QUESTION_TIMEOUT_S in harness.sleep.requested_s

    harness.sleep.expire_holds.set()
    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "general")
    assert harness.active_profiles.get("rob") == "general"


async def test_a_newer_request_runs_the_held_one_on_the_active_profile_and_is_evaluated(
    harness: _Harness,
) -> None:
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "delegate", {"task": WEATHER}))]
    )

    runs = await harness.wait_for_runs(2)

    assert sorted(run.body["input"] for run in runs) == sorted([NVDA, WEATHER])
    for run in runs:
        _assert_ran_on(run, "general")
    assert isinstance(harness.router, _SuggestingRouter)
    assert harness.router.requests == [(NVDA, "general"), (WEATHER, "general")]
    assert harness.said().count(suggest_switch("stock-picker")) == 1


async def test_a_newer_suggested_request_replaces_the_hold_and_is_asked_about(harness: _Harness) -> None:
    later = "and what about AMD"
    harness.start(
        [_handoff("call_1", NVDA), _after_the_question(_call("call_2", "delegate", {"task": later}))]
    )

    (run,) = await harness.wait_for_runs(1)
    await harness.wait_for(lambda: harness.said().count(suggest_switch("stock-picker")) == 2)

    _assert_ran_on(run, "general")
    assert run.body["input"] == NVDA, "the older hold runs; the newer one is held"


async def test_a_suggested_profile_the_speaker_may_not_use_is_never_asked_about(harness: _Harness) -> None:
    harness.start([_handoff("call_1", NVDA)], speaker="alex")

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "general", person="alex")
    assert suggest_switch("stock-picker") not in harness.said()


@pytest.mark.parametrize(
    ("suggest", "speaker", "asked"),
    [
        (True, "rob", True),
        (False, "rob", False),  # suggestions off
        (True, "alex", False),  # stock-picker is not Alex's to use
    ],
)
async def test_the_router_suggests_only_when_on_and_only_a_permitted_profile(
    harness: _Harness, tmp_path: Path, suggest: bool, speaker: str, asked: bool
) -> None:
    config = _config(tmp_path, suggest_switch=suggest)
    router = CompositeRouter(config.router, config.profiles, [_FixedBackend()])
    await router.configure()
    harness.router = router
    harness.start([_handoff("call_1", NVDA)], speaker=speaker)

    if asked:
        await harness.wait_for(lambda: bool(harness.said()))
        await asyncio.sleep(0.1)
        assert harness.said() == [suggest_switch("stock-picker")]
        assert harness.runs() == []
    else:
        (run,) = await harness.wait_for_runs(1)
        assert run.path == "/p/general/v1/runs"
        assert suggest_switch("stock-picker") not in harness.said()


async def test_a_request_said_with_a_switch_runs_on_the_new_profile_without_a_question(
    harness: _Harness,
) -> None:
    await harness.active_profiles.switch("rob", "stock-picker")
    harness.start([_handoff("call_1", "go back to general and then tell me what NVDA did today")])

    (run,) = await harness.wait_for_runs(1)

    _assert_ran_on(run, "general")
    assert run.body["input"] == "tell me what NVDA did today"
    assert isinstance(harness.router, _SuggestingRouter)
    assert harness.router.requests == [("tell me what NVDA did today", "general")]
    await harness.wait_for(lambda: len(harness.said()) == 2)
    assert harness.said() == [
        switched_to("general"),
        'Started "what NVDA did today" on the general agent.',
    ]


async def test_a_pending_hold_starts_on_the_active_profile_when_the_session_ends(harness: _Harness) -> None:
    harness.start([_handoff("call_1", NVDA)])
    await harness.wait_for(lambda: bool(harness.said()))

    await harness.end_session()

    (run,) = harness.runs()
    _assert_ran_on(run, "general")
    (task,) = harness.tracker.adopt("rob").open_tasks
    assert (task.text, task.profile, task.state) == (NVDA, "general", "running"), "left unattended"
