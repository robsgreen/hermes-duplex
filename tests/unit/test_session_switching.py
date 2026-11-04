"""VoiceSession switching: switch requests in hand-off words and ``switch_profile`` calls, switches made
outside the conversation, and the fallback notice, with the fake provider and the fake Hermes API."""

import asyncio
import json
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
from hermes_duplex.prompts.notices import (
    already_active,
    fallback_to_default,
    switch_refused,
    switched_to,
    which_profile,
)
from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.providers.fake.adapter import FakeProvider, SentCall
from hermes_duplex.router.base import Effort, RouteDecision
from hermes_duplex.session import VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.base import InputFrame
from tests.fakes.fake_hermes_api import FakeHermesApi

RATE = 24000
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
EVERY_PROFILE = ["general", "stock-picker"]


class _MemoryConversation:
    """A ``Conversation`` that yields scripted frames, then waits until it is closed."""

    id = "memory-1"

    def __init__(self, frames: Sequence[InputFrame], person: str | None) -> None:
        self.person = person
        self._frames = frames
        self._closed = asyncio.Event()

    async def frames(self) -> AsyncIterator[InputFrame]:
        for frame in self._frames:
            yield frame
        await self._closed.wait()

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        pass

    async def flush_playback(self) -> None:
        pass

    async def notify(self, event: dict[str, Any]) -> None:
        pass

    async def close(self, reason: str) -> None:
        self._closed.set()


class _RecordingRouter:
    """A router that must not be asked: it records each request and routes it as a medium-effort task."""

    def __init__(self) -> None:
        self.requests: list[str] = []

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
        self.requests.append(text)
        return RouteDecision(
            effort="medium",
            model=None,
            suggestion=None,
            control_intent=None,
            probabilities={},
            source="knn",
            latency_ms=1,
        )


def _frames(duration_ms: int, *people: str) -> list[InputFrame]:
    """20 ms silent frames covering ``duration_ms``, each voiced by ``people``."""
    return [
        InputFrame(silent_frame(RATE), RATE, frozenset(people), t_ms) for t_ms in range(0, duration_ms, 20)
    ]


def _handoff(call_id: str, task: str) -> dict[str, Any]:
    return {"at_input_ms": 500, "event": {"type": "delegation", "id": call_id, "tool_args": {"task": task}}}


def _switch_call(call_id: str, profile: object) -> dict[str, Any]:
    return {
        "at_input_ms": 500,
        "event": {
            "type": "delegation",
            "id": call_id,
            "target": "switch_profile",
            "tool_args": {"profile": profile},
        },
    }


def _config(tmp_path: Path) -> Config:
    """Rob may use every profile and starts on general, the router default; Alex may use only general."""
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather"]},
            "stock-picker": {
                "description": "Portfolio, holdings, stock research, trades",
                "examples": ["how's my portfolio"],
                "aliases": ["the stock picker", "stocks"],
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
    """A session on the fake provider, a router that records requests, and the fake Hermes API."""

    def __init__(self, tmp_path: Path, api: FakeHermesApi) -> None:
        self.api = api
        self.config = _config(tmp_path)
        self.permissions = PermissionChecker(self.config)
        self.store_path = tmp_path / "home" / "duplex" / "active_profiles.json"
        self.active_profiles = ActiveProfiles(self.store_path, self.permissions, self.config)
        self.router = _RecordingRouter()
        self.tracker = TaskTracker(tmp_path / "home")
        self.provider: FakeProvider | None = None
        self._conversation: _MemoryConversation | None = None
        self._hermes: HermesApiClient | None = None
        self._run: asyncio.Task[None] | None = None

    def start(
        self, script: Iterable[Mapping[str, Any]], frames: Sequence[InputFrame], *, person: str | None = None
    ) -> None:
        self.provider = FakeProvider(script)
        self._conversation = _MemoryConversation(frames, person)
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
            profile_aliases={name: profile.aliases for name, profile in self.config.profiles.items()},
        )
        self._run = asyncio.create_task(session.run())

    async def stop(self) -> None:
        if self._conversation is not None and self._run is not None:
            await self._conversation.close("test over")
            await asyncio.wait_for(self._run, timeout=5)
            self._run = None
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

    def instructions_for(self, profile: str) -> str:
        assert self.provider is not None
        return self.provider.prompter.instructions(service.prompt_context(self.config, profile))

    async def wait_for(self, condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                if self._run is not None and self._run.done():
                    self._run.result()
                    raise AssertionError("the session ended before the condition held")
                await asyncio.sleep(0.01)

    def assert_nothing_ran(self) -> None:
        """No switch request reaches the router, and none becomes a task or a Hermes run."""
        assert self.router.requests == []
        assert self.tracker.active_for("rob") == [] and self.tracker.active_for("alex") == []
        assert self.api.requests_to("POST", "/runs") == []


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


@pytest.fixture
async def harness(tmp_path: Path, api: FakeHermesApi) -> AsyncIterator[_Harness]:
    harness = _Harness(tmp_path, api)
    yield harness
    await harness.stop()


@pytest.mark.parametrize(
    ("before", "words", "after"),
    [
        ("general", "switch to stock-picker", "stock-picker"),
        ("general", "let me talk to the stock picker", "stock-picker"),
        ("stock-picker", "go back to general", "general"),
    ],
)
async def test_a_switch_request_switches_and_is_confirmed_without_routing_or_a_task(
    harness: _Harness, before: str, words: str, after: str
) -> None:
    await harness.active_profiles.switch("rob", before)
    harness.start([_handoff("call_1", words)], _frames(1000, "rob"), person="rob")

    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)

    assert harness.active_profiles.get("rob") == after
    assert harness.outputs() == {"call_1": {"status": "received"}}, "the receipt still goes out first"
    assert harness.said() == [switched_to(after)]
    assert [call.args["text"] for call in harness.sent("update_instructions")] == [
        harness.instructions_for(after)
    ]
    harness.assert_nothing_ran()


async def test_a_switch_to_a_name_that_fits_no_profile_asks_which_and_changes_nothing(
    harness: _Harness,
) -> None:
    harness.start([_handoff("call_1", "switch to finance")], _frames(1000, "rob"))

    await harness.wait_for(lambda: bool(harness.said()))

    assert harness.said() == [which_profile(EVERY_PROFILE)]
    assert harness.active_profiles.get("rob") == "general"
    assert harness.sent("update_instructions") == []
    harness.assert_nothing_ran()


async def test_a_switch_to_a_profile_the_speaker_may_not_use_is_refused(harness: _Harness) -> None:
    harness.start([_handoff("call_1", "switch to stock-picker")], _frames(1000, "alex"))

    await harness.wait_for(lambda: bool(harness.said()))

    assert harness.said() == [switch_refused("stock-picker")]
    assert harness.active_profiles.get("alex") == "general"
    assert harness.sent("update_instructions") == []
    harness.assert_nothing_ran()


async def test_a_switch_to_the_active_profile_says_it_already_is(harness: _Harness) -> None:
    harness.start([_handoff("call_1", "talk to general please")], _frames(1000, "rob"))

    await harness.wait_for(lambda: bool(harness.said()))

    assert harness.said() == [already_active("general")]
    harness.assert_nothing_ran()


async def test_a_switch_followed_by_a_request_switches_then_runs_the_request_there(
    harness: _Harness,
) -> None:
    harness.start([_handoff("call_1", "switch to stock-picker and check NVDA")], _frames(1000, "rob"))

    await harness.wait_for(lambda: bool(harness.api.requests_to("POST", "/runs")))

    (run,) = harness.api.requests_to("POST", "/runs")
    assert run.path == "/p/stock-picker/v1/runs"
    assert run.body["input"] == "check NVDA"
    assert harness.router.requests == ["check NVDA"], "only the request is routed, not the switch"
    assert harness.active_profiles.get("rob") == "stock-picker"
    await harness.wait_for(lambda: len(harness.said()) == 2)
    assert harness.said()[0] == switched_to("stock-picker")


@pytest.mark.parametrize(
    ("speaker", "words", "notice"),
    [
        ("alex", "switch to stock-picker and check NVDA", switch_refused("stock-picker")),
        ("rob", "switch to finance and check NVDA", which_profile(EVERY_PROFILE)),
    ],
)
async def test_a_request_after_a_switch_that_does_not_happen_is_dropped(
    harness: _Harness, speaker: str, words: str, notice: str
) -> None:
    harness.start([_handoff("call_1", words)], _frames(1000, speaker))

    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)

    assert harness.said() == [notice]
    harness.assert_nothing_ran()


@pytest.mark.parametrize(
    "words",
    ["switch to stock-picker please", "let me talk to the stock picker please", "switch to stocks, thanks"],
)
async def test_polite_words_after_a_switch_are_no_request(harness: _Harness, words: str) -> None:
    harness.start([_handoff("call_1", words)], _frames(1000, "rob"))

    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.1)

    assert harness.said() == [switched_to("stock-picker")]
    harness.assert_nothing_ran()


async def test_switch_profile_call_switches_and_answers_with_the_outcome(harness: _Harness) -> None:
    harness.start([_switch_call("call_1", "stock-picker")], _frames(1000, "rob"))

    await harness.wait_for(lambda: bool(harness.said()))

    assert harness.outputs() == {"call_1": {"status": "switched"}}
    assert harness.said() == [switched_to("stock-picker")], "the confirmation is its own message"
    assert harness.active_profiles.get("rob") == "stock-picker"
    assert [call.args["text"] for call in harness.sent("update_instructions")] == [
        harness.instructions_for("stock-picker")
    ]
    harness.assert_nothing_ran()


@pytest.mark.parametrize(
    ("speaker", "profile", "outcome", "message"),
    [
        ("alex", "stock-picker", "refused", switch_refused("stock-picker")),
        ("rob", "general", "already_active", already_active("general")),
        ("rob", "stocks", "switched", None),  # an alias resolves as a spoken one would
        ("rob", "finance", "unknown", which_profile(EVERY_PROFILE)),
        ("rob", 7, "unknown", which_profile(EVERY_PROFILE)),
    ],
)
async def test_switch_profile_call_answers_each_outcome_in_its_output(
    harness: _Harness, speaker: str, profile: object, outcome: str, message: str | None
) -> None:
    harness.start([_switch_call("call_1", profile)], _frames(1000, speaker))

    await harness.wait_for(lambda: "call_1" in harness.outputs())
    await asyncio.sleep(0.1)

    expected = {"status": outcome} if message is None else {"status": outcome, "message": message}
    assert harness.outputs()["call_1"] == expected
    assert harness.said() == ([switched_to("stock-picker")] if outcome == "switched" else [])
    harness.assert_nothing_ran()


async def test_a_switch_made_elsewhere_updates_the_instructions_and_is_confirmed(harness: _Harness) -> None:
    harness.start([], _frames(200, "rob"))
    await harness.wait_for(lambda: len(harness.sent("connect")) == 1)
    await asyncio.sleep(0.1)  # Rob has been heard

    # As the Discord command or the web picker does.
    result = await harness.active_profiles.switch("rob", "stock-picker")
    await harness.wait_for(lambda: bool(harness.said()))

    assert result.notified_live
    assert harness.said() == [switched_to("stock-picker")]
    assert [call.args["text"] for call in harness.sent("update_instructions")] == [
        harness.instructions_for("stock-picker")
    ]
    # Nobody listens for Alex, who has not been heard here.
    assert not (await harness.active_profiles.switch("alex", "general")).notified_live


async def test_the_signed_in_person_is_listened_for_before_they_are_heard(harness: _Harness) -> None:
    harness.start([], [], person="rob")
    await harness.wait_for(lambda: len(harness.sent("connect")) == 1)

    result = await harness.active_profiles.switch("rob", "stock-picker")
    await harness.wait_for(lambda: bool(harness.said()))

    assert result.notified_live
    assert harness.said() == [switched_to("stock-picker")]


async def test_a_session_that_ended_no_longer_listens(harness: _Harness) -> None:
    harness.start([], _frames(200, "rob"), person="rob")
    await harness.wait_for(lambda: len(harness.sent("connect")) == 1)
    await harness.stop()

    assert not (await harness.active_profiles.switch("rob", "stock-picker")).notified_live


async def test_the_fallback_to_the_default_is_spoken_once_when_the_person_is_first_heard(
    harness: _Harness,
) -> None:
    harness.store_path.parent.mkdir(parents=True)
    harness.store_path.write_text(json.dumps({"alex": "stock-picker"}), encoding="utf-8")
    harness.active_profiles.load()  # Alex may not use stock-picker (any more)

    harness.start([], [*_frames(500, "alex"), *_frames(500), *_frames(500, "alex")])
    await harness.wait_for(lambda: bool(harness.said()))
    await asyncio.sleep(0.2)

    assert harness.said() == [fallback_to_default("general")]
    assert harness.active_profiles.fallback_pending("alex") is None
