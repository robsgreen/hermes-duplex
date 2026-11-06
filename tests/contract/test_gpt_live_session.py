"""VoiceSession with the real GPT-Live adapter over the fake WebSocket server: delegations worded from
transcript windows, speaker credit around the delegation offset, and loudness-gate barge-in."""

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from hermes_duplex import session as session_module
from hermes_duplex.audio.frames import bytes_to_ms, frame_samples, silent_frame
from hermes_duplex.config import load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.prompts.notices import suggest_switch, switching_to
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.gpt_live.adapter import GptLiveProvider
from hermes_duplex.providers.gpt_live.prompt import GptLivePrompter, frame_result
from hermes_duplex.router.base import ControlIntent, Effort, RouteDecision
from hermes_duplex.service import prompt_context
from hermes_duplex.session import VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript
from tests.fakes.fake_ws_server import FakeWsServer

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
LOUD_FRAME = np.full(frame_samples(RATE), 1000, dtype="<i2").tobytes()  # about -30 dBFS, above the gate
STARTED = [{"wait_for": "session.start"}, {"type": "session.started", "event_id": "event_1"}]


def _decision(control_intent: ControlIntent | None = None, suggestion: str | None = None) -> RouteDecision:
    return RouteDecision(
        effort="medium",
        model=None,
        suggestion=suggestion,
        control_intent=control_intent,
        probabilities={},
        source="knn",
        latency_ms=1,
    )


ROUTES = {
    "how is my portfolio doing": _decision(),
    "and what about Tesla": _decision(),
    "what's the weather": _decision(),
    "what's the weather tomorrow": _decision(suggestion="general"),
}


class _StubRouter:
    """Routes each task text to a fixed decision and records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, tuple[str, ...]]] = []

    def model_for(self, profile: str, effort: Effort) -> ModelChoice | None:
        return None

    async def route(
        self,
        text: str,
        speaker: str,
        *,
        active_profile: str,
        permitted: Callable[[str], bool] | None = None,
        history: Sequence[str] = (),
    ) -> RouteDecision:
        self.requests.append((text, speaker, tuple(history)))
        return ROUTES[text]


class _QueueConversation:
    """A ``Conversation`` whose input frames the test feeds; it records playback and flushes."""

    id = "queue-1"
    person: str | None = None

    def __init__(self) -> None:
        self._frames: asyncio.Queue[InputFrame | None] = asyncio.Queue()
        self._next_t_ms = 0
        self.frames_yielded = 0
        self.played: list[bytes] = []
        self.flushes: list[int] = []
        """``frames_yielded`` at each ``flush_playback`` call."""
        self.played_at_flushes: list[int] = []
        """``len(played)`` at each ``flush_playback`` call, taken then, not when a test next looks."""

    def feed(
        self, duration_ms: int, voiced: Callable[[int], Iterable[str]], pcm: bytes | None = None
    ) -> None:
        """Queue 20 ms frames for ``duration_ms``; ``voiced(t_ms)`` names who spoke in each."""
        for _ in range(duration_ms // 20):
            people = frozenset(voiced(self._next_t_ms))
            self._frames.put_nowait(InputFrame(pcm or silent_frame(RATE), RATE, people, self._next_t_ms))
            self._next_t_ms += 20

    async def frames(self) -> AsyncIterator[InputFrame]:
        while (frame := await self._frames.get()) is not None:
            self.frames_yielded += 1
            yield frame

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        assert rate == RATE
        self.played.append(pcm16_mono)

    def present(self, person: str) -> bool:
        return True

    def alone(self, person: str) -> bool:
        return False

    async def flush_playback(self) -> None:
        self.flushes.append(self.frames_yielded)
        self.played_at_flushes.append(len(self.played))

    async def notify(self, event: dict[str, Any]) -> None:
        pass

    async def close(self, reason: str) -> None:
        self._frames.put_nowait(None)


def _voiced(*spans: tuple[str, int, int]) -> Callable[[int], Iterable[str]]:
    """Who is voiced at each ``t_ms``: every person whose [start_ms, end_ms) span holds it."""
    return lambda t_ms: [person for person, start_ms, end_ms in spans if start_ms <= t_ms < end_ms]


def _input(delta: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {"type": "session.input_transcript.delta", "delta": delta, "start_ms": start_ms, "end_ms": end_ms}


def _output(delta: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {"type": "session.output_transcript.delta", "delta": delta, "start_ms": start_ms, "end_ms": end_ms}


def _delegation(delegation_id: str, offset_ms: int) -> dict[str, Any]:
    return {
        "type": "session.delegation.created",
        "delegation": {"id": delegation_id, "target": "client", "type": "delegation"},
        "offset_ms": offset_ms,
    }


def _audio(duration_ms: int, sample: int) -> dict[str, Any]:
    pcm = np.full(RATE * duration_ms // 1000, sample, dtype="<i2").tobytes()
    return {"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode("ascii")}


class _Harness:
    """The GPT-Live adapter on the fake server, joined to a real session, a stub router, and the fake
    Hermes API."""

    def __init__(self, tmp_path: Path, api: FakeHermesApi, server: FakeWsServer) -> None:
        self.api = api
        self.server = server
        self.router = _StubRouter()
        self.tracker = TaskTracker(tmp_path / "home")
        self.conversation = _QueueConversation()
        self._hermes = HermesApiClient(api.api_base, KEYS.get, sleep=lambda _s: asyncio.sleep(0.01))
        self.config = _config(tmp_path)
        self._permissions = PermissionChecker(self.config)
        self.active_profiles = _active_profiles(tmp_path, self._permissions)
        self._run: asyncio.Task[None] | None = None

    def start(self, clock: Callable[[], float] = time.monotonic) -> None:
        provider = GptLiveProvider("sk-test-0123456789abcdef", url=f"{self.server.url}/v1/live/sessions")
        session = VoiceSession(
            self.conversation,
            provider,
            router=self.router,
            hermes=self._hermes,
            tracker=self.tracker,
            permissions=self._permissions,
            active_profiles=self.active_profiles,
            prompt_context_for=lambda profile, *_: prompt_context(self.config, profile),
            default_profile=self.config.router.default_profile,
            clock=clock,
        )
        self._run = asyncio.create_task(session.run())

    async def stop(self) -> None:
        if self._run is not None:
            await self.conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
        await self._hermes.close()
        await self.tracker.close()

    def sent(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.server.client_events if event["type"] == event_type]

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                if self._run is not None and self._run.done():
                    self._run.result()
                    raise AssertionError("the session ended before the condition held")
                await asyncio.sleep(0.01)


def _active_profiles(tmp_path: Path, permissions: PermissionChecker) -> ActiveProfiles:
    """Everyone on stock-picker, the conversation's default profile too, so hearing someone does not
    restart the provider session unless a test switches someone first."""
    config = _config(tmp_path)
    return ActiveProfiles(tmp_path / "home" / "duplex" / "active_profiles.json", permissions, config)


def _config(tmp_path: Path) -> Any:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn", "default_profile": "stock-picker"},
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather"]},
            "stock-picker": {"description": "Portfolio and stocks", "examples": ["how's my portfolio"]},
        },
        "people": {
            "rob": {"discord_id": "123", "profiles": ["*"], "default_profile": "stock-picker"},
            "alex": {"discord_id": "456", "profiles": ["*"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={})


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@asynccontextmanager
async def _session(
    tmp_path: Path, api: FakeHermesApi, script: Iterable[Mapping[str, Any]]
) -> AsyncIterator[_Harness]:
    """A harness on a fake server playing ``script``, stopped (session, then server) on the way out."""
    async with FakeWsServer(script) as server:
        harness = _Harness(tmp_path, api, server)
        try:
            yield harness
        finally:
            await harness.stop()


async def test_gpt_live_delegation_is_worded_from_the_transcript(tmp_path: Path, api: FakeHermesApi) -> None:
    api.run_scripts.append(
        RunScript(
            events=[
                {"event": "run.started"},
                *(
                    {"event": "tool.started", "tool": "portfolio", "preview": f"step {n}"}
                    for n in range(1, 4)
                ),
                {"event": "run.completed", "output": "Up 2% today."},
            ]
        )
    )
    script = [
        *STARTED,
        _output("Hi Rob. Anything else?", 0, 800),
        _input("how is", 1000, 1400),
        _input(" my portfolio doing", 1400, 2200),
        _delegation("del_1", 2300),
        _output("Let me check.", 2400, 3000),  # after the offset: not context
    ]
    ticks = iter(range(3, 1000, 3))  # each progress event 3 s after the previous one
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(4000, _voiced(("rob", 1000, 2200)))
        harness.start(clock=lambda: float(next(ticks)))

        await harness.server.wait_for_client_event("session.commentary.append")

        # Lines end where the turn changes, since GPT-Live marks no utterance ends; the context last.
        history = ("Hi Rob. Anything else?", "how is my portfolio doing", "Anything else?")
        assert harness.router.requests == [("how is my portfolio doing", "rob", history)]
        task = harness.tracker.get(1)
        assert task is not None and task.text == "how is my portfolio doing" and task.delegation_id == "del_1"
        [start] = api.requests_to("POST", "/runs")
        assert start.body["input"] == (
            "how is my portfolio doing\n\n"
            '(Context: just before this request, the voice assistant said: "Anything else?")'
        )
        # The announcement, then progress at 3 s and 9 s; 6 s is within 5 s of a report.
        assert harness.sent("session.thinking.append") == [
            {"type": "session.thinking.append", "content": text, "delegation_id": "del_1"}
            for text in (
                'Started "how is my portfolio doing" on the stock-picker agent.',
                "portfolio: step 1",
                "portfolio: step 3",
            )
        ]
        assert harness.sent("session.commentary.append") == [
            {
                "type": "session.commentary.append",
                "content": frame_result("stock-picker", "Up 2% today.", 500),
                "delegation_id": "del_1",
            }
        ]


async def test_failed_gpt_live_task_is_a_short_apology_with_the_reason(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(
        RunScript(events=[{"event": "run.started"}, {"event": "run.failed", "error": "model overloaded"}])
    )
    script = [*STARTED, _input("how is my portfolio doing", 1000, 2200), _delegation("del_1", 2300)]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(4000, _voiced(("rob", 1000, 2200)))
        harness.start()

        commentary = await harness.server.wait_for_client_event("session.commentary.append")

        assert commentary == {
            "type": "session.commentary.append",
            "content": 'Sorry, "how is my portfolio doing" failed: model overloaded.',
            "delegation_id": "del_1",
        }


async def test_back_to_back_delegations_split_the_transcript(tmp_path: Path, api: FakeHermesApi) -> None:
    script = [
        *STARTED,
        _input("how is my", 1000, 1500),
        _input(" portfolio doing", 1500, 2100),
        _delegation("del_1", 2200),
        _input("and what about", 2300, 2700),
        _input(" Tesla", 2700, 2900),
        _delegation("del_2", 3000),  # 800 ms later: the first one's grace would reach "Tesla"
    ]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(5000, _voiced(("rob", 1000, 2900)))
        harness.start()

        await harness.wait_for(lambda: len(harness.sent("session.commentary.append")) == 2)

        assert [request[:2] for request in harness.router.requests] == [
            ("how is my portfolio doing", "rob"),
            ("and what about Tesla", "rob"),
        ]
        tasks = [harness.tracker.get(1), harness.tracker.get(2)]
        assert [(t.text, t.delegation_id) for t in tasks if t] == [
            ("how is my portfolio doing", "del_1"),
            ("and what about Tesla", "del_2"),
        ]


async def test_overlapping_speakers_get_their_own_tasks(tmp_path: Path, api: FakeHermesApi) -> None:
    # Rob asks, and Alex starts talking as Rob finishes; each delegation is credited to the person
    # voiced most in [offset - 4 s, offset + 500 ms] and worded from its own window.
    script = [
        *STARTED,
        _input("how is my", 1000, 1500),
        _input(" portfolio doing", 1500, 2000),
        _delegation("del_1", 2100),
        _input("what's", 2100, 2600),
        _input(" the weather", 2600, 3400),
        _delegation("del_2", 3000),
    ]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(5000, _voiced(("rob", 1000, 2000), ("alex", 2000, 3600)))
        harness.start()

        await harness.wait_for(lambda: len(harness.sent("session.commentary.append")) == 2)

        assert [request[:2] for request in harness.router.requests] == [
            ("how is my portfolio doing", "rob"),
            ("what's the weather", "alex"),
        ]
        sessions = [request.body["session_id"] for request in api.requests_to("POST", "/runs")]
        assert sessions == ["duplex-rob-stock-picker", "duplex-alex-stock-picker"]


async def test_credit_waits_for_input_past_the_offset(tmp_path: Path, api: FakeHermesApi) -> None:
    # Nobody spoke before the offset; Rob is voiced only in the 500 ms after it.
    script = [*STARTED, _input("what's the weather", 2000, 2400), _delegation("del_1", 2000)]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(2400, _voiced(("rob", 2100, 2400)))
        harness.start()
        await harness.wait_for(lambda: harness.conversation.frames_yielded == 120)
        await asyncio.sleep(0.1)
        assert harness.router.requests == [], "credited before the input passed offset + 500 ms"

        harness.conversation.feed(2000, _voiced())
        await harness.server.wait_for_client_event("session.commentary.append")

        assert harness.router.requests[0][:2] == ("what's the weather", "rob")


async def test_gpt_live_barge_in_flushes_within_one_tick(tmp_path: Path, api: FakeHermesApi) -> None:
    quiet_frames, loud_frames = 5, 15
    script = [
        *STARTED,
        _audio(2000, 100),  # the reply being talked over: 2 s, far more than the pacer lets out
        *[{"wait_for": "session.input_audio.append"}] * (quiet_frames + loud_frames),
        _audio(400, 200),  # a later reply, after the barge-in
    ]
    async with _session(tmp_path, api, script) as harness:
        conversation = harness.conversation
        harness.start()
        await harness.wait_for(lambda: bool(conversation.played))  # output is streaming

        conversation.feed(quiet_frames * 20, _voiced())
        conversation.feed(loud_frames * 20, _voiced(("rob", 0, 10_000)), LOUD_FRAME)
        await harness.wait_for(lambda: bool(conversation.flushes))
        # Taken at the flush itself: on a busy machine this test may wake only after the later reply
        # has started playing, and counting that audio as "before" would leave too little after.
        [played_before_flush] = conversation.played_at_flushes

        first_loud_frame = quiet_frames + 1
        [frames_at_flush] = conversation.flushes
        elapsed_ticks = frames_at_flush - first_loud_frame
        print(f"ticks between the gate trip and the flush: {elapsed_ticks}")
        assert elapsed_ticks <= 1

        later_bytes = RATE * 400 // 1000 * 2

        def after_flush() -> bytes:
            return b"".join(conversation.played[played_before_flush:])

        await harness.wait_for(lambda: len(after_flush()) >= later_bytes)
        await asyncio.sleep(0.1)
        played_after = np.frombuffer(after_flush(), dtype="<i2")
        assert set(played_after.tolist()) == {200}, "the held backlog of the interrupted reply was dropped"
        assert bytes_to_ms(played_after.size * 2, RATE) == 400, "the later reply plays in full"
        assert {event["type"] for event in harness.server.client_events} == {
            "session.start",
            "session.input_audio.append",
        }, "GPT-Live gets no truncate or other event for a barge-in"


async def test_a_context_change_restarts_gpt_live_with_new_instructions_and_the_task_still_reports(
    tmp_path: Path, api: FakeHermesApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single short session would end the call, so the restart must not count as one.
    monkeypatch.setattr(session_module, "MAX_SHORT_PROVIDER_SESSIONS", 1)
    run = RunScript(events=[{"event": "run.started"}])  # runs until the test completes it
    api.run_scripts.append(run)
    script = [
        *STARTED,
        _input("what's the weather", 1000, 1800),
        _delegation("del_1", 1900),
        {"wait_for": "session.close"},
        {"close": {}},
        {"wait_for": "session.start"},
        {"type": "session.started", "event_id": "event_2"},
    ]
    async with _session(tmp_path, api, script) as harness:
        # Alex is on stock-picker, as the conversation is; Rob has switched to general.
        await harness.active_profiles.switch("rob", "general")
        harness.conversation.feed(4000, _voiced(("alex", 1000, 1800)))
        harness.start()
        await harness.wait_for(lambda: bool(harness.sent("session.thinking.append")))

        harness.conversation.feed(200, _voiced(("rob", 0, 1_000_000)))
        await harness.wait_for(lambda: len(harness.sent("session.start")) == 2)
        run.events.append({"event": "run.completed", "output": "Sunny."})
        await harness.wait_for(lambda: bool(harness.sent("session.commentary.append")))

        first, second = harness.server.connections
        assert [event["type"] for event in first.events if event["type"] != "session.input_audio.append"] == [
            "session.start",
            "session.thinking.append",
            "session.close",
        ]
        start = second.events[0]
        assert start["session"]["instructions"] == GptLivePrompter().instructions(
            prompt_context(harness.config, "general")
        )
        [summary] = start["session"]["input"]
        assert (
            '"what\'s the weather" (internal id 1) on the stock-picker agent is running'
            in (summary["content"][0]["text"])
        )
        assert harness.sent("session.commentary.append") == [
            {
                "type": "session.commentary.append",
                "content": frame_result("stock-picker", "Sunny.", 500),
                "delegation_id": None,
            }
        ]


async def test_a_switch_handed_off_as_words_says_so_then_restarts_gpt_live_on_the_new_profile(
    tmp_path: Path, api: FakeHermesApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "RESTART_QUIET_S", 0.05)
    monkeypatch.setattr(session_module, "SWITCH_LINE_SETTLE_S", 0.05)
    script = [
        *STARTED,
        _input("switch to general", 1000, 1800),
        _delegation("del_1", 1900),
        {"wait_for": "session.commentary.append"},
        _audio(300, 0),  # the model says the line
        {"wait_for": "session.close"},
        {"close": {}},
        {"wait_for": "session.start"},
        {"type": "session.started", "event_id": "event_2"},
    ]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(4000, _voiced(("rob", 1000, 1800)))
        harness.start()
        await harness.wait_for(lambda: len(harness.server.connections) == 2)
        await asyncio.sleep(0.1)

        assert harness.active_profiles.get("rob") == "general"
        first, second = harness.server.connections
        assert [event["type"] for event in first.events if event["type"] != "session.input_audio.append"] == [
            "session.start",
            "session.commentary.append",
            "session.close",
        ], "no task was started or announced"
        assert harness.conversation.played, "the line was played before the restart"
        assert second.events[0]["session"]["instructions"] == GptLivePrompter().instructions(
            prompt_context(harness.config, "general")
        )
        assert harness.sent("session.commentary.append") == [
            {"type": "session.commentary.append", "content": switching_to("general"), "delegation_id": None}
        ], "the line before the restart is the only confirmation"
        assert harness.router.requests == []
        assert api.requests_to("POST", "/runs") == []


async def test_a_no_handed_off_as_words_answers_the_switch_question_and_runs_on_the_active_profile(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(RunScript(events=[{"event": "run.started"}]))  # stays open
    script = [
        *STARTED,
        _input("what's the weather tomorrow", 1000, 1800),
        _delegation("del_1", 1900),
        {"wait_for": "session.commentary.append"},  # the switch question
        _input("no thanks", 2500, 2800),
        _delegation("del_2", 2900),
    ]
    async with _session(tmp_path, api, script) as harness:
        harness.conversation.feed(6000, _voiced(("rob", 1000, 1800), ("rob", 2500, 2800)))
        harness.start()
        await harness.wait_for(lambda: bool(harness.sent("session.thinking.append")))

        assert harness.sent("session.commentary.append") == [
            {"type": "session.commentary.append", "content": suggest_switch("general"), "delegation_id": None}
        ]
        assert [(text, speaker) for text, speaker, _ in harness.router.requests] == [
            ("what's the weather tomorrow", "rob")
        ], "the answer is never routed"
        assert harness.active_profiles.get("rob") == "stock-picker"
        (run,) = api.requests_to("POST", "/runs")
        assert run.path == "/p/stock-picker/v1/runs"
        assert run.body["input"] == "what's the weather tomorrow"
        assert harness.sent("session.thinking.append") == [
            {
                "type": "session.thinking.append",
                "content": 'Started "what\'s the weather tomorrow" on the stock-picker agent.',
                "delegation_id": "del_1",
            }
        ]
