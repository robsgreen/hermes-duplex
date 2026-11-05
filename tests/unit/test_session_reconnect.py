"""VoiceSession across provider reconnects: backoff, re-seeding, a fresh input timeline, and tasks that
outlive the provider session that started them, with the fake provider."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex import session as session_module
from hermes_duplex.audio.frames import silent_frame
from hermes_duplex.config import load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.prompts.notices import NOT_HEARD, PROVIDER_LOST
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.fake.adapter import FakeProvider, SentCall
from hermes_duplex.providers.gpt_live.prompt import GptLivePrompter, frame_result
from hermes_duplex.providers.wire import WireError
from hermes_duplex.router.base import ControlIntent, Effort, RouteDecision
from hermes_duplex.service import prompt_context
from hermes_duplex.session import PROVIDER_LOST_REASON, VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
LIVE = {"delegation_style": "live", "emits_speech_events": False, "supports_truncate": False}


def _decision(control_intent: ControlIntent | None = None) -> RouteDecision:
    return RouteDecision(
        effort="medium",
        model=None,
        suggestion=None,
        control_intent=control_intent,
        probabilities={},
        source="knn",
        latency_ms=1,
    )


ROUTES = {"how is my portfolio doing": _decision(), "what's the weather": _decision()}


class _StubRouter:
    """Routes each task text to a fixed decision and records who asked what."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

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
        self.requests.append((text, speaker))
        return ROUTES[text]


class _QueueConversation:
    """A ``Conversation`` whose input frames the test feeds; ``t_ms`` never restarts, as in a real
    transport."""

    id = "queue-1"
    person: str | None = None

    def __init__(self) -> None:
        self._frames: asyncio.Queue[InputFrame | None] = asyncio.Queue()
        self._next_t_ms = 0
        self.notified: list[dict[str, Any]] = []
        self.closed_reason: str | None = None

    def feed(self, duration_ms: int, voiced: Callable[[int], Iterable[str]] = lambda _: ()) -> None:
        """Queue 20 ms frames for ``duration_ms``; ``voiced(n)`` names who spoke in the n-th ms fed."""
        for elapsed_ms in range(0, duration_ms, 20):
            people = frozenset(voiced(elapsed_ms))
            self._frames.put_nowait(InputFrame(silent_frame(RATE), RATE, people, self._next_t_ms))
            self._next_t_ms += 20

    async def frames(self) -> AsyncIterator[InputFrame]:
        while (frame := await self._frames.get()) is not None:
            yield frame
            # Paced, if faster than real time, so provider events are handled near the input they
            # follow: the fake places transcript deltas at the input sent when they are handled.
            await asyncio.sleep(0.001)

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        pass

    def present(self, person: str) -> bool:
        return True

    async def flush_playback(self) -> None:
        pass

    async def notify(self, event: dict[str, Any]) -> None:
        self.notified.append(event)

    async def close(self, reason: str) -> None:
        if self.closed_reason is None:
            self.closed_reason = reason
            self._frames.put_nowait(None)


class _UnreachableAfterFirstConnect(FakeProvider):
    """A fake provider that connects once; every later connect fails as an unreachable server would."""

    def __init__(self, script: Iterable[Mapping[str, Any]]) -> None:
        super().__init__(script)
        self.connect_attempts = 0

    async def connect(self, instructions: str, voice: str | None, history_summary: str | None) -> None:
        self.connect_attempts += 1
        if self.connect_attempts > 1:
            raise WireError("the voice provider is unreachable")
        await super().connect(instructions, voice, history_summary)


class _Harness:
    """A session on a fake provider, a stub router, and the fake Hermes API; reconnect waits are
    recorded instead of slept."""

    def __init__(self, tmp_path: Path, api: FakeHermesApi) -> None:
        self.api = api
        self.router = _StubRouter()
        self.tracker = TaskTracker(tmp_path / "home")
        self.conversation = _QueueConversation()
        self.reconnect_waits_s: list[float] = []
        self.config = _config(tmp_path)
        self._permissions = PermissionChecker(self.config)
        self.active_profiles = _active_profiles(tmp_path, self._permissions)
        self._hermes = HermesApiClient(api.api_base, KEYS.get, sleep=lambda _s: asyncio.sleep(0.01))
        self._run: asyncio.Task[None] | None = None
        self.provider = FakeProvider()

    def start(self, provider: FakeProvider) -> None:
        self.provider = provider

        async def record_wait(delay_s: float) -> None:
            self.reconnect_waits_s.append(delay_s)

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
            sleep=record_wait,
        )
        self._run = asyncio.create_task(session.run())

    async def stop(self) -> None:
        if self._run is not None:
            await self.conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
        await self._hermes.close()
        await self.tracker.close()

    def sent(self, method: str) -> list[SentCall]:
        return [call for call in self.provider.calls if call.method == method]

    def calls_after_reconnect(self) -> list[SentCall]:
        calls = list(self.provider.calls)
        second_connect = [i for i, call in enumerate(calls) if call.method == "connect"][1]
        return calls[second_connect + 1 :]

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                await asyncio.sleep(0.01)


def _active_profiles(tmp_path: Path, permissions: PermissionChecker) -> ActiveProfiles:
    """Everyone on their default profile: rob on stock-picker, which the conversation also starts on,
    and alex on general."""
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
            "alex": {"discord_id": "456", "profiles": ["general"]},
            "carol": {"discord_id": "789", "profiles": ["general"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={})


def _voiced(person: str, start_ms: int = 0, end_ms: int = 1_000_000) -> Callable[[int], Iterable[str]]:
    return lambda t_ms: (person,) if start_ms <= t_ms < end_ms else ()


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@pytest.fixture
async def harness(tmp_path: Path, api: FakeHermesApi) -> AsyncIterator[_Harness]:
    harness = _Harness(tmp_path, api)
    yield harness
    await harness.stop()


async def test_provider_reconnect_reseeds_and_announces_the_running_task(
    harness: _Harness, api: FakeHermesApi
) -> None:
    run = RunScript(events=[{"event": "run.started"}])  # runs until the test completes it
    api.run_scripts.append(run)
    script = [
        {"at_input_ms": 200, "event": {"type": "input_transcript", "delta": "how is my portfolio doing"}},
        {"at_input_ms": 200, "event": {"type": "output_transcript", "delta": "Let me check."}},
        {"at_input_ms": 200, "event": {"type": "output_done"}},
        {
            "at_input_ms": 400,
            "event": {
                "type": "delegation",
                "id": "call_1",
                "tool_args": {"task": "how is my portfolio doing"},
            },
        },
        # The socket closes mid-task, right after the task's start was announced.
        {"on_sent": "say", "event": {"type": "close", "reason": "connection_lost"}},
    ]
    harness.conversation.feed(1000, _voiced("rob"))
    harness.start(FakeProvider(script))

    await harness.wait_for(lambda: len(harness.sent("connect")) == 2)
    run.events.append({"event": "run.completed", "output": "Up 2% today."})
    result = frame_result("stock-picker", "Up 2% today.", 500)
    await harness.wait_for(lambda: any(call.args["text"] == result for call in harness.sent("say")))

    first, second = harness.sent("connect")
    print(f"connect calls: {[call.args['history_summary'] for call in (first, second)]}")
    assert first.args["history_summary"] is None
    summary = second.args["history_summary"]
    assert "user: how is my portfolio doing" in summary
    assert "assistant: Let me check." in summary
    assert '"how is my portfolio doing" (internal id 1) on the stock-picker agent is running' in summary
    assert second.args["instructions"] == first.args["instructions"]
    assert harness.reconnect_waits_s == [0.5]
    # The new provider session never requested call_1, so the result is spoken as a notice.
    assert [call.method for call in harness.calls_after_reconnect()] == ["say"]
    task = harness.tracker.get(1)
    assert task is not None and task.state == "completed"
    assert harness.conversation.closed_reason is None


async def test_attribution_across_reconnect_uses_the_new_session_timeline(
    harness: _Harness, api: FakeHermesApi
) -> None:
    script = [
        {
            "at_input_ms": 200,
            "event": {"type": "input_transcript", "delta": "how is my portfolio", "final": False},
        },
        {"at_input_ms": 1000, "event": {"type": "close", "reason": "expired"}},
        # The new provider session's input timeline starts at 0 again.
        {
            "at_input_ms": 300,
            "event": {"type": "input_transcript", "delta": "what's the weather", "final": False},
        },
        {"at_input_ms": 1000, "event": {"type": "delegation", "id": "del_2", "offset_ms": 1000}},
    ]
    harness.conversation.feed(1200, _voiced("rob"))  # 60 ticks of Rob before the reconnect
    harness.start(FakeProvider(script, capabilities=LIVE))
    await harness.wait_for(lambda: len(harness.sent("connect")) == 2)

    # 40 ticks of Alex, ending 100 ms before the delegation's offset, then silence past the grace.
    # Were the old ticks kept, Rob would be credited; were the timeline not restarted, Alex's ticks
    # would fall after the credit window; either way the old words must not join the request.
    harness.conversation.feed(3000, _voiced("alex", 600, 1400))
    await harness.wait_for(lambda: bool(harness.sent("deliver_result")))

    assert harness.router.requests == [("what's the weather", "alex")]
    [delivered] = harness.sent("deliver_result")
    assert delivered.args["ref"]["delegation_id"] == "del_2", "the new session's own delegation is answered"


async def test_live_delegation_cut_off_by_the_reconnect_is_asked_again(harness: _Harness) -> None:
    script = [
        {"at_input_ms": 200, "event": {"type": "input_transcript", "delta": "what's the", "final": False}},
        {"at_input_ms": 900, "event": {"type": "delegation", "id": "del_1", "offset_ms": 900}},
        # Closed before the input reaches the offset + 500 ms needed to credit the speaker.
        {"at_input_ms": 1000, "event": {"type": "close", "reason": "connection_lost"}},
    ]
    harness.conversation.feed(1200, _voiced("rob"))
    harness.start(FakeProvider(script, capabilities=LIVE))

    await harness.wait_for(lambda: bool(harness.sent("say")))

    assert [call.method for call in harness.calls_after_reconnect()] == ["say"]
    assert [call.args["text"] for call in harness.sent("say")] == [NOT_HEARD]
    assert harness.router.requests == []


async def test_three_failed_reconnects_end_the_conversation_with_a_notice(harness: _Harness) -> None:
    provider = _UnreachableAfterFirstConnect(
        [{"at_input_ms": 200, "event": {"type": "close", "reason": "expired"}}]
    )
    harness.conversation.feed(400)
    harness.start(provider)

    await harness.wait_for(lambda: harness.conversation.closed_reason is not None)

    assert harness.conversation.closed_reason == PROVIDER_LOST_REASON
    assert provider.connect_attempts == 4, "the first connect, then three reconnects"
    assert harness.reconnect_waits_s == [0.5, 1.0, 2.0]
    assert harness.conversation.notified[-1] == {
        "transcript": {"speaker": "assistant", "text": PROVIDER_LOST}
    }


async def test_sessions_that_close_at_once_end_the_conversation_after_three_in_a_row(
    harness: _Harness,
) -> None:
    # The provider accepts every connection and closes it at once; the script never runs out.
    script = [{"after_ms": 0, "event": {"type": "close", "reason": "server_error"}}] * 10
    harness.start(FakeProvider(script))

    await harness.wait_for(lambda: harness.conversation.closed_reason is not None)

    assert harness.conversation.closed_reason == PROVIDER_LOST_REASON
    assert len(harness.sent("connect")) == 3, "the first session, then two reconnects"
    assert harness.conversation.notified[-1] == {
        "transcript": {"speaker": "assistant", "text": PROVIDER_LOST}
    }


async def test_a_session_that_lasts_resets_the_count_of_short_ones(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "SHORT_PROVIDER_SESSION_S", 0.2)
    short = {"after_ms": 0, "event": {"type": "close", "reason": "server_error"}}
    lasting = {"after_ms": 300, "event": {"type": "close", "reason": "expired"}}
    harness.start(FakeProvider([short, short, lasting, short, short]))

    # Short, short, lasting, short, short: never three short ones in a row, so the sixth session stays.
    await harness.wait_for(lambda: len(harness.sent("connect")) == 6)
    await asyncio.sleep(0.1)

    assert harness.conversation.closed_reason is None


async def test_a_reconnect_after_a_context_change_renders_the_new_profile(harness: _Harness) -> None:
    # The conversation starts on stock-picker; hearing alex moves it to general, then the socket drops.
    script = [{"on_sent": "update_instructions", "event": {"type": "close", "reason": "connection_lost"}}]
    harness.conversation.feed(400, _voiced("alex"))
    harness.start(FakeProvider(script))

    await harness.wait_for(lambda: len(harness.sent("connect")) == 2)

    general = GptLivePrompter().instructions(prompt_context(harness.config, "general"))
    first, second = harness.sent("connect")
    assert first.args["instructions"] == GptLivePrompter().instructions(
        prompt_context(harness.config, "stock-picker")
    )
    assert [call.args["text"] for call in harness.sent("update_instructions")] == [general]
    assert second.args["instructions"] == general


async def test_restarts_for_new_instructions_never_end_the_conversation_as_short_sessions(
    harness: _Harness,
) -> None:
    # Each newly heard person moves the instructions to another profile, and the provider cannot
    # replace them in place: three quick restarts in a row, which three short drops would not survive.
    harness.start(FakeProvider(capabilities={"replaces_instructions": False}))
    for connects, person in enumerate(("alex", "rob", "carol"), start=2):
        harness.conversation.feed(100, _voiced(person))
        await harness.wait_for(lambda connects=connects: len(harness.sent("connect")) == connects)

    await asyncio.sleep(0.1)
    assert harness.conversation.closed_reason is None
    profiles = ["stock-picker", "general", "stock-picker", "general"]
    assert [call.args["instructions"] for call in harness.sent("connect")] == [
        GptLivePrompter().instructions(prompt_context(harness.config, profile)) for profile in profiles
    ]
