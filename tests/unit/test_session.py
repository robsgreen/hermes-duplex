"""VoiceSession: delegation → credit → route → permission → dispatch → announce, with the fake provider."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.audio.frames import silent_frame
from hermes_duplex.config import CONTROL_PROFILE, load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.prompts.base import PromptContext, TaskReport, VoicePrompter
from hermes_duplex.prompts.notices import (
    NO_SUCH_TASK,
    NOTHING_TO_CANCEL,
    WHO_IS_ASKING,
    only_one_running,
    task_summary,
    which_to_cancel,
)
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.fake.adapter import FakeProvider, SentCall
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.router.base import ControlIntent, RouteDecision
from hermes_duplex.router.composite import CompositeRouter
from hermes_duplex.router.knn import KnnBackend
from hermes_duplex.session import Router, VoiceSession, _resolve_task, _topic_words
from hermes_duplex.tasks import Task, TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_embedder import fake_embed
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript, admission_queued

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
STOCKS_MODEL = ModelChoice(provider="openai", model="gpt-x")

STARTED_1 = 'Started "how is my portfolio doing" on the stock-picker agent.'

QUEUED_TESLA = 'Queued "and what about Tesla" behind "how is my portfolio doing".'
STARTED_WEATHER = 'Started "what\'s the weather" on the general agent.'
CANCELLED_1 = 'Cancelled "how is my portfolio doing".'
AWAY_1 = 'While you were away, "how is my portfolio doing" finished: '

# A run that starts and never ends on its own, so it stays open for status, cancel, and queueing.
OPEN_RUN = RunScript(events=[{"event": "run.started"}])


class _MemoryConversation:
    """A ``Conversation`` that yields scripted frames, then waits until it is closed."""

    id = "memory-1"

    def __init__(self, frames: Sequence[InputFrame]) -> None:
        self._frames = frames
        self._closed = asyncio.Event()
        self.played: list[bytes] = []
        self.notified: list[dict[str, Any]] = []
        self.closed_reason: str | None = None

    async def frames(self) -> AsyncIterator[InputFrame]:
        for frame in self._frames:
            yield frame
        await self._closed.wait()

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        assert rate == RATE
        self.played.append(pcm16_mono)

    async def flush_playback(self) -> None:
        pass

    async def notify(self, event: dict[str, Any]) -> None:
        self.notified.append(event)

    async def close(self, reason: str) -> None:
        self.closed_reason = reason
        self._closed.set()


def _frames(duration_ms: int, voiced: Callable[[int], Iterable[str]] = lambda t_ms: ()) -> list[InputFrame]:
    """20 ms silent frames covering ``duration_ms``; ``voiced(t_ms)`` names who spoke in each."""
    return [
        InputFrame(silent_frame(RATE), RATE, frozenset(voiced(t_ms)), t_ms)
        for t_ms in range(0, duration_ms, 20)
    ]


def _voiced(person: str) -> Callable[[int], Iterable[str]]:
    return lambda t_ms: (person,)


def _delegation(at_input_ms: int, call_id: str, task: str) -> dict[str, Any]:
    return {
        "at_input_ms": at_input_ms,
        "event": {"type": "delegation", "id": call_id, "tool_args": {"task": task}},
    }


def _decision(profile: str, control_intent: ControlIntent | None = None) -> RouteDecision:
    is_control = profile == CONTROL_PROFILE
    return RouteDecision(
        profile=profile,
        effort="high" if profile == "stock-picker" else "medium",
        model=STOCKS_MODEL if profile == "stock-picker" else None,
        confidence=0.9,
        probabilities={profile: 0.9},
        followup_prob=None,
        sticky=False,
        source="knn",
        latency_ms=1,
        control_intent=(control_intent or "status") if is_control else None,
    )


class _StubRouter:
    """Routes each task text to a fixed decision and records what it was asked."""

    def __init__(self, decisions: Mapping[str, RouteDecision]) -> None:
        self._decisions = decisions
        self.requests: list[tuple[str, str, tuple[str, ...], str | None]] = []
        self.permitted: list[dict[str, bool]] = []
        """Per request, whether the speaker may use each profile, as the session told the router."""

    async def route(
        self,
        text: str,
        speaker: str,
        *,
        history: Sequence[str] = (),
        hint: str | None = None,
        permitted: Callable[[str], bool] | None = None,
    ) -> RouteDecision:
        self.requests.append((text, speaker, tuple(history), hint))
        if permitted is not None:
            self.permitted.append({profile: permitted(profile) for profile in ("general", "stock-picker")})
        return self._decisions[text]


ROUTES = {
    "how is my portfolio doing": _decision("stock-picker"),
    "and what about Tesla": _decision("stock-picker"),
    "what's the weather": _decision("general"),
    "what are you working on": _decision(CONTROL_PROFILE, "status"),
    "cancel that": _decision(CONTROL_PROFILE, "cancel"),
    "cancel task 1": _decision(CONTROL_PROFILE, "cancel"),
    "cancel the portfolio one": _decision(CONTROL_PROFILE, "cancel"),
    "cancel the latest one": _decision(CONTROL_PROFILE, "cancel"),
    "cancel the Tesla question": _decision(CONTROL_PROFILE, "cancel"),
    "how's the Tesla thing": _decision(CONTROL_PROFILE, "status"),
}


class _Harness:
    """A session wired to the fake provider, a stub router, and the fake Hermes API."""

    def __init__(
        self, tmp_path: Path, api: FakeHermesApi, *, api_base: str | None = None, router: Router | None = None
    ) -> None:
        self.tmp_path = tmp_path
        self.api = api
        self.api_base = api_base or api.api_base
        self.stub = _StubRouter(ROUTES)
        self.router = router or self.stub
        self.tracker = TaskTracker(tmp_path / "home")
        self.permissions = PermissionChecker(_config(tmp_path))
        self.provider: FakeProvider | None = None
        self.conversation: _MemoryConversation | None = None
        self._hermes: HermesApiClient | None = None
        self._run: asyncio.Task[None] | None = None

    def start(
        self,
        script: Iterable[Mapping[str, Any]],
        frames: Sequence[InputFrame],
        *,
        capabilities: Mapping[str, object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        prompter: VoicePrompter | None = None,
        prompt_context: PromptContext | None = None,
    ) -> None:
        self.provider = FakeProvider(
            script, capabilities=capabilities, prompter=prompter, prompt_context=prompt_context
        )
        self.conversation = _MemoryConversation(frames)
        self._hermes = HermesApiClient(self.api_base, KEYS.get, sleep=lambda _s: asyncio.sleep(0.01))
        session = VoiceSession(
            self.conversation,
            self.provider,
            router=self.router,
            hermes=self._hermes,
            tracker=self.tracker,
            permissions=self.permissions,
            clock=clock,
        )
        self._run = asyncio.create_task(session.run())

    async def stop(self) -> None:
        if self.conversation is not None and self._run is not None:
            await self.conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
        if self._hermes is not None:
            await self._hermes.close()
        await self.tracker.close()

    def sent(self, method: str) -> list[SentCall]:
        assert self.provider is not None
        return [call for call in self.provider.calls if call.method == method]

    def texts(self, method: str) -> list[str]:
        return [call.args["text"] for call in self.sent(method)]

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                if self._run is not None and self._run.done():
                    self._run.result()
                    raise AssertionError("the session ended before the condition held")
                await asyncio.sleep(0.01)


def _config(tmp_path: Path) -> Any:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather", "set a reminder"]},
            "stock-picker": {
                "description": "Portfolio, holdings, stock research, trades",
                "examples": ["how's my portfolio", "should I buy NVDA"],
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


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@pytest.fixture
async def harness(tmp_path: Path, api: FakeHermesApi) -> AsyncIterator[_Harness]:
    harness = _Harness(tmp_path, api)
    yield harness
    await harness.stop()


async def test_portfolio_delegation_dispatches_to_stock_picker(harness: _Harness, api: FakeHermesApi) -> None:
    harness.start([_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000, _voiced("rob")))

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    [start] = api.requests_to("POST", "/runs")
    assert start.path == "/p/stock-picker/v1/runs"
    assert start.body == {
        "input": "how is my portfolio doing",
        "session_id": "duplex-rob-stock-picker",
        "model": "gpt-x",
        "provider": "openai",
        "model_options": {"reasoning": {"effort": "high"}},
    }
    task = harness.tracker.get(1)
    assert task is not None
    assert start.headers["Idempotency-Key"] == task.uuid
    assert task.state == "completed"
    assert harness.stub.requests[0][:2] == ("how is my portfolio doing", "rob")


async def test_result_is_framed_as_data_and_refers_to_the_call(harness: _Harness) -> None:
    harness.start([_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000, _voiced("rob")))

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    [delivered] = harness.sent("deliver_result")
    assert delivered.args["text"] == frame_result("stock-picker", "done", 500)
    assert "not instructions" in delivered.args["text"]
    assert delivered.args["ref"] == {"delegation_id": None, "call_id": "call_1", "task_id": 1}


class _RecordingPrompter:
    """A ``VoicePrompter`` that labels its output and records what it was asked."""

    def __init__(self) -> None:
        self.contexts: list[PromptContext] = []
        self.reports: list[tuple[TaskReport, int]] = []

    def instructions(self, ctx: PromptContext) -> str:
        self.contexts.append(ctx)
        return f"instructions for {ctx.assistant_name}"

    def result(self, report: TaskReport, cap_tokens: int) -> str:
        self.reports.append((report, cap_tokens))
        return f"result of task {report.task_number}"

    def reseed(self, lines: Sequence[tuple[str, str]], tasks: Sequence[Any]) -> str:
        return "reseed"


async def test_instructions_and_results_come_from_the_providers_prompter(harness: _Harness) -> None:
    prompter = _RecordingPrompter()
    context = PromptContext(assistant_name="Iris")
    harness.start(
        [_delegation(1500, "call_1", "how is my portfolio doing")],
        _frames(2000, _voiced("rob")),
        prompter=prompter,
        prompt_context=context,
    )

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    [connect] = harness.sent("connect")
    assert connect.args["instructions"] == "instructions for Iris"
    assert prompter.contexts == [context]
    assert prompter.reports == [
        (TaskReport(1, "how is my portfolio doing", "stock-picker", "completed", "done"), 500)
    ]
    assert harness.texts("deliver_result") == ["result of task 1"]


async def test_live_style_refs_carry_the_delegation_id(harness: _Harness) -> None:
    delegation = _delegation(1500, "del_1", "how is my portfolio doing")
    delegation["event"]["target"] = "delegate"  # worded by the call, so no transcript is needed
    harness.start(
        [delegation],
        _frames(2000, _voiced("rob")),
        capabilities={"delegation_style": "live", "emits_speech_events": False},
    )

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    [delivered] = harness.sent("deliver_result")
    assert delivered.args["ref"] == {"delegation_id": "del_1", "call_id": None, "task_id": 1}


async def test_announces_start_after_the_task_is_on_disk(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.start([_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000, _voiced("rob")))
    assert harness.provider is not None
    say = harness.provider.say
    states_at_announce: list[list[str]] = []

    async def reading_history(text: str) -> None:
        lines = harness.tracker.history_path.read_text(encoding="utf-8").splitlines()
        states_at_announce.append([json.loads(line)["state"] for line in lines])
        await say(text)

    monkeypatch.setattr(harness.provider, "say", reading_history)

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    # The call's receipt carried no task number, so the start notice is spoken.
    assert harness.texts("say")[0] == STARTED_1
    assert states_at_announce[0] == ["routing", "running"]


async def test_refuses_a_profile_the_person_may_not_use(
    tmp_path: Path, api: FakeHermesApi, caplog: pytest.LogCaptureFixture
) -> None:
    # The real router, so the log shows its decision line before the refusal line.
    config = _config(tmp_path)
    router = CompositeRouter(config.router, config.profiles, [KnnBackend(fake_embed)])
    await router.configure()
    harness = _Harness(tmp_path, api, router=router)
    caplog.set_level(logging.INFO)
    try:
        harness.start(
            [_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000, _voiced("alex"))
        )

        await harness.wait_for(lambda: bool(harness.sent("say")))

        [said] = harness.texts("say")
        assert "stock-picker" in said
        assert api.requests_to("POST", "/runs") == []
        assert harness.tracker.get(1) is None
        decisions = [r for r in caplog.records if r.name == "hermes_duplex.router.composite"]
        refusals = [
            r for r in caplog.records if r.name == "hermes_duplex.session" and "refused" in r.getMessage()
        ]
        assert len(decisions) == 1 and "profile=stock-picker" in decisions[0].getMessage()
        assert len(refusals) == 1 and "alex" in refusals[0].getMessage()
    finally:
        await harness.stop()


async def test_router_is_told_which_profiles_the_speaker_may_use(harness: _Harness) -> None:
    harness.start([_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000, _voiced("alex")))

    await harness.wait_for(lambda: bool(harness.sent("say")))

    # The router keeps a refused profile from becoming Alex's active profile.
    assert harness.stub.permitted == [{"general": True, "stock-picker": False}]


async def _session_that_ended(tmp_path: Path, api: FakeHermesApi) -> _Harness:
    """A session in which Rob delegated the portfolio question (a run that stays open), and which then
    ended; its task is left unattended in its tracker."""
    api.run_scripts.append(RunScript(events=[{"event": "run.started"}]))  # its own, for _complete_run
    first = _Harness(tmp_path, api)
    first.start([_delegation(500, "call_1", "how is my portfolio doing")], _frames(1000, _voiced("rob")))
    await first.wait_for(lambda: bool(first.sent("say")))
    assert first.conversation is not None and first._run is not None
    await first.conversation.close("rob left")
    await asyncio.wait_for(first._run, timeout=5)
    return first


async def _close(*harnesses: _Harness) -> None:
    for harness in harnesses:
        await harness.stop()


def _complete_run(api: FakeHermesApi, output: str) -> None:
    """End the fake's only run as completed with ``output``, as if Hermes finished it meanwhile."""
    [run] = api.runs.values()
    run.script.events.append({"event": "run.completed", "output": output})


async def test_a_new_session_adopts_the_open_task_and_queues_behind_it(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    first = await _session_that_ended(tmp_path, api)
    api.run_scripts.append(OPEN_RUN)
    second = _Harness(tmp_path, api)
    second.tracker = first.tracker  # one tracker per service, shared by its sessions
    try:
        second.start(
            [
                _delegation(500, "call_2", "and what about Tesla"),
                {
                    "on_sent": "say",
                    "event": {
                        "type": "delegation",
                        "id": "c3",
                        "tool_args": {"task": "what are you working on"},
                    },
                },
            ],
            _frames(1000, _voiced("rob")),
        )
        await second.wait_for(lambda: len(second.sent("say")) >= 2)

        old, new = second.tracker.get(1), second.tracker.get(2)
        assert old is not None and old.state == "running", "its Hermes run goes on"
        assert new is not None
        assert second.texts("say") == [QUEUED_TESLA, task_summary([old, new])]
    finally:
        await _close(first, second)


async def test_a_result_that_finished_while_the_person_was_away_is_announced_in_their_next_session(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    first = await _session_that_ended(tmp_path, api)
    _complete_run(api, "Up 2% today.")
    second = _Harness(tmp_path, api)
    second.tracker = first.tracker
    try:
        second.start([], _frames(200, _voiced("rob")))
        await second.wait_for(lambda: bool(second.sent("say")))

        assert second.texts("say") == [AWAY_1 + frame_result("stock-picker", "Up 2% today.", 500)]
        task = second.tracker.get(1)
        assert task is not None and task.state == "completed"
        assert api.requests_to("GET", f"/runs/{task.run_id}"), "the ended run was read by its status"
    finally:
        await _close(first, second)


async def test_status_and_cancel_reach_an_adopted_task(tmp_path: Path, api: FakeHermesApi) -> None:
    first = await _session_that_ended(tmp_path, api)
    second = _Harness(tmp_path, api)
    second.tracker = first.tracker
    try:
        second.start(
            [
                _delegation(500, "c2", "what are you working on"),
                {
                    "on_sent": "say",
                    "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "cancel task 1"}},
                },
            ],
            _frames(1000, _voiced("rob")),
        )
        await second.wait_for(lambda: len(second.sent("say")) >= 2)

        assert second.texts("say")[0] == (
            '"how is my portfolio doing" on the stock-picker agent is running, and I\'ll tell you when it '
            "finishes."
        )
        assert second.texts("say")[1] == CANCELLED_1
        assert len(api.requests_to("POST", "/stop")) == 1
        await asyncio.sleep(0.1)
        assert len(second.sent("say")) == 2, "the run's own cancelled event is not announced again"
    finally:
        await _close(first, second)


async def test_other_peoples_unattended_tasks_are_not_adopted(tmp_path: Path, api: FakeHermesApi) -> None:
    first = await _session_that_ended(tmp_path, api)
    _complete_run(api, "Up 2% today.")
    second = _Harness(tmp_path, api)
    second.tracker = first.tracker
    try:
        second.start([], _frames(200, _voiced("alex")))
        await asyncio.sleep(0.3)

        assert second.sent("say") == []
        task = second.tracker.get(1)
        assert task is not None and task.state == "running", "nobody followed Rob's task"
        assert [task.number for task in second.tracker.adopt("rob").open_tasks] == [1], (
            "still waiting for Rob"
        )
    finally:
        await _close(first, second)


async def test_a_restarted_service_announces_what_its_previous_run_left_open(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    first = await _session_that_ended(tmp_path, api)
    await first.stop()  # the service stops; its tracker's state is lost, the history file stays
    _complete_run(api, "Up 2% today.")

    restarted = TaskTracker(tmp_path / "home")
    await restarted.restore()
    second = _Harness(tmp_path, api)
    second.tracker = restarted
    try:
        second.start([], _frames(200, _voiced("rob")))
        await second.wait_for(lambda: bool(second.sent("say")))

        assert second.texts("say") == [AWAY_1 + frame_result("stock-picker", "Up 2% today.", 500)]
    finally:
        await second.stop()


async def test_asks_who_is_asking_when_nobody_was_voiced(harness: _Harness, api: FakeHermesApi) -> None:
    harness.start([_delegation(1500, "call_1", "how is my portfolio doing")], _frames(2000))

    await harness.wait_for(lambda: bool(harness.sent("send_function_output")))
    await asyncio.sleep(0.1)

    # The call itself is answered with the question, so the model asks it; nothing else is said.
    [output] = harness.sent("send_function_output")
    assert output.args == {
        "call_id": "call_1",
        "payload": {"status": "not_started", "message": WHO_IS_ASKING},
    }
    assert harness.sent("say") == []
    assert harness.stub.requests == []
    assert api.requests_to("POST", "/runs") == []


async def test_credits_the_person_voiced_most_in_the_last_four_seconds(
    harness: _Harness, api: FakeHermesApi
) -> None:
    # Alex talked long before; Rob is the only one voiced in the 4 s before the call.
    def voiced(t_ms: int) -> Iterable[str]:
        return ("alex",) if t_ms < 1000 else ("rob",) if t_ms >= 5000 else ()

    harness.start([_delegation(6000, "call_1", "how is my portfolio doing")], _frames(6000, voiced))

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    assert harness.stub.requests[0][1] == "rob"
    assert api.requests_to("POST", "/runs")[0].body["session_id"] == "duplex-rob-stock-picker"


async def test_control_status_summarizes_the_speakers_open_tasks(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "what are you working on"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 2)

    running = harness.tracker.get(1)
    assert running is not None
    assert harness.texts("say") == [STARTED_1, task_summary([running])]
    assert len(api.requests_to("POST", "/runs")) == 1


async def test_control_cancel_stops_the_most_recent_running_task(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "cancel that"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 2)

    task = harness.tracker.get(1)
    assert task is not None and task.state == "cancelled"
    [stop] = api.requests_to("POST", "/stop")
    assert stop.path == f"/p/stock-picker/v1/runs/{task.run_id}/stop"
    assert harness.texts("say") == [STARTED_1, CANCELLED_1]
    assert harness.sent("deliver_result") == [], "the cancel is confirmed once, by say"


async def test_cancel_is_confirmed_once_when_the_run_reports_cancelled_before_stop_returns(
    harness: _Harness, api: FakeHermesApi
) -> None:
    run = RunScript(events=[{"event": "run.started"}])
    api.run_scripts.append(run)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "cancel that"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )
    assert harness._hermes is not None

    async def stop_after_the_run_reports_cancelled(profile: str, run_id: str) -> None:
        run.events.append({"event": "run.cancelled"})
        await harness.wait_for(
            lambda: (task := harness.tracker.get(1)) is not None and task.state == "cancelled"
        )

    harness._hermes.stop = stop_after_the_run_reports_cancelled  # type: ignore[method-assign]

    await harness.wait_for(lambda: len(harness.sent("say")) >= 2)
    await asyncio.sleep(0.2)

    assert harness.texts("say") == [STARTED_1, CANCELLED_1]
    assert harness.sent("deliver_result") == [], "the cancel is confirmed once, by say"


async def test_control_cancel_by_number(harness: _Harness, api: FakeHermesApi) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "what's the weather"}},
            },
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "cancel task 1"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 3)

    assert harness.texts("say") == [
        STARTED_1,
        STARTED_WEATHER,
        CANCELLED_1,
    ]
    second = harness.tracker.get(2)
    assert second is not None and second.state == "running"


async def test_second_task_on_the_same_profile_is_queued_behind_the_first(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "and what about Tesla"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 2)

    assert harness.texts("say")[1] == QUEUED_TESLA
    sessions = [request.body["session_id"] for request in api.requests_to("POST", "/runs")]
    assert sessions == ["duplex-rob-stock-picker", "duplex-rob-stock-picker"]


async def test_tasks_on_different_profiles_run_concurrently(harness: _Harness, api: FakeHermesApi) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "what's the weather"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    assert harness.texts("say")[1] == STARTED_WEATHER
    [delivered] = harness.sent("deliver_result")
    assert delivered.args["ref"]["task_id"] == 2
    first = harness.tracker.get(1)
    assert first is not None and first.state == "running"


async def test_progress_is_throttled_per_delegation(harness: _Harness, api: FakeHermesApi) -> None:
    api.run_scripts.append(
        RunScript(
            events=[
                {"event": "run.started"},
                *(
                    {"event": "tool.started", "tool": "web_search", "preview": f"step {n}"}
                    for n in range(1, 5)
                ),
                {"event": "run.completed", "output": "done"},
            ]
        )
    )
    ticks = iter(range(2, 1000, 2))  # each progress event is 2 s after the previous one
    harness.start(
        [_delegation(500, "call_1", "how is my portfolio doing")],
        _frames(1000, _voiced("rob")),
        clock=lambda: float(next(ticks)),
    )

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    # Progress at 2 s and 8 s; 4 s and 6 s are within 5 s of a report.
    assert harness.texts("report_progress") == [
        "web_search: step 1",
        "web_search: step 4",
    ]


async def test_queued_run_that_ends_without_a_start_passes_through_running(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.start_replies.append(admission_queued(with_run_id=True))
    api.run_scripts.append(RunScript(events=[{"event": "run.completed", "output": "done"}]))
    harness.start([_delegation(500, "call_1", "how is my portfolio doing")], _frames(1000, _voiced("rob")))

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    states = [json.loads(line)["state"] for line in harness.tracker.history_path.read_text().splitlines()]
    assert states == ["routing", "queued", "running", "completed"]
    assert harness.texts("deliver_result") == [frame_result("stock-picker", "done", 500)]


async def test_failed_run_delivers_a_short_failure_notice(harness: _Harness, api: FakeHermesApi) -> None:
    api.run_scripts.append(
        RunScript(events=[{"event": "run.started"}, {"event": "run.failed", "error": "model overloaded"}])
    )
    harness.start([_delegation(500, "call_1", "how is my portfolio doing")], _frames(1000, _voiced("rob")))

    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    assert harness.texts("deliver_result") == ['Sorry, "how is my portfolio doing" failed: model overloaded.']
    task = harness.tracker.get(1)
    assert task is not None and task.state == "failed"


async def test_hermes_unreachable_is_spoken_and_fails_the_task(tmp_path: Path, api: FakeHermesApi) -> None:
    harness = _Harness(tmp_path, api, api_base="http://127.0.0.1:9")
    try:
        harness.start(
            [_delegation(500, "call_1", "how is my portfolio doing")], _frames(1000, _voiced("rob"))
        )

        await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

        [notice] = harness.texts("deliver_result")
        assert "Hermes is unreachable" in notice
        task = harness.tracker.get(1)
        assert task is not None and task.state == "failed"
    finally:
        await harness.stop()


async def test_audio_flows_both_ways_and_the_ui_hears_about_it(harness: _Harness) -> None:
    harness.start(
        [
            {"at_input_ms": 200, "event": {"type": "input_transcript", "delta": "hello", "final": True}},
            {"at_input_ms": 200, "event": {"type": "output_transcript", "delta": "Hi there."}},
            {"at_input_ms": 200, "event": {"type": "output_audio", "ms": 100, "item_id": "item_1"}},
            {"at_input_ms": 200, "event": {"type": "output_done"}},
        ],
        _frames(400, _voiced("rob")),
    )
    assert harness.provider is not None and harness.conversation is not None
    conversation = harness.conversation

    await harness.wait_for(lambda: sum(len(pcm) for pcm in conversation.played) >= 100 * RATE // 1000 * 2)

    assert harness.provider.input_ms == 400
    transcript = [event["transcript"] for event in conversation.notified if "transcript" in event]
    assert transcript == [{"speaker": "user", "text": "hello"}, {"speaker": "assistant", "text": "Hi there."}]


def _open_task(number: int, text: str) -> Task:
    moment = datetime(2026, 11, 3, tzinfo=UTC)
    return Task(
        number=number,
        uuid=f"u{number}",
        person="rob",
        profile="stock-picker",
        effort="medium",
        model=None,
        text=text,
        state="running",
        run_id=f"run_{number}",
        created_at=moment,
        updated_at=moment,
    )


PORTFOLIO = _open_task(1, "how is my portfolio doing")
TESLA = _open_task(2, "and what about Tesla")


@pytest.mark.parametrize(
    ("words", "task_id", "number"),
    [
        ("cancel the portfolio one", None, 1),
        ("how's the Tesla thing", None, 2),
        ("what about Tesla's stock", None, 2),
        ("cancel that", None, None),  # names no topic, and two are running
        ("cancel the weather one", None, None),  # unrelated words
        ("", None, None),
        ("cancel task 2", None, 2),
        ("cancel task number 1", None, 1),
        ("stop task #7", None, 7),  # named outright, though not open: the caller says it is not found
        ("cancel the top 5 stocks", None, None),  # a number that is not a task number
        ("the portfolio one", 2, 2),  # a tool call's task_id comes first
        ("about tesla and my portfolio", None, None),  # a tie is no clear winner
    ],
)
def test_resolve_task_by_id_then_named_number_then_topic(
    words: str, task_id: int | None, number: int | None
) -> None:
    assert _resolve_task([PORTFOLIO, TESLA], words, task_id) == number


@pytest.mark.parametrize(
    "words", ["cancel the latest one", "the new one", "the other one", "the first one", "the last one"]
)
def test_order_and_recency_words_name_no_task(words: str) -> None:
    assert _topic_words(words) == set()
    assert _resolve_task([PORTFOLIO, TESLA], words) is None


def test_news_is_a_topic_word_not_a_plural_of_new() -> None:
    assert _topic_words("any news about Tesla") == {"news", "tesla"}
    assert _topic_words("my stocks") == {"stock"}, "other plurals still drop their s"


@pytest.mark.parametrize("words", ["don't", "won't", "never mind", "now", "just", "no, just stop it now"])
def test_contractions_and_dismissals_add_no_topic_words(words: str) -> None:
    assert _topic_words(words) == set()


@pytest.mark.parametrize("words", ["cancel that", "", "cancel the latest one", "never mind, don't do it"])
def test_resolve_task_with_one_open_task_and_no_topic_words_means_that_one(words: str) -> None:
    assert _resolve_task([TESLA], words) == 2
    assert _resolve_task([], words) is None


def test_resolve_task_with_one_open_task_and_other_topic_words_names_none() -> None:
    assert _resolve_task([TESLA], "cancel the weather one") is None
    assert _resolve_task([TESLA], "cancel the Tesla one") == 2


async def test_status_and_cancel_find_the_task_by_its_topic(harness: _Harness, api: FakeHermesApi) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "and what about Tesla"}},
            },
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "how's the Tesla thing"}},
            },
            {
                "on_sent": "say",
                "event": {
                    "type": "delegation",
                    "id": "c4",
                    "tool_args": {"task": "cancel the portfolio one"},
                },
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 4)

    portfolio, tesla = harness.tracker.get(1), harness.tracker.get(2)
    assert portfolio is not None and tesla is not None
    assert harness.texts("say")[2].startswith('"and what about Tesla" on the stock-picker agent is ')
    assert "portfolio" not in harness.texts("say")[2]
    assert harness.texts("say")[3] == CANCELLED_1
    assert portfolio.state == "cancelled" and tesla.state in ("queued", "running")
    [stop] = api.requests_to("POST", "/stop")
    assert stop.path.endswith(f"/runs/{portfolio.run_id}/stop")


async def test_cancel_naming_no_task_asks_which_when_several_are_running(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "what's the weather"}},
            },
            {
                "on_sent": "say",
                "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "cancel that"}},
            },
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 3)

    running = [task for number in (1, 2) if (task := harness.tracker.get(number))]
    assert harness.texts("say")[2] == which_to_cancel(running)
    assert [task.state for task in running] == ["running", "running"]
    assert api.requests_to("POST", "/stop") == []


def _after_say(call_id: str, task: str) -> dict[str, Any]:
    """A delegation of ``task`` sent once the session's previous notice is said."""
    return {"on_sent": "say", "event": {"type": "delegation", "id": call_id, "tool_args": {"task": task}}}


async def test_cancel_the_latest_one_asks_which_when_several_are_running(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            _after_say("c2", "what's the weather"),
            _after_say("c3", "cancel the latest one"),
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 3)

    running = [task for number in (1, 2) if (task := harness.tracker.get(number))]
    assert harness.texts("say")[2] == which_to_cancel(running)
    assert [task.state for task in running] == ["running", "running"]
    assert api.requests_to("POST", "/stop") == []


async def test_cancel_naming_a_task_that_is_not_open_while_another_is_finds_none(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            _after_say("c2", "what's the weather"),
            _after_say("c3", "cancel task 1"),
            _after_say("c4", "cancel task 1"),
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 4)

    weather = harness.tracker.get(2)
    assert harness.texts("say")[2:] == [CANCELLED_1, NO_SUCH_TASK]
    assert weather is not None and weather.state == "running"
    assert len(api.requests_to("POST", "/stop")) == 1


async def test_cancel_naming_a_task_when_none_is_open_has_nothing_to_cancel(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            _after_say("c2", "cancel task 1"),
            _after_say("c3", "cancel task 1"),
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 3)

    assert harness.texts("say") == [STARTED_1, CANCELLED_1, NOTHING_TO_CANCEL]


async def test_cancel_about_another_topic_asks_about_the_only_open_task(
    harness: _Harness, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    harness.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            _after_say("c2", "cancel the Tesla question"),
        ],
        _frames(1000, _voiced("rob")),
    )

    await harness.wait_for(lambda: len(harness.sent("say")) >= 2)

    portfolio = harness.tracker.get(1)
    assert portfolio is not None and portfolio.state == "running"
    assert harness.texts("say") == [
        STARTED_1,
        'The only one running is "how is my portfolio doing". Cancel that?',
    ]
    assert harness.texts("say")[1] == only_one_running(portfolio)
    assert api.requests_to("POST", "/stop") == []


async def test_a_session_already_hearing_the_person_adopts_what_another_session_leaves(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(RunScript(events=[{"event": "run.started"}]))
    web = _Harness(tmp_path, api)
    discord = _Harness(tmp_path, api)
    discord.tracker = web.tracker
    try:
        web.start([_delegation(500, "call_1", "how is my portfolio doing")], _frames(1000, _voiced("rob")))
        await web.wait_for(lambda: bool(web.sent("say")))
        discord.start([], _frames(200, _voiced("rob")))  # Rob is heard here too, before the web call ends
        await asyncio.sleep(0.1)
        assert web.conversation is not None and web._run is not None
        await web.conversation.close("rob left the web call")
        await asyncio.wait_for(web._run, timeout=5)
        conversation = discord.conversation
        assert conversation is not None
        await discord.wait_for(lambda: any("tasks" in event for event in conversation.notified))
        await asyncio.sleep(0.1)  # its run is read as still going, and followed

        _complete_run(api, "Up 2% today.")
        await discord.wait_for(lambda: bool(discord.sent("say")))

        assert discord.texts("say") == [frame_result("stock-picker", "Up 2% today.", 500)]
        task = discord.tracker.get(1)
        assert task is not None and task.state == "completed"
    finally:
        await _close(web, discord)


async def test_a_result_not_yet_spoken_when_the_session_ends_is_told_in_the_next_one(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(RunScript(events=[{"event": "run.started"}]))
    first = _Harness(tmp_path, api)
    # Rob starts talking after the start notice and never stops, so the result waits for the floor.
    first.start(
        [
            _delegation(500, "call_1", "how is my portfolio doing"),
            {"on_sent": "say", "event": {"type": "speech_started"}},
        ],
        _frames(1000, _voiced("rob")),
        clock=lambda: 0.0,
    )
    await first.wait_for(lambda: bool(first.sent("say")))
    _complete_run(api, "Up 2% today.")
    await first.wait_for(lambda: (task := first.tracker.get(1)) is not None and task.state == "completed")
    await asyncio.sleep(0.1)
    assert first.sent("deliver_result") == [] and len(first.sent("say")) == 1, "the result is unspoken"
    assert first.conversation is not None and first._run is not None
    await first.conversation.close("rob left")
    await asyncio.wait_for(first._run, timeout=5)

    second = _Harness(tmp_path, api)
    second.tracker = first.tracker
    try:
        second.start([], _frames(200, _voiced("rob")))
        await second.wait_for(lambda: bool(second.sent("say")))

        assert second.texts("say") == [AWAY_1 + frame_result("stock-picker", "Up 2% today.", 500)]
    finally:
        await _close(first, second)


async def test_an_adopted_run_whose_status_cannot_be_read_ends_as_news(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    first = await _session_that_ended(tmp_path, api)
    [run] = api.runs.values()
    run.script.polls.append(503)
    _complete_run(api, "Up 2% today.")
    second = _Harness(tmp_path, api)
    second.tracker = first.tracker
    try:
        second.start([], _frames(200, _voiced("rob")))
        await second.wait_for(lambda: bool(second.sent("say")))

        assert second.texts("say") == [AWAY_1 + frame_result("stock-picker", "Up 2% today.", 500)]
    finally:
        await _close(first, second)
