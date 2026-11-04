"""The fake provider: script triggers and events, recording, capabilities, and loading from config."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from hermes_duplex.config import ConfigError, ProviderConfig, Secrets
from hermes_duplex.prompts.base import PromptContext
from hermes_duplex.providers.base import (
    Closed,
    DelegationRef,
    DelegationRequested,
    InputTranscript,
    OutputAudio,
    OutputDone,
    OutputTranscript,
    ProviderError,
    ProviderEvent,
    RealtimeProvider,
    ResponseStarted,
    SessionReady,
    SpeechStarted,
    SpeechStopped,
)
from hermes_duplex.providers.fake.adapter import (
    DEFAULT_CAPABILITIES,
    FakeProvider,
    ScriptError,
    SentCall,
    load_script,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "fixtures" / "provider_scripts"
RATE = 24000


def _audio_ms(ms: int, rate: int = RATE) -> bytes:
    return bytes(2 * rate * ms // 1000)


class EventLog:
    """Collects a provider's events in the background, across reconnects."""

    def __init__(self, provider: FakeProvider) -> None:
        self.events: list[ProviderEvent] = []
        self._task = asyncio.create_task(self._collect(provider))

    async def _collect(self, provider: FakeProvider) -> None:
        while True:
            async for event in provider.events():
                self.events.append(event)

    def stop(self) -> None:
        self._task.cancel()

    def after_ready(self) -> list[ProviderEvent]:
        """Events other than ``SessionReady``."""
        return [event for event in self.events if not isinstance(event, SessionReady)]


@pytest.fixture
async def make_provider() -> AsyncIterator[Callable[..., tuple[FakeProvider, EventLog]]]:
    logs: list[EventLog] = []

    def make(script: list[dict[str, Any]] | None = None, **kwargs: Any) -> tuple[FakeProvider, EventLog]:
        provider = FakeProvider(script or [], **kwargs)
        log = EventLog(provider)
        logs.append(log)
        return provider, log

    yield make
    for log in logs:
        log.stop()


async def _settle() -> None:
    """Give the provider's script player and the event log time to run."""
    await asyncio.sleep(0.01)


async def _wait_until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.002)


async def _connect(provider: FakeProvider, history_summary: str | None = None) -> None:
    await provider.connect("be helpful", "marin", history_summary)


def test_fake_provider_satisfies_the_provider_interface() -> None:
    provider: RealtimeProvider = FakeProvider()
    assert provider.name == "fake"
    assert provider.capabilities == DEFAULT_CAPABILITIES


async def test_connect_emits_session_ready(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider()
    await _connect(provider)
    await _settle()

    assert log.events == [SessionReady()]


async def test_at_input_ms_fires_once_that_much_input_has_arrived(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"at_input_ms": 100, "event": {"type": "speech_started"}},
            {"at_input_ms": 160, "event": {"type": "speech_stopped"}},
        ]
    )
    await _connect(provider)

    await provider.send_audio(_audio_ms(80))
    await _settle()
    assert log.after_ready() == []

    await provider.send_audio(_audio_ms(20))
    await _settle()
    assert log.after_ready() == [SpeechStarted()]

    await provider.send_audio(_audio_ms(40))
    await _settle()
    assert log.after_ready() == [SpeechStarted()]
    assert provider.input_ms == 140

    await provider.send_audio(_audio_ms(20))
    await _settle()
    assert log.after_ready() == [SpeechStarted(), SpeechStopped()]


async def test_steps_fire_in_order_even_when_a_later_trigger_is_met_first(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"at_input_ms": 500, "event": {"type": "speech_started"}},
            {"at_input_ms": 100, "event": {"type": "speech_stopped"}},
        ]
    )
    await _connect(provider)

    await provider.send_audio(_audio_ms(200))
    await _settle()
    assert log.after_ready() == []

    await provider.send_audio(_audio_ms(300))
    await _settle()
    assert log.after_ready() == [SpeechStarted(), SpeechStopped()]


async def test_after_ms_waits_from_connect_then_from_the_previous_step(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"after_ms": 50, "event": {"type": "output_done"}},
            {"after_ms": 50, "event": {"type": "speech_started"}},
        ]
    )
    loop = asyncio.get_running_loop()
    fired_at_s: list[float] = []

    start_s = loop.time()
    await _connect(provider)
    await _wait_until(lambda: len(log.after_ready()) >= 1)
    fired_at_s.append(loop.time() - start_s)
    await _wait_until(lambda: len(log.after_ready()) >= 2)
    fired_at_s.append(loop.time() - start_s)

    assert log.after_ready() == [OutputDone(), SpeechStarted()]
    assert fired_at_s[0] >= 0.05
    assert fired_at_s[1] >= 0.1


QUIET = {"emits_speech_events": False}
"""Capabilities under which the fake answers no call with a response of its own."""


async def test_calls_that_make_a_model_respond_get_an_empty_response_with_speech_events(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider([{"on_sent": "say", "event": {"type": "output_audio", "ms": 100}}])
    await _connect(provider)

    await provider.send_function_output("call_1", {"status": "started"})
    await provider.report_progress(DelegationRef(delegation_id=None, call_id="call_1", task_id=1), "busy")
    await provider.say("one moment")
    await _settle()

    assert log.after_ready() == [
        ResponseStarted(),
        OutputDone(),
        ResponseStarted(),
        OutputDone(),
        OutputAudio(pcm=_audio_ms(100), item_id=None),
    ]


async def test_on_sent_fires_after_a_matching_call(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [{"on_sent": "deliver_result", "event": {"type": "output_audio", "ms": 100}}],
        capabilities=QUIET,
    )
    await _connect(provider)

    await provider.say("one moment")
    await _settle()
    assert log.after_ready() == []

    await provider.deliver_result(DelegationRef(delegation_id=None, call_id="c1", task_id=1), "done")
    await _settle()
    assert log.after_ready() == [OutputAudio(pcm=_audio_ms(100), item_id=None)]


async def test_on_sent_counts_only_calls_after_the_previous_step_fired(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"at_input_ms": 100, "event": {"type": "speech_started"}},
            {"on_sent": "say", "event": {"type": "output_done"}},
        ],
        capabilities=QUIET,
    )
    await _connect(provider)

    await provider.say("before the first step")
    await provider.send_audio(_audio_ms(100))
    await _settle()
    assert log.after_ready() == [SpeechStarted()]

    await provider.say("after the first step")
    await _settle()
    assert log.after_ready() == [SpeechStarted(), OutputDone()]


async def test_scripted_close_ends_the_connection_and_the_script_resumes_on_reconnect(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"at_input_ms": 100, "event": {"type": "close", "reason": "expired"}},
            {"at_input_ms": 50, "event": {"type": "speech_started"}},
        ]
    )
    await _connect(provider)
    await provider.send_audio(_audio_ms(100))
    await _settle()
    assert log.events == [SessionReady(), Closed("expired")]

    await _connect(provider, history_summary="we talked about stocks")
    assert provider.input_ms == 0
    await provider.send_audio(_audio_ms(50))
    await _settle()

    assert log.events == [SessionReady(), Closed("expired"), SessionReady(), SpeechStarted()]
    connects = [call.args["history_summary"] for call in provider.calls if call.method == "connect"]
    assert connects == [None, "we talked about stocks"]


async def test_close_emits_closed_once_and_is_safe_to_repeat(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider([{"after_ms": 20, "event": {"type": "speech_started"}}])
    await _connect(provider)

    await provider.close()
    await provider.close()
    await asyncio.sleep(0.05)

    assert log.events == [SessionReady(), Closed("closed by client")]
    assert [call.method for call in provider.calls] == ["connect", "close", "close"]


async def test_records_every_call_in_memory_and_to_record_path(tmp_path: Path) -> None:
    record_path = tmp_path / "sent.jsonl"
    provider = FakeProvider(record_path=record_path)
    ref = DelegationRef(delegation_id="d1", call_id=None, task_id=3)

    await provider.connect("be helpful", None, None)
    await provider.send_audio(_audio_ms(20))
    await provider.truncate("item-1", 420)
    await provider.send_function_output("call_1", {"status": "started"})
    await provider.report_progress(ref, "looking it up")
    await provider.deliver_result(ref, "up 2%")
    await provider.say("task 3 is done")
    await provider.update_instructions("be brief")
    await provider.close()

    ref_args = {"delegation_id": "d1", "call_id": None, "task_id": 3}
    expected = [
        SentCall("connect", {"instructions": "be helpful", "voice": None, "history_summary": None}),
        SentCall("truncate", {"item_id": "item-1", "audio_end_ms": 420}),
        SentCall("send_function_output", {"call_id": "call_1", "payload": {"status": "started"}}),
        SentCall("report_progress", {"ref": ref_args, "text": "looking it up"}),
        SentCall("deliver_result", {"ref": ref_args, "text": "up 2%"}),
        SentCall("say", {"text": "task 3 is done"}),
        SentCall("update_instructions", {"text": "be brief"}),
        SentCall("close", {}),
    ]
    assert list(provider.calls) == expected
    lines = record_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"method": c.method, "args": c.args} for c in expected]


async def test_record_path_is_appended_to(tmp_path: Path) -> None:
    record_path = tmp_path / "sent.jsonl"
    record_path.write_text('{"method": "earlier", "args": {}}\n', encoding="utf-8")

    await FakeProvider(record_path=record_path).say("hello")

    assert len(record_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "speech_started"}, SpeechStarted()),
        ({"type": "speech_stopped"}, SpeechStopped()),
        ({"type": "response_started"}, ResponseStarted()),
        ({"type": "output_done"}, OutputDone()),
        ({"type": "output_audio", "ms": 20, "item_id": "i1"}, OutputAudio(pcm=_audio_ms(20), item_id="i1")),
        ({"type": "input_transcript", "delta": "hi"}, InputTranscript(delta="hi", final=True)),
        (
            {"type": "input_transcript", "delta": "hi", "final": False},
            InputTranscript(delta="hi", final=False),
        ),
        ({"type": "output_transcript", "delta": "Sure."}, OutputTranscript(delta="Sure.")),
        (
            {"type": "delegation", "id": "d1", "tool_args": {"task": "t"}},
            DelegationRequested(id="d1", offset_ms=None, target="delegate", tool_args={"task": "t"}),
        ),
        (
            {"type": "delegation", "id": "d1", "target": "client", "offset_ms": 900},
            DelegationRequested(id="d1", offset_ms=900, target="client", tool_args=None),
        ),
        ({"type": "error", "message": "boom"}, ProviderError(message="boom")),
        ({"type": "error", "message": "boom", "code": "x"}, ProviderError(message="boom", code="x")),
        ({"type": "close"}, Closed("script")),
    ],
)
async def test_each_event_type_maps_to_its_provider_event(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
    event: dict[str, Any],
    expected: ProviderEvent,
) -> None:
    provider, log = make_provider([{"after_ms": 0, "event": event}])
    await _connect(provider)
    await _settle()

    assert log.after_ready() == [expected]


async def test_capability_overrides_replace_only_the_keys_given(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(
        [
            {"after_ms": 0, "event": {"type": "delegation", "id": "d1"}},
            {"after_ms": 0, "event": {"type": "output_audio", "ms": 20}},
        ],
        capabilities={"delegation_style": "live", "emits_speech_events": False, "output_rate": 16000},
    )

    assert provider.capabilities == {
        **DEFAULT_CAPABILITIES,
        "delegation_style": "live",
        "emits_speech_events": False,
        "output_rate": 16000,
    }
    await _connect(provider)
    await _settle()
    assert log.after_ready() == [
        DelegationRequested(id="d1", offset_ms=None, target="client", tool_args=None),
        OutputAudio(pcm=_audio_ms(20, rate=16000), item_id=None),
    ]


async def test_input_ms_uses_the_input_rate() -> None:
    provider = FakeProvider(capabilities={"input_rate": 16000})
    await provider.send_audio(_audio_ms(100, rate=16000))
    assert provider.input_ms == 100


def test_replaces_instructions_is_on_by_default_and_can_be_turned_off() -> None:
    assert DEFAULT_CAPABILITIES["replaces_instructions"] is True
    assert (
        FakeProvider(capabilities={"replaces_instructions": False}).capabilities["replaces_instructions"]
        is False
    )


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="barge_in"):
        FakeProvider(capabilities={"barge_in": True})


@pytest.mark.parametrize(
    "step",
    [
        {"event": {"type": "speech_started"}},
        {"at_input_ms": 1, "after_ms": 1, "event": {"type": "speech_started"}},
        {"at_input_ms": -1, "event": {"type": "speech_started"}},
        {"after_ms": "10", "event": {"type": "speech_started"}},
        {"on_sent": "send_audio", "event": {"type": "speech_started"}},
        {"after_ms": 0, "when": "now", "event": {"type": "speech_started"}},
        {"after_ms": 0},
        {"after_ms": 0, "event": {"type": "laugh"}},
        {"after_ms": 0, "event": {"type": "delegation"}},
        {"after_ms": 0, "event": {"type": "delegation", "id": "d1", "tool_arg": {}}},
    ],
)
def test_malformed_step_is_rejected_naming_the_step(step: dict[str, Any]) -> None:
    with pytest.raises(ScriptError, match="^step 2: "):
        FakeProvider([{"after_ms": 0, "event": {"type": "output_done"}}, step])


def test_load_script_reads_steps_and_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text('{"after_ms": 0, "event": {"type": "output_done"}}\n\n', encoding="utf-8")

    assert load_script(path) == [{"after_ms": 0, "event": {"type": "output_done"}}]


@pytest.mark.parametrize("line", ["{not json", "[1, 2]"])
def test_load_script_rejects_a_bad_line_naming_it(tmp_path: Path, line: str) -> None:
    path = tmp_path / "script.jsonl"
    path.write_text(f'{{"after_ms": 0, "event": {{"type": "output_done"}}}}\n{line}\n', encoding="utf-8")

    with pytest.raises(ScriptError, match="^line 2: "):
        load_script(path)


def _fake_config(**fields: Any) -> ProviderConfig:
    return ProviderConfig(
        name="fake", model=None, voice=None, api_key_env=None, turn_detection=None, **fields
    )


async def test_from_config_uses_script_record_path_and_capabilities(tmp_path: Path) -> None:
    config = _fake_config(
        script=SCRIPTS / "delegate_portfolio.jsonl",
        record_path=tmp_path / "sent.jsonl",
        capabilities={"delegation_style": "live"},
    )

    provider = FakeProvider.from_config(config, Secrets({}), PromptContext())
    await provider.say("hello")

    assert provider.capabilities["delegation_style"] == "live"
    assert (tmp_path / "sent.jsonl").exists()


@pytest.mark.parametrize("content", [None, '{"after_ms": 0, "event": {"type": "laugh"}}\n'])
def test_from_config_reports_an_unusable_script_on_the_script_key(
    tmp_path: Path, content: str | None
) -> None:
    script = tmp_path / "script.jsonl"
    if content is not None:
        script.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        FakeProvider.from_config(_fake_config(script=script), Secrets({}), PromptContext())

    assert caught.value.key == "provider.script"


async def test_delegate_portfolio_script_delegates_twice_as_input_arrives(
    make_provider: Callable[..., tuple[FakeProvider, EventLog]],
) -> None:
    provider, log = make_provider(load_script(SCRIPTS / "delegate_portfolio.jsonl"))
    await _connect(provider)

    delegations_at_ms: list[tuple[int, DelegationRequested]] = []
    while provider.input_ms < 6000:
        await provider.send_audio(_audio_ms(20))
        await _settle()
        for event in log.after_ready():
            if isinstance(event, DelegationRequested) and event not in [d for _, d in delegations_at_ms]:
                delegations_at_ms.append((provider.input_ms, event))

    assert [(ms, d.tool_args) for ms, d in delegations_at_ms] == [
        (1500, {"task": "how is my portfolio doing"}),
        (4500, {"task": "and what about Tesla"}),
    ]
