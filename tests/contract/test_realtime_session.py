"""VoiceSession with the real Realtime adapter over the fake WebSocket server: barge-in with truncation,
stale function calls, the announcement queue, and the locally answered task tools."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.audio.frames import silent_frame
from hermes_duplex.config import load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.prompts.base import TaskReport, json_report
from hermes_duplex.prompts.notices import (
    NO_SUCH_TASK,
    suggest_switch,
    switched_to,
    task_summary,
    which_to_cancel,
)
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.realtime.adapter import Dialect, RealtimeApiProvider
from hermes_duplex.providers.realtime.openai.dialect import OPENAI
from hermes_duplex.providers.realtime.xai.dialect import XAI
from hermes_duplex.router.base import ControlIntent, Effort, RouteDecision
from hermes_duplex.service import prompt_context
from hermes_duplex.session import ANNOUNCEMENT_MAX_WAIT_S, VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript
from tests.fakes.fake_ws_server import FakeWsServer

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
STARTED = [
    {"type": "session.created"},
    {"wait_for": "session.update"},
    {"type": "session.updated"},
]
RECEIPT_BUDGET_MS = 50
TRUNCATE_TOLERANCE_MS = 20

# A run that starts and never ends on its own, so it stays open for status and cancel.
OPEN_RUN = RunScript(events=[{"event": "run.started"}])

STARTED_WEATHER = 'Started "what\'s the weather" on the stock-picker agent.'


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
    "what's the weather": _decision(),
    "what's the weather tomorrow": _decision(suggestion="general"),
    "cancel that": _decision("cancel"),
}


class _StubRouter:
    """Routes each task text to a fixed decision and records every request with its hint."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

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
        self.requests.append((text, speaker, active_profile))
        return ROUTES[text]


class _Clock:
    """A clock the test moves by hand, in seconds; it counts how often it was read."""

    def __init__(self) -> None:
        self.now_s = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now_s


class _QueueConversation:
    """A ``Conversation`` whose input frames the test feeds; it records when playback starts and flushes."""

    id = "queue-1"
    person: str | None = None

    def __init__(self) -> None:
        self._frames: asyncio.Queue[InputFrame | None] = asyncio.Queue()
        self._next_t_ms = 0
        self._played_bytes = 0
        self.first_play_s: float | None = None
        """``time.monotonic()`` of the first ``play`` call."""
        self.flushes = 0

    def feed(self, duration_ms: int, voiced: Callable[[int], Iterable[str]]) -> None:
        """Queue 20 ms frames for ``duration_ms``; ``voiced(t_ms)`` names who spoke in each."""
        for _ in range(duration_ms // 20):
            people = frozenset(voiced(self._next_t_ms))
            self._frames.put_nowait(InputFrame(silent_frame(RATE), RATE, people, self._next_t_ms))
            self._next_t_ms += 20

    async def frames(self) -> AsyncIterator[InputFrame]:
        while (frame := await self._frames.get()) is not None:
            yield frame

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        assert rate == RATE
        if self.first_play_s is None:
            self.first_play_s = time.monotonic()
        self._played_bytes += len(pcm16_mono)

    @property
    def played_ms(self) -> int:
        """Audio handed to ``play`` so far, in whole ms."""
        return self._played_bytes * 1000 // (2 * RATE)

    def present(self, person: str) -> bool:
        return True

    async def flush_playback(self) -> None:
        self.flushes += 1

    async def notify(self, event: dict[str, Any]) -> None:
        pass

    async def close(self, reason: str) -> None:
        self._frames.put_nowait(None)


def _rob(t_ms: int) -> Iterable[str]:
    return ("rob",)


def _audio(duration_ms: int, item_id: str) -> dict[str, Any]:
    pcm = bytes(2 * RATE * duration_ms // 1000)
    return {
        "type": "response.output_audio.delta",
        "response_id": "resp_audio",
        "item_id": item_id,
        "delta": base64.b64encode(pcm).decode("ascii"),
    }


def _call(call_id: str, name: str, arguments: dict[str, Any], response_id: str) -> dict[str, Any]:
    return {
        "type": "response.function_call_arguments.done",
        "response_id": response_id,
        "item_id": f"item_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _created(response_id: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}


def _done(response_id: str, status: str = "completed") -> dict[str, Any]:
    return {"type": "response.done", "response": {"id": response_id, "status": status}}


def _responding(call_id: str, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """A response that only calls ``name``."""
    response_id = f"resp_{call_id}"
    return [_created(response_id), _call(call_id, name, arguments, response_id), _done(response_id)]


class _Harness:
    """The Realtime adapter on the fake server, joined to a real session, a stub router, and the fake
    Hermes API."""

    def __init__(self, tmp_path: Path, api: FakeHermesApi, server: FakeWsServer) -> None:
        self.api = api
        self.server = server
        self.router = _StubRouter()
        self.clock = _Clock()
        self.tracker = TaskTracker(tmp_path / "home")
        self.conversation = _QueueConversation()
        self._hermes = HermesApiClient(api.api_base, KEYS.get, sleep=lambda _s: asyncio.sleep(0.01))
        self.config = _config(tmp_path)
        self._permissions = PermissionChecker(self.config)
        self.active_profiles = _active_profiles(tmp_path, self._permissions)
        self._run: asyncio.Task[None] | None = None
        self._answered_requests = 0
        self.dialect = OPENAI
        """The Realtime dialect ``start`` speaks."""

    async def start(self) -> None:
        provider = RealtimeApiProvider(
            "sk-test-0123456789abcdef", dialect=self.dialect, url=f"{self.server.url}/v1/realtime"
        )
        session = VoiceSession(
            self.conversation,
            provider,
            router=self.router,
            hermes=self._hermes,
            tracker=self.tracker,
            permissions=self._permissions,
            active_profiles=self.active_profiles,
            prompt_context_for=lambda profile: prompt_context(self.config, profile),
            default_profile=self.config.router.default_profile,
            clock=self.clock,
        )
        self._run = asyncio.create_task(session.run())
        await self.wait_for(lambda: bool(self.sent("session.update")))

    async def stop(self) -> None:
        if self._run is not None:
            await self.conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
        await self._hermes.close()
        await self.tracker.close()

    def sent(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.server.client_events if event["type"] == event_type]

    def outputs(self) -> dict[str, dict[str, Any]]:
        """Every function output the session sent, by call id, with its payload parsed."""
        items = [event["item"] for event in self.sent("conversation.item.create")]
        return {
            item["call_id"]: json.loads(item["output"])
            for item in items
            if item["type"] == "function_call_output"
        }

    def announcements(self) -> list[str]:
        """The text of every announcement the session sent, oldest first: out of band, or a question
        spoken in the conversation."""
        return [
            event["response"]["input"][0]["content"][0]["text"]
            for event in self.sent("response.create")
            if "input" in event.get("response", {})
        ]

    async def answer_requests(self) -> None:
        """Play the model's side of every response the session asked for since the last call: each
        starts and is done at once, saying nothing."""
        requests = self.sent("response.create")
        for number in range(self._answered_requests, len(requests)):
            await self.server.send(_created(f"resp_requested_{number}"), _done(f"resp_requested_{number}"))
        self._answered_requests = len(requests)

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                if self._run is not None and self._run.done():
                    self._run.result()
                    raise AssertionError("the session ended before the condition held")
                await asyncio.sleep(0.005)


def _active_profiles(tmp_path: Path, permissions: PermissionChecker) -> ActiveProfiles:
    """Everyone on their default profile: rob on stock-picker, alex on general."""
    config = _config(tmp_path)
    return ActiveProfiles(tmp_path / "home" / "duplex" / "active_profiles.json", permissions, config)


def _config(tmp_path: Path) -> Any:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather"]},
            "stock-picker": {"description": "Portfolio and stocks", "examples": ["how's my portfolio"]},
        },
        "people": {"rob": {"discord_id": "123", "profiles": ["*"], "default_profile": "stock-picker"}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={})


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@asynccontextmanager
async def _session(tmp_path: Path, api: FakeHermesApi) -> AsyncIterator[_Harness]:
    """A started harness on a fake server that is configured and then driven by the test; Rob has
    been talking for a second before the test starts."""
    async with FakeWsServer(STARTED) as server:
        harness = _Harness(tmp_path, api, server)
        harness.conversation.feed(1000, _rob)
        try:
            await harness.start()
            await harness.wait_for(lambda: len(harness.sent("input_audio_buffer.append")) == 50)
            yield harness
        finally:
            await harness.stop()


async def _delegate_portfolio(harness: _Harness) -> None:
    """Have the model delegate the portfolio question, and wait until its start is announced."""
    await harness.server.send(*_responding("call_1", "delegate", {"task": "how is my portfolio doing"}))
    await harness.wait_for(lambda: "call_1" in harness.outputs())
    await harness.answer_requests()
    await harness.wait_for(
        lambda: harness.announcements() == ['Started "how is my portfolio doing" on the stock-picker agent.']
    )
    await harness.answer_requests()


async def test_barge_in_flushes_playback_and_truncates_the_item_to_the_ms_played(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await harness.server.send(_created("resp_1"), _audio(3000, "item_asst_1"))
        await harness.wait_for(lambda: harness.conversation.first_play_s is not None)
        assert harness.conversation.first_play_s is not None
        await asyncio.sleep(max(0.0, harness.conversation.first_play_s + 0.8 - time.monotonic()))

        await harness.server.send({"type": "input_audio_buffer.speech_started", "item_id": "item_user_2"})
        truncate = await harness.server.wait_for_client_event("conversation.item.truncate")
        played_ms = harness.conversation.played_ms
        await asyncio.sleep(0.1)

        print(f"truncate sent audio_end_ms={truncate['audio_end_ms']}")
        assert truncate["item_id"] == "item_asst_1"
        assert abs(truncate["audio_end_ms"] - 800) <= TRUNCATE_TOLERANCE_MS
        assert harness.conversation.flushes == 1
        assert harness.conversation.played_ms == played_ms, "the held backlog was dropped, not played"
        assert harness.sent("response.cancel") == [], "the server cancels the response itself"


async def test_function_call_from_the_interrupted_response_is_not_executed(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await harness.server.send(_created("resp_1"), _audio(2000, "item_asst_1"))
        await harness.wait_for(lambda: harness.conversation.played_ms > 0)
        await harness.server.send(
            {"type": "input_audio_buffer.speech_started", "item_id": "item_user_2"},
            _call("call_stale", "delegate", {"task": "how is my portfolio doing"}, "resp_1"),
            _done("resp_1", status="cancelled"),
            {"type": "input_audio_buffer.speech_stopped", "item_id": "item_user_2"},
            *_responding("call_2", "delegate", {"task": "what's the weather"}),
        )

        await harness.wait_for(lambda: "call_2" in harness.outputs())
        await asyncio.sleep(0.1)

        assert list(harness.outputs()) == ["call_2"]
        assert harness.router.requests == [("what's the weather", "rob", "stock-picker")]
        assert [start.path for start in api.requests_to("POST", "/runs")] == ["/p/stock-picker/v1/runs"]


async def test_announcement_waits_for_speech_stopped_the_model_and_playback(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    run = RunScript(events=[{"event": "run.started"}])  # runs until the test completes it
    api.run_scripts.append(run)
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)
        await harness.server.send({"type": "input_audio_buffer.speech_started", "item_id": "item_user_2"})
        await asyncio.sleep(0.05)

        run.events.append({"event": "run.completed", "output": "Up 2% today."})
        await harness.wait_for(
            lambda: (task := harness.tracker.get(1)) is not None and task.state == "completed"
        )
        await asyncio.sleep(0.2)
        assert len(harness.announcements()) == 1, "held while Rob talks"

        # Rob stops, and the model answers him with 300 ms of audio.
        await harness.server.send({"type": "input_audio_buffer.speech_stopped", "item_id": "item_user_2"})
        await asyncio.sleep(0.2)
        assert len(harness.announcements()) == 1, "held for the model's answer to Rob's turn"
        answered_s = time.monotonic()
        await harness.server.send(_created("resp_2"), _audio(300, "item_asst_2"), _done("resp_2"))

        await harness.wait_for(lambda: len(harness.announcements()) == 2)
        waited_s = time.monotonic() - answered_s

        print(f"announced {waited_s * 1000:.0f} ms after the model's 300 ms answer arrived")
        assert waited_s >= 0.25, "held until playback drained"
        assert harness.conversation.played_ms == 300
        assert harness.announcements()[1] == json_report(
            TaskReport(1, "how is my portfolio doing", "stock-picker", "completed", "Up 2% today."), 500
        )
        [announcement] = [
            event for event in harness.sent("response.create") if event.get("response", {}).get("input")
        ][1:]
        assert announcement["response"]["tools"] == [], "announcement turns have tools disabled"


async def test_announcement_is_spoken_anyway_after_the_wait_cap(tmp_path: Path, api: FakeHermesApi) -> None:
    run = RunScript(events=[{"event": "run.started"}])
    api.run_scripts.append(run)
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)
        # Speech starts and its stop is never seen.
        await harness.server.send({"type": "input_audio_buffer.speech_started", "item_id": "item_user_2"})
        await asyncio.sleep(0.05)
        reads_before = harness.clock.reads
        run.events.append({"event": "run.completed", "output": "Up 2% today."})
        # The announcement's wait starts at its first clock read; move the clock only after it.
        await harness.wait_for(lambda: harness.clock.reads > reads_before)

        harness.clock.now_s += ANNOUNCEMENT_MAX_WAIT_S - 1
        await asyncio.sleep(0.2)
        assert len(harness.announcements()) == 1

        harness.clock.now_s += 1
        await harness.wait_for(lambda: len(harness.announcements()) == 2)

        assert harness.announcements()[1] == json_report(
            TaskReport(1, "how is my portfolio doing", "stock-picker", "completed", "Up 2% today."), 500
        )


async def test_cancel_task_tool_stops_the_run_and_answers_in_the_function_output(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)

        await harness.server.send(*_responding("call_2", "cancel_task", {}))
        await harness.wait_for(lambda: "call_2" in harness.outputs())

        assert harness.outputs()["call_2"] == {
            "status": "ok",
            "message": 'Cancelled "how is my portfolio doing".',
        }
        task = harness.tracker.get(1)
        assert task is not None and task.state == "cancelled"
        [stop] = api.requests_to("POST", "/stop")
        assert stop.path == f"/p/stock-picker/v1/runs/{task.run_id}/stop"
        assert harness.router.requests == [("how is my portfolio doing", "rob", "stock-picker")], (
            "never routed"
        )


async def test_control_intent_cancel_routed_from_delegate_stops_the_run_and_is_confirmed(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)

        await harness.server.send(*_responding("call_2", "delegate", {"task": "cancel that"}))
        await harness.wait_for(lambda: "call_2" in harness.outputs())
        await harness.answer_requests()
        await harness.wait_for(lambda: len(harness.announcements()) == 2)

        assert harness.outputs()["call_2"] == {"status": "received"}
        assert harness.announcements()[1] == 'Cancelled "how is my portfolio doing".'
        task = harness.tracker.get(1)
        assert task is not None and task.state == "cancelled"
        assert len(api.requests_to("POST", "/stop")) == 1


async def test_task_status_tool_answers_from_the_tracker(tmp_path: Path, api: FakeHermesApi) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)

        await harness.server.send(
            *_responding("call_2", "task_status", {}), *_responding("call_3", "task_status", {"task_id": 4})
        )
        await harness.wait_for(lambda: "call_3" in harness.outputs())

        running = harness.tracker.get(1)
        assert running is not None
        listed = [
            {"id": 1, "topic": "how is my portfolio doing", "agent": "stock-picker", "state": "running"}
        ]
        assert harness.outputs()["call_2"] == {
            "status": "ok",
            "message": task_summary([running]),
            "tasks": listed,
        }
        assert harness.outputs()["call_3"] == {"status": "ok", "message": NO_SUCH_TASK, "tasks": listed}


async def test_status_and_cancel_tools_find_the_task_the_users_words_are_about(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts += [OPEN_RUN, RunScript(events=[{"event": "run.started"}])]
    async with _session(tmp_path, api) as harness:
        await _delegate_portfolio(harness)
        await harness.server.send(*_responding("call_2", "delegate", {"task": "what's the weather"}))
        await harness.wait_for(
            lambda: (task := harness.tracker.get(2)) is not None and task.state == "running"
        )

        await harness.server.send(*_responding("call_3", "task_status", {"about": "the weather"}))
        await harness.wait_for(lambda: "call_3" in harness.outputs())
        await harness.server.send(*_responding("call_4", "cancel_task", {}))
        await harness.wait_for(lambda: "call_4" in harness.outputs())
        await harness.server.send(*_responding("call_5", "cancel_task", {"about": "my portfolio"}))
        await harness.wait_for(lambda: "call_5" in harness.outputs())

        portfolio, weather = harness.tracker.get(1), harness.tracker.get(2)
        assert portfolio is not None and weather is not None
        assert harness.outputs()["call_3"]["message"] == task_summary([weather])
        assert [entry["topic"] for entry in harness.outputs()["call_3"]["tasks"]] == [
            "how is my portfolio doing",
            "what's the weather",
        ]
        assert harness.outputs()["call_4"]["message"] == which_to_cancel([portfolio, weather])
        assert harness.outputs()["call_5"]["message"] == 'Cancelled "how is my portfolio doing".'
        assert portfolio.state == "cancelled" and weather.state == "running"
        assert len(api.requests_to("POST", "/stop")) == 1


async def test_delegate_receipt_comes_at_once_and_the_task_reaches_the_router(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await harness.server.send(*_responding("call_1", "delegate", {"task": "what's the weather"}))
        sent_s = time.perf_counter()
        await harness.wait_for(lambda: "call_1" in harness.outputs(), timeout_s=1)
        receipt_ms = (time.perf_counter() - sent_s) * 1000

        print(f"delegate receipt reached the provider {receipt_ms:.1f} ms after the call was sent")
        assert receipt_ms < RECEIPT_BUDGET_MS
        assert harness.outputs()["call_1"] == {"status": "received"}
        await harness.wait_for(lambda: bool(harness.router.requests))
        assert harness.router.requests == [("what's the weather", "rob", "stock-picker")]


async def _response_create_waits_for_the_calling_response(
    harness: _Harness, call_id: str, name: str, arguments: dict[str, Any]
) -> None:
    await harness.server.send(_created(f"resp_{call_id}"), _call(call_id, name, arguments, f"resp_{call_id}"))
    sent_s = time.perf_counter()
    await harness.wait_for(lambda: call_id in harness.outputs(), timeout_s=1)
    receipt_ms = (time.perf_counter() - sent_s) * 1000
    await asyncio.sleep(0.2)
    early = harness.sent("response.create")
    assert receipt_ms < RECEIPT_BUDGET_MS
    assert early == [], "response.create was sent while the calling response was still active"
    await harness.server.send(_done(f"resp_{call_id}"))
    await harness.wait_for(lambda: bool(harness.sent("response.create")), timeout_s=1)
    assert list(harness.outputs()) == [call_id]


async def test_delegate_output_asks_for_a_response_only_after_the_calling_response_is_done(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await _response_create_waits_for_the_calling_response(
            harness, "call_1", "delegate", {"task": "what's the weather"}
        )


async def test_task_status_output_asks_for_a_response_only_after_the_calling_response_is_done(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await _response_create_waits_for_the_calling_response(harness, "call_2", "task_status", {})


async def test_outputs_of_one_response_share_one_reply_and_the_floor_frees_after_it(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await harness.server.send(
            _created("resp_1"),
            _call("call_1", "delegate", {"task": "what's the weather"}, "resp_1"),
            _call("call_2", "task_status", {"task_id": 4}, "resp_1"),
        )
        await harness.wait_for(lambda: {"call_1", "call_2"} <= set(harness.outputs()))
        await harness.server.send(_done("resp_1"))
        await harness.wait_for(lambda: bool(harness.sent("response.create")))
        await asyncio.sleep(0.1)
        assert harness.sent("response.create") == [{"type": "response.create"}]

        # Once the one reply is done the floor is free, so the start notice is spoken without waiting.
        await harness.answer_requests()
        await harness.wait_for(lambda: harness.announcements() == [STARTED_WEATHER], timeout_s=1)


async def test_reply_to_a_call_whose_response_a_person_cut_off_is_dropped_and_frees_the_floor(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts.append(OPEN_RUN)
    async with _session(tmp_path, api) as harness:
        await harness.server.send(
            _created("resp_1"), _call("call_1", "delegate", {"task": "what's the weather"}, "resp_1")
        )
        await harness.wait_for(lambda: "call_1" in harness.outputs())
        await harness.server.send(
            {"type": "input_audio_buffer.speech_started", "item_id": "item_user_2"},
            _done("resp_1", "cancelled"),
        )
        await asyncio.sleep(0.1)
        assert harness.sent("response.create") == [], "no reply talks over the person"

        # Rob finishes and the model answers his turn; nothing else is due, so the floor is free.
        await harness.server.send(
            {"type": "input_audio_buffer.speech_stopped", "item_id": "item_user_2"},
            _created("resp_turn"),
            _done("resp_turn"),
        )
        await harness.wait_for(lambda: harness.announcements() == [STARTED_WEATHER], timeout_s=1)


@pytest.mark.parametrize("dialect", [OPENAI, XAI], ids=["openai", "xai"])
async def test_a_context_change_replaces_only_the_instructions_in_place(
    tmp_path: Path, api: FakeHermesApi, dialect: Dialect
) -> None:
    # The conversation starts on general, the default profile; hearing Rob moves it to stock-picker.
    script = [*STARTED, {"wait_for": "session.update"}, {"type": "session.updated"}]
    async with FakeWsServer(script) as server:
        harness = _Harness(tmp_path, api, server)
        harness.dialect = dialect
        harness.conversation.feed(200, _rob)
        try:
            await harness.start()
            await harness.wait_for(lambda: len(harness.sent("session.update")) == 2)
        finally:
            await harness.stop()

    connect, update = harness.sent("session.update")
    prompter = dialect.prompter
    assert connect["session"]["instructions"] == prompter.instructions(
        prompt_context(harness.config, "general")
    )
    assert "tools" in connect["session"]
    assert update == {
        "type": "session.update",
        "event_id": "duplex_instructions_1",
        "session": dialect.instructions_config(
            prompter.instructions(prompt_context(harness.config, "stock-picker"))
        ),
    }
    assert "tools" not in update["session"]
    assert len(server.connections) == 1


async def test_switch_profile_tool_switches_updates_the_instructions_and_confirms_it(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await harness.wait_for(lambda: len(harness.sent("session.update")) == 2)  # Rob was heard
        await harness.server.send({"type": "session.updated"})

        await harness.server.send(*_responding("call_1", "switch_profile", {"profile": "general"}))
        await harness.wait_for(lambda: "call_1" in harness.outputs())
        await harness.answer_requests()
        await harness.wait_for(lambda: bool(harness.announcements()))
        await harness.wait_for(lambda: len(harness.sent("session.update")) == 3)

        assert harness.outputs()["call_1"] == {"status": "switched"}
        assert harness.announcements() == [switched_to("general")]
        assert harness.active_profiles.get("rob") == "general"
        update = harness.sent("session.update")[2]
        assert update["session"] == OPENAI.instructions_config(
            OPENAI.prompter.instructions(prompt_context(harness.config, "general"))
        )
        assert harness.router.requests == []
        assert api.requests_to("POST", "/runs") == []


async def test_a_suggested_switch_is_asked_after_the_receipt_and_confirm_switch_runs_it_there(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await harness.wait_for(lambda: len(harness.sent("session.update")) == 2)  # Rob was heard

        await harness.server.send(*_responding("call_1", "delegate", {"task": "what's the weather tomorrow"}))
        sent_s = time.perf_counter()
        await harness.wait_for(lambda: "call_1" in harness.outputs(), timeout_s=1)
        receipt_ms = (time.perf_counter() - sent_s) * 1000
        await harness.answer_requests()
        await harness.wait_for(lambda: bool(harness.announcements()))

        assert receipt_ms < RECEIPT_BUDGET_MS
        assert harness.outputs()["call_1"] == {"status": "received"}
        assert harness.announcements() == [suggest_switch("general")], "the question follows the receipt"
        assert api.requests_to("POST", "/runs") == []

        await harness.answer_requests()
        await harness.server.send(*_responding("call_2", "confirm_switch", {"accept": True}))
        await harness.wait_for(lambda: "call_2" in harness.outputs())
        await harness.answer_requests()
        await harness.wait_for(lambda: len(harness.announcements()) == 2)
        await harness.answer_requests()
        await harness.wait_for(lambda: len(harness.announcements()) == 3)

        assert harness.outputs()["call_2"] == {"status": "switched"}
        assert harness.announcements()[1:] == [
            switched_to("general"),
            'Started "what\'s the weather tomorrow" on the general agent.',
        ]
        assert harness.active_profiles.get("rob") == "general"
        (run,) = api.requests_to("POST", "/runs")
        assert run.path == "/p/general/v1/runs"
        assert harness.router.requests == [("what's the weather tomorrow", "rob", "stock-picker")]
