"""RealtimeApiProvider against recorded Realtime sessions replayed by the fake WebSocket server."""

import asyncio
import base64
import dataclasses
import json
import socket
import time
from collections.abc import AsyncIterator
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
    ResponseStarted,
    SessionReady,
    SpeechStarted,
    SpeechStopped,
)
from hermes_duplex.providers.realtime.adapter import Dialect, RealtimeApiProvider, session_tools
from hermes_duplex.providers.realtime.openai.dialect import OPENAI
from hermes_duplex.providers.realtime.openai.prompt import OpenAiRealtimePrompter
from hermes_duplex.providers.realtime.xai.dialect import XAI
from hermes_duplex.providers.realtime.xai.prompt import GrokPrompter
from hermes_duplex.providers.wire import WireError
from tests.fakes.fake_ws_server import FakeWsServer

SCRIPTS = Path(__file__).parent.parent / "fixtures" / "provider_scripts"
API_KEY = "sk-test-0123456789abcdef"
INSTRUCTIONS = OpenAiRealtimePrompter().instructions(PromptContext())
CONTEXT = PromptContext(profiles=(("stock-picker", "Portfolio, holdings, trades"), ("general", "")))
REF = DelegationRef(delegation_id=None, call_id="call_1", task_id=1)
RECEIPT_BUDGET_MS = 50

# A session that is configured and then only records. The fake server cannot shut down while a
# ``wait_for`` step is pending, so tests that stop early use this instead of a longer recorded session.
STARTED = [
    {"type": "session.created", "event_id": "event_1"},
    {"wait_for": "session.update"},
    {"type": "session.updated", "event_id": "event_2"},
]

WITHOUT_OUT_OF_BAND = dataclasses.replace(
    OPENAI, capabilities={**OPENAI.capabilities, "supports_out_of_band_response": False}
)
WITHOUT_TRUNCATE = dataclasses.replace(
    OPENAI, capabilities={**OPENAI.capabilities, "supports_truncate": False}
)


def _provider(server: FakeWsServer, **options: Any) -> RealtimeApiProvider:
    options.setdefault("dialect", OPENAI)
    return RealtimeApiProvider(API_KEY, url=f"{server.url}/v1/realtime", **options)


async def _next(events: AsyncIterator[ProviderEvent], count: int) -> list[ProviderEvent]:
    """The next ``count`` events, failing the test instead of hanging when they do not come."""
    async with asyncio.timeout(5):
        return [await anext(events) for _ in range(count)]


async def _rest(events: AsyncIterator[ProviderEvent]) -> list[ProviderEvent]:
    async with asyncio.timeout(5):
        return [event async for event in events]


def test_openai_capabilities_describe_tool_delegation_with_speech_events() -> None:
    assert RealtimeApiProvider(API_KEY, dialect=OPENAI).capabilities == {
        "input_rate": 24000,
        "output_rate": 24000,
        "supports_truncate": True,
        "supports_out_of_band_response": True,
        "emits_speech_events": True,
        "delegation_style": "tool",
        "max_session_s": 3600,
        "replaces_instructions": True,
    }
    assert OPENAI.url == "wss://api.openai.com/v1/realtime"


async def test_connect_sends_session_update_with_pcm_turn_detection_and_tools() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, turn_detection={"type": "semantic_vad", "eagerness": "auto"})
        await provider.connect(INSTRUCTIONS, "cedar", None)
        await provider.close()

    (connection,) = server.connections
    assert connection.path == "/v1/realtime?model=gpt-realtime-2.1"
    assert connection.headers["authorization"] == f"Bearer {API_KEY}"
    (update,) = server.client_events
    print("session.update:", json.dumps(update, indent=2))
    session = update["session"]
    pcm = {"type": "audio/pcm", "rate": 24000}
    assert update["type"] == "session.update"
    assert session["type"] == "realtime"
    assert session["instructions"] == INSTRUCTIONS
    assert session["output_modalities"] == ["audio"]
    assert session["audio"] == {
        "input": {
            "format": pcm,
            "turn_detection": {"type": "semantic_vad", "eagerness": "auto", "interrupt_response": True},
            "transcription": {"model": "gpt-4o-transcribe"},
        },
        "output": {"format": pcm, "voice": "cedar"},
    }
    assert session["tool_choice"] == "auto"
    assert session["tools"] == list(session_tools(PromptContext()))


def test_session_tools_take_a_task_a_task_choice_a_profile_and_a_yes_or_no() -> None:
    tools = {tool["name"]: tool for tool in session_tools(CONTEXT)}

    assert list(tools) == ["delegate", "task_status", "cancel_task", "switch_profile", "confirm_switch"]
    assert all(tool["type"] == "function" for tool in tools.values())
    delegate = tools["delegate"]["parameters"]
    assert delegate["properties"] == {
        "task": {"type": "string", "description": "The request restated as one self-contained task."}
    }
    assert delegate["required"] == ["task"]
    for name in ("task_status", "cancel_task"):
        assert tools[name]["parameters"]["properties"]["task_id"]["type"] == "integer"
        assert tools[name]["parameters"]["properties"]["about"]["type"] == "string"
        assert tools[name]["parameters"]["required"] == []
    switch = tools["switch_profile"]["parameters"]
    assert list(switch["properties"]) == ["profile"]
    assert switch["properties"]["profile"]["type"] == "string"
    assert switch["properties"]["profile"]["enum"] == ["stock-picker", "general"]
    assert "alias" in switch["properties"]["profile"]["description"]
    assert switch["required"] == ["profile"]
    confirm = tools["confirm_switch"]
    assert "question about switching" in confirm["description"]
    assert list(confirm["parameters"]["properties"]) == ["accept"]
    assert confirm["parameters"]["properties"]["accept"]["type"] == "boolean"
    assert confirm["parameters"]["required"] == ["accept"]


def test_the_delegate_receipt_says_the_request_was_received_not_started() -> None:
    delegate, *_ = session_tools(CONTEXT)

    assert '{"status": "received"}' in delegate["description"]
    assert "started" not in delegate["description"]
    assert "Hermes agents" not in delegate["description"]


@pytest.mark.parametrize("dialect", [OPENAI, XAI])
async def test_the_switch_profile_tool_lists_the_hermes_agents_of_the_prompt_context(
    dialect: Dialect,
) -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, dialect=dialect, prompt_context=CONTEXT)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.close()

    (update,) = server.client_events
    tools = {tool["name"]: tool for tool in update["session"]["tools"]}
    assert tools["switch_profile"]["description"].endswith(
        " Hermes agents: stock-picker (Portfolio, holdings, trades); general."
    )


def test_without_profiles_there_is_no_switch_profile_tool() -> None:
    names = [tool["name"] for tool in session_tools(PromptContext())]

    assert names == ["delegate", "task_status", "cancel_task", "confirm_switch"]


@pytest.mark.parametrize(
    ("dialect", "instructions_session"),
    [
        (OPENAI, {"type": "realtime", "instructions": "Now on the stock-picker agent."}),
        (XAI, {"instructions": "Now on the stock-picker agent."}),
    ],
)
async def test_update_instructions_replaces_only_the_instructions_and_the_tools_still_work(
    dialect: Dialect, instructions_session: dict[str, Any]
) -> None:
    script = [
        *STARTED,
        {"wait_for": "session.update"},
        {"type": "session.updated", "event_id": "event_3"},
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "switch_profile",
            "arguments": '{"profile": "general"}',
        },
        {"type": "response.done", "response": {"id": "resp_1", "status": "completed"}},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server, dialect=dialect, prompt_context=CONTEXT)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.update_instructions("Now on the stock-picker agent.")
        events = await _rest(provider.events())

    connect_update, instructions_update = server.client_events
    assert "tools" in connect_update["session"]
    assert instructions_update == {"type": "session.update", "session": instructions_session}
    assert events == [
        SessionReady(),
        ResponseStarted(),
        DelegationRequested(
            id="call_1", offset_ms=0, target="switch_profile", tool_args={"profile": "general"}
        ),
        OutputDone(),
        Closed("remote_hangup"),
    ]


async def test_announcements_after_update_instructions_use_the_new_instructions() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.update_instructions("Now on the stock-picker agent.")
        await provider.say("Switched to stock-picker.")
        await provider.close()

    announcement = server.client_events[-1]
    assert announcement["type"] == "response.create"
    assert announcement["response"]["instructions"] == "Now on the stock-picker agent."


async def test_update_instructions_before_connect_raises() -> None:
    with pytest.raises(RuntimeError):
        await RealtimeApiProvider(API_KEY, dialect=OPENAI).update_instructions("hello")


async def test_connect_defaults_model_voice_and_turn_detection_and_seeds_history_summary() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, model="gpt-realtime-2.1-mini")
        await provider.connect(INSTRUCTIONS, None, "Rob asked about his portfolio.")
        await provider.close()

    assert server.connections[0].path == "/v1/realtime?model=gpt-realtime-2.1-mini"
    update, seed = server.client_events
    assert update["session"]["audio"]["output"]["voice"] == "marin"
    assert update["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "interrupt_response": True,
    }
    assert seed == {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "Rob asked about his portfolio."}],
        },
    }


async def test_turn_detection_from_config_cannot_turn_off_interrupt_response() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, turn_detection={"type": "server_vad", "interrupt_response": False})
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.close()

    turn_detection = server.client_events[0]["session"]["audio"]["input"]["turn_detection"]
    assert turn_detection == {"type": "server_vad", "interrupt_response": True}


async def test_session_maps_events_and_answers_the_delegate_call_within_50_ms() -> None:
    one_and_a_half_s = b"\x01\x00" * 36000  # 1.5 s of PCM16 at 24 kHz
    async with FakeWsServer(SCRIPTS / "realtime_delegation.jsonl") as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 1) == [SessionReady()]

        await provider.send_audio(one_and_a_half_s)
        before_call = await _next(events, 6)
        (call,) = await _next(events, 1)
        received_s = time.perf_counter()
        await provider.send_function_output("call_1", {"status": "started", "task_id": 7})
        output = await server.wait_for_client_event("conversation.item.create")
        receipt_ms = (time.perf_counter() - received_s) * 1000
        print(f"delegate receipt reached the provider {receipt_ms:.1f} ms after the call arrived")

        after_call = await _rest(events)
        await provider.close()

    assert before_call == [
        SpeechStarted(),
        SpeechStopped(),
        InputTranscript(delta="how is my ", final=False),
        InputTranscript(delta="portfolio doing", final=False),
        InputTranscript(delta="?", final=True),
        ResponseStarted(),
    ]
    assert call == DelegationRequested(
        id="call_1",
        offset_ms=1500,
        target="delegate",
        tool_args={"task": "check how Rob's portfolio is doing today"},
    )
    assert receipt_ms < RECEIPT_BUDGET_MS
    assert output == {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"status": "started", "task_id": 7}',
        },
    }
    assert after_call == [
        OutputDone(),
        ResponseStarted(),
        OutputTranscript(delta="On it."),
        OutputAudio(pcm=b"\x00\x00\x01\x00\xff\xff", item_id="item_asst_1"),
        OutputDone(),
        ResponseStarted(),
        DelegationRequested(id="call_2", offset_ms=1500, target="task_status", tool_args={"task_id": 3}),
        OutputDone(),
        ProviderError(
            message="Conversation already has an active response in progress.",
            code="conversation_already_has_active_response",
        ),
        Closed("remote_hangup"),
    ]
    assert [event["type"] for event in server.client_events] == [
        "session.update",
        "input_audio_buffer.append",
        "conversation.item.create",
        "response.create",
    ]
    assert server.client_events[1]["audio"] == base64.b64encode(one_and_a_half_s).decode()
    assert server.client_events[3] == {"type": "response.create"}


async def test_outputs_sent_during_a_response_get_one_response_create_after_it_is_done() -> None:
    script = [
        *STARTED,
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"wait_for": "conversation.item.create"},
        {"wait_for": "conversation.item.create"},
        {"type": "response.done", "response": {"id": "resp_1", "status": "completed"}},
        {"wait_for": "response.create"},
        {"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}},
        {"type": "response.done", "response": {"id": "resp_2", "status": "completed"}},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 2) == [SessionReady(), ResponseStarted()]
        asked = [
            await provider.send_function_output("call_1", {"status": "started"}),
            await provider.send_function_output("call_2", {"status": "ok"}),
        ]
        assert await _rest(events) == [OutputDone(), ResponseStarted(), OutputDone(), Closed("remote_hangup")]

    assert asked == [True, False]
    assert [event["type"] for event in server.client_events] == [
        "session.update",
        "conversation.item.create",
        "conversation.item.create",
        "response.create",
    ]


async def test_reply_due_after_a_response_cancelled_by_barge_in_is_dropped() -> None:
    script = [
        *STARTED,
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"wait_for": "conversation.item.create"},
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 900, "item_id": "item_9"},
        {
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "status": "cancelled",
                "status_details": {"type": "cancelled", "reason": "turn_detected"},
            },
        },
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 2) == [SessionReady(), ResponseStarted()]
        asked = await provider.send_function_output("call_1", {"status": "started"})
        assert await _rest(events) == [
            SpeechStarted(),
            OutputDone(reply_dropped=True),
            Closed("remote_hangup"),
        ]

    assert asked is True
    assert [event["type"] for event in server.client_events] == ["session.update", "conversation.item.create"]


async def test_output_sent_while_no_response_is_active_asks_for_a_response_at_once() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        asked = await provider.send_function_output("call_1", {"status": "ok"})
        await server.wait_for_client_event("response.create")
        await provider.close()

    assert asked is True
    assert [event["type"] for event in server.client_events] == [
        "session.update",
        "conversation.item.create",
        "response.create",
    ]


async def test_truncate_sends_the_played_length_of_the_item() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.truncate("item_asst_1", 800)
        truncate = await server.wait_for_client_event("conversation.item.truncate")
        await provider.close()

    assert truncate == {
        "type": "conversation.item.truncate",
        "item_id": "item_asst_1",
        "content_index": 0,
        "audio_end_ms": 800,
    }


async def test_truncate_is_a_no_op_when_the_dialect_does_not_support_it() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, dialect=WITHOUT_TRUNCATE)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.truncate("item_asst_1", 800)
        await provider.report_progress(REF, "Looking up your holdings.")
        await provider.close()

    assert [event["type"] for event in server.client_events] == ["session.update"]


async def test_announcements_are_out_of_band_responses_with_tools_disabled() -> None:
    result = "Report from the finance agent. <report>Your portfolio is up 2% today.</report>"
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.deliver_result(REF, result)
        await provider.say("Task 1 was cancelled.")
        await provider.close()

    def announcement(text: str) -> dict[str, Any]:
        return {
            "type": "response.create",
            "response": {
                "conversation": "none",
                "tools": [],
                "output_modalities": ["audio"],
                "instructions": INSTRUCTIONS,
                "input": [
                    {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}
                ],
            },
        }

    assert server.client_events[1:] == [announcement(result), announcement("Task 1 was cancelled.")]


async def test_announcement_without_out_of_band_uses_a_system_item_deleted_once_spoken() -> None:
    async with FakeWsServer(SCRIPTS / "realtime_announcement_fallback.jsonl") as server:
        provider = _provider(server, dialect=WITHOUT_OUT_OF_BAND)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 1) == [SessionReady()]
        await provider.deliver_result(REF, "Your portfolio is up 2% today.")
        assert await _rest(events) == [
            ResponseStarted(),
            OutputAudio(pcm=b"\x00\x00\x01\x00\xff\xff", item_id="item_asst_1"),
            OutputDone(),
            Closed("remote_hangup"),
        ]

    assert server.client_events[1:] == [
        {
            "type": "conversation.item.create",
            "item": {
                "id": "duplex_announcement_1",
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "Your portfolio is up 2% today."}],
            },
        },
        {
            "type": "response.create",
            "response": {"tool_choice": "none"},
        },
        {"type": "conversation.item.delete", "item_id": "duplex_announcement_1"},
    ]


async def test_malformed_and_unknown_events_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    script = [
        *STARTED,
        {"type": "response.output_audio.delta", "item_id": "item_1", "delta": "not base64!"},
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_9",
            "name": "delegate",
            "arguments": "{",
        },
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_9",
            "name": "delegate",
            "arguments": "[]",
        },
        {"type": "rate_limits.updated", "rate_limits": []},
        {"type": "ping"},
        {"type": "response.output_audio_transcript.delta", "item_id": "item_1", "delta": "ok"},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = await _rest(provider.events())

    assert events == [SessionReady(), OutputTranscript(delta="ok"), Closed("remote_hangup")]
    assert "Skipped a malformed openai-realtime response.output_audio.delta event" in caplog.text
    assert caplog.text.count("Skipped a malformed openai-realtime response.function_call_arguments.done") == 2


async def test_completed_transcript_without_deltas_is_delivered_whole() -> None:
    script = [
        *STARTED,
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_user_1",
            "content_index": 0,
            "transcript": "cancel task two",
        },
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = await _rest(provider.events())

    assert events == [
        SessionReady(),
        InputTranscript(delta="cancel task two", final=True),
        Closed("remote_hangup"),
    ]


async def test_connection_dropped_ends_with_connection_lost() -> None:
    script = [
        *STARTED,
        {"type": "response.output_audio.delta", "item_id": "item_1", "delta": "AAABAP//"},
        {"drop": True},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server)
        await provider.connect(INSTRUCTIONS, None, None)
        events = await _rest(provider.events())
        await provider.close()

    assert events == [
        SessionReady(),
        OutputAudio(pcm=b"\x00\x00\x01\x00\xff\xff", item_id="item_1"),
        Closed("connection_lost"),
    ]


async def test_rejected_session_update_raises_wire_error() -> None:
    async with FakeWsServer(SCRIPTS / "realtime_rejected.jsonl") as server:
        provider = _provider(server)
        with pytest.raises(WireError, match="rejected the session: Invalid value: 'nobody'"):
            await provider.connect(INSTRUCTIONS, "nobody", None)


async def test_session_not_updated_in_time_raises_wire_error_and_closes() -> None:
    async with FakeWsServer([{"type": "session.created"}, {"wait_for": "session.update"}]) as server:
        provider = _provider(server, start_timeout_s=0.1)
        with pytest.raises(WireError, match="did not start the session"):
            await provider.connect(INSTRUCTIONS, None, None)
        with pytest.raises(RuntimeError):
            await provider.send_audio(b"\x00\x00")

    assert [event["type"] for event in server.client_events] == ["session.update"]


async def test_unreachable_provider_raises_wire_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again before connecting, so nothing listens on it

    provider = RealtimeApiProvider(API_KEY, dialect=OPENAI, url=f"ws://127.0.0.1:{port}")
    with pytest.raises(WireError, match="unreachable"):
        await provider.connect(INSTRUCTIONS, None, None)


async def test_close_is_safe_to_call_twice_and_before_connect() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server)
        await provider.close()
        await provider.connect(INSTRUCTIONS, None, None)
        await provider.close()
        await provider.close()

    assert [event["type"] for event in server.client_events] == ["session.update"]


def test_from_config_builds_the_dialect_the_provider_name_selects() -> None:
    config = ProviderConfig(
        name="openai-realtime",
        model="gpt-realtime-2.1-mini",
        voice="cedar",
        api_key_env="OPENAI_API_KEY",
        turn_detection={"type": "semantic_vad"},
    )

    provider = RealtimeApiProvider.from_config(
        config, Secrets({"OPENAI_API_KEY": "sk-test"}), CONTEXT, dialect=OPENAI
    )

    assert provider.name == "openai-realtime"
    assert provider.capabilities == OPENAI.capabilities
    assert isinstance(provider.prompter, OpenAiRealtimePrompter)
    assert provider.prompt_context == CONTEXT


@pytest.mark.parametrize(
    ("name", "api_key_env", "key"),
    [("gpt-live", "OPENAI_API_KEY", "provider.name"), ("openai-realtime", None, "provider.api_key_env")],
)
def test_from_config_rejects_other_providers_and_a_missing_key_env(
    name: str, api_key_env: str | None, key: str
) -> None:
    config = ProviderConfig(name=name, model=None, voice=None, api_key_env=api_key_env, turn_detection=None)

    with pytest.raises(ConfigError) as caught:
        RealtimeApiProvider.from_config(
            config, Secrets({"OPENAI_API_KEY": "sk-test"}), CONTEXT, dialect=OPENAI
        )

    assert caught.value.key == key


# Grok dialect


def _system_item(item_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def test_xai_capabilities_turn_off_truncate_and_out_of_band_responses() -> None:
    provider = RealtimeApiProvider(API_KEY, dialect=XAI)

    assert provider.name == "xai-realtime"
    assert provider.capabilities == {
        "input_rate": 24000,
        "output_rate": 24000,
        "supports_truncate": False,
        "supports_out_of_band_response": False,
        "emits_speech_events": True,
        "delegation_style": "tool",
        "max_session_s": None,
        "replaces_instructions": True,
    }
    assert XAI.url == "wss://api.x.ai/v1/realtime"


async def test_grok_connect_sends_the_flat_session_update_with_the_xai_voice_prefix() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(
            server, dialect=XAI, turn_detection={"type": "server_vad", "idle_timeout_ms": 8000}
        )
        await provider.connect(INSTRUCTIONS, "ara", None)
        await provider.close()

    (connection,) = server.connections
    assert connection.path == "/v1/realtime?model=grok-voice-latest"
    assert connection.headers["authorization"] == f"Bearer {API_KEY}"
    (update,) = server.client_events
    print("session.update:", json.dumps(update, indent=2))
    pcm = {"type": "audio/pcm", "rate": 24000}
    assert update == {
        "type": "session.update",
        "session": {
            "voice": "xai_ara",
            "instructions": INSTRUCTIONS,
            "turn_detection": {"type": "server_vad", "idle_timeout_ms": 8000},
            "audio": {
                "input": {"format": pcm, "transcription": {"model": "grok-transcribe"}},
                "output": {"format": pcm},
            },
            "tools": update["session"]["tools"],
        },
    }
    assert update["session"]["tools"] == list(session_tools(PromptContext()))


@pytest.mark.parametrize(
    ("voice", "wire_voice"), [(None, "xai_ara"), ("xai_eve", "xai_eve"), ("rex", "xai_rex")]
)
async def test_grok_voice_gets_the_xai_prefix_exactly_once(voice: str | None, wire_voice: str) -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, dialect=XAI)
        await provider.connect(INSTRUCTIONS, voice, None)
        await provider.close()

    session = server.client_events[0]["session"]
    assert session["voice"] == wire_voice
    assert session["turn_detection"] == {"type": "server_vad"}


async def test_grok_rejects_turn_detection_other_than_server_vad_before_connecting() -> None:
    async with FakeWsServer(STARTED) as server:
        provider = _provider(server, dialect=XAI, turn_detection={"type": "semantic_vad"})
        with pytest.raises(ConfigError) as caught:
            await provider.connect(INSTRUCTIONS, None, None)

    assert caught.value.key == "provider.turn_detection.type"
    assert server.connections == []


async def test_grok_session_ignores_pings_tolerates_unimplemented_status_and_never_truncates() -> None:
    result = "Your portfolio is up 2% today."
    script = [
        {"type": "session.created", "session": {"id": "sess_x1", "voice": "xai_ara"}},
        {"wait_for": "session.update"},
        {"type": "ping"},
        {"type": "session.updated", "session": {"id": "sess_x1", "voice": "xai_ara", "x_extra": True}},
        {"type": "ping"},
        {"type": "input_audio_buffer.speech_started", "item_id": "item_user_1", "audio_start_ms": 0},
        {"wait_for": "response.create"},
        {"type": "response.created", "response": {"id": "resp_x1", "status": "in_progress"}},
        {"type": "response.output_audio.delta", "item_id": "item_asst_1", "delta": "AAABAP//"},
        {"type": "ping"},
        {
            "type": "response.done",
            "response": {"id": "resp_x1", "status": "completed", "status_details": "unimplemented"},
        },
        {"wait_for": "conversation.item.delete"},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server, dialect=XAI)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 2) == [SessionReady(), SpeechStarted()]
        await provider.truncate("item_asst_0", 800)
        await provider.deliver_result(REF, result)
        assert await _rest(events) == [
            ResponseStarted(),
            OutputAudio(pcm=b"\x00\x00\x01\x00\xff\xff", item_id="item_asst_1"),
            OutputDone(),
            Closed("remote_hangup"),
        ]

    assert server.client_events[1:] == [
        _system_item("duplex_announcement_1", result),
        {"type": "response.create", "response": {"tool_choice": "none"}},
        {"type": "conversation.item.delete", "item_id": "duplex_announcement_1"},
    ]


async def test_refused_announcement_item_is_deleted_after_the_next_response() -> None:
    script = [
        *STARTED,
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {"type": "response.output_audio.delta", "item_id": "item_asst_1", "delta": "AAABAP//"},
        {"wait_for": "response.create"},
        {
            "type": "error",
            "error": {
                "message": "Conversation already has an active response in progress.",
                "code": "conversation_already_has_active_response",
            },
        },
        {"type": "response.done", "response": {"id": "resp_1", "status": "completed"}},
        {"wait_for": "input_audio_buffer.append"},
        {"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}},
        {"type": "response.done", "response": {"id": "resp_2", "status": "completed"}},
        {"wait_for": "conversation.item.delete"},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server:
        provider = _provider(server, dialect=XAI)
        await provider.connect(INSTRUCTIONS, None, None)
        events = provider.events()
        assert await _next(events, 3) == [
            SessionReady(),
            ResponseStarted(),
            OutputAudio(pcm=b"\x00\x00\x01\x00\xff\xff", item_id="item_asst_1"),
        ]
        await provider.say("Task 1 was cancelled.")
        assert await _next(events, 2) == [
            ProviderError(
                message="Conversation already has an active response in progress.",
                code="conversation_already_has_active_response",
            ),
            OutputDone(),
        ]
        await provider.send_audio(b"\x00\x00")  # sent only once the running response is done
        assert await _rest(events) == [ResponseStarted(), OutputDone(), Closed("remote_hangup")]

    assert [event["type"] for event in server.client_events] == [
        "session.update",
        "conversation.item.create",
        "response.create",
        "input_audio_buffer.append",
        "conversation.item.delete",
    ]


def test_from_config_builds_the_xai_dialect_for_xai_realtime() -> None:
    config = ProviderConfig(
        name="xai-realtime", model=None, voice="ara", api_key_env="XAI_API_KEY", turn_detection=None
    )

    provider = RealtimeApiProvider.from_config(
        config, Secrets({"XAI_API_KEY": "xai-test"}), CONTEXT, dialect=XAI
    )

    assert provider.name == "xai-realtime"
    assert provider.capabilities == XAI.capabilities
    assert isinstance(provider.prompter, GrokPrompter)
