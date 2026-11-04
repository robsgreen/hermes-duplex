"""A scriptable fake provider for tests: it replays a JSONL script of events and records what the session sent.

Script format: one JSON object per line (blank lines are skipped). Each line is a step with exactly one
trigger key and an ``event``. Steps fire strictly in order; a step's trigger is only watched once the
step before it has fired.

Triggers:

- ``"at_input_ms": N`` fires once the provider has received at least N ms of input audio through
  ``send_audio`` since the latest ``connect``.
- ``"after_ms": N`` fires N ms of wall-clock time after the previous step fired, or after ``connect``
  for the first step of a connection.
- ``"on_sent": "<method>"`` fires once the session calls that method (``connect``, ``truncate``,
  ``send_function_output``, ``report_progress``, ``deliver_result``, ``say``, ``update_instructions``,
  or ``close``) after the previous step fired.

Events, each mapping to one ``ProviderEvent``:

- ``{"type": "speech_started"}``, ``{"type": "speech_stopped"}``, ``{"type": "response_started"}``,
  ``{"type": "output_done"}``
- ``{"type": "output_audio", "ms": 600, "item_id": "item-1"}``: ``ms`` of silent PCM16 at the output
  rate; ``item_id`` is optional.
- ``{"type": "input_transcript", "delta": "how is", "final": false}``: ``final`` defaults to true.
- ``{"type": "output_transcript", "delta": "Sure."}``
- ``{"type": "delegation", "id": "d1", "tool_args": {"task": "how is my portfolio doing"}}``:
  ``target`` defaults to ``"delegate"`` for tool-style capabilities and ``"client"`` for live style;
  ``tool_args`` and ``offset_ms`` default to null.
- ``{"type": "error", "message": "rate limited", "code": "rate_limit"}``: ``code`` is optional.
- ``{"type": "close", "reason": "expired"}``: ends the connection with ``Closed``; ``reason`` defaults to
  ``"script"``. The remaining steps resume on the next ``connect``.

Example::

    {"at_input_ms": 1500, "event": {"type": "delegation", "id": "d1", "tool_args": {"task": "hi"}}}
    {"on_sent": "deliver_result", "event": {"type": "output_audio", "ms": 600}}

Every ``connect`` emits ``SessionReady`` before any script step, so scripts never list it.

With speech events (``emits_speech_events``), the fake also acts as a model that answers at once and
says nothing: each ``send_function_output``, ``deliver_result``, and ``say`` on an open connection emits
``ResponseStarted`` and then ``OutputDone``, ahead of any step that call triggers.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ...config import ConfigError, ProviderConfig, Secrets
from ...prompts.base import PromptContext, VoicePrompter
from ..base import (
    DELEGATE_TOOL,
    Closed,
    DelegationRef,
    DelegationRequested,
    InputTranscript,
    OutputAudio,
    OutputDone,
    OutputTranscript,
    ProviderCapabilities,
    ProviderError,
    ProviderEvent,
    ResponseStarted,
    SessionReady,
    SpeechStarted,
    SpeechStopped,
)
from ..gpt_live.prompt import GptLivePrompter

DEFAULT_CAPABILITIES: ProviderCapabilities = {
    "input_rate": 24000,
    "output_rate": 24000,
    "supports_truncate": True,
    "supports_out_of_band_response": True,
    "emits_speech_events": True,
    "delegation_style": "tool",
    "max_session_s": None,
    "replaces_instructions": True,
}
"""The fake's capabilities unless overridden: a Realtime-family provider with tool-style delegation."""

RECORDED_METHODS = frozenset(
    {
        "connect",
        "truncate",
        "send_function_output",
        "report_progress",
        "deliver_result",
        "say",
        "update_instructions",
        "close",
    }
)
"""The provider calls the fake records, and the methods an ``on_sent`` trigger can name."""

_TRIGGERS = ("at_input_ms", "after_ms", "on_sent")

# Each event type's optional and required fields besides "type"; any other field is a script error.
_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "speech_started": frozenset(),
    "speech_stopped": frozenset(),
    "response_started": frozenset(),
    "output_done": frozenset(),
    "output_audio": frozenset({"ms", "item_id"}),
    "input_transcript": frozenset({"delta", "final"}),
    "output_transcript": frozenset({"delta"}),
    "delegation": frozenset({"id", "target", "tool_args", "offset_ms"}),
    "error": frozenset({"message", "code"}),
    "close": frozenset({"reason"}),
}


class ScriptError(ValueError):
    """A provider script is malformed; the message names the line or step and what is wrong."""


@dataclass(frozen=True)
class SentCall:
    """One call the session made on the provider.

    ``method`` is one of ``RECORDED_METHODS``; ``args`` holds its arguments by parameter name, as JSON
    values (a ``DelegationRef`` becomes a dict of its fields).
    """

    method: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class _AtInputMs:
    ms: int


@dataclass(frozen=True)
class _AfterMs:
    ms: int


@dataclass(frozen=True)
class _OnSent:
    method: str


type _Trigger = _AtInputMs | _AfterMs | _OnSent


def load_script(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL script into its steps, skipping blank lines.

    Raises ``OSError`` when the file cannot be read and ``ScriptError`` naming the line when a line is
    not a JSON object. The steps themselves are checked by ``FakeProvider``.
    """
    steps = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError as error:
            raise ScriptError(f"line {number}: invalid JSON ({error.msg})") from None
        if not isinstance(step, dict):
            raise ScriptError(f"line {number}: must be a JSON object")
        steps.append(step)
    return steps


class FakeProvider:
    """A ``RealtimeProvider`` that plays script steps instead of talking to a model.

    Each ``connect`` opens a connection that emits ``SessionReady`` and then plays the remaining script
    steps; ``Closed`` (a scripted close, or ``close()``) ends it, and a later ``connect`` resumes the
    script where it stopped. Every call in ``RECORDED_METHODS`` is kept in ``calls`` and, when
    ``record_path`` is set, appended to that file as one JSON line ``{"method": ..., "args": {...}}``
    as it happens, so a test in another process can read it.
    """

    name = "fake"

    def __init__(
        self,
        script: Iterable[Mapping[str, Any]] = (),
        *,
        capabilities: Mapping[str, object] | None = None,
        record_path: Path | None = None,
        prompter: VoicePrompter | None = None,
        prompt_context: PromptContext | None = None,
    ) -> None:
        """Validate ``script`` (steps in the module's script format) and ``capabilities``.

        ``capabilities`` overrides any of the ``DEFAULT_CAPABILITIES`` keys. ``prompter`` None means
        ``GptLivePrompter``, whose results are plain framed text. Raises ``ScriptError`` naming the
        step for a malformed step, and ``ValueError`` naming the key for an unknown capability.
        """
        overrides = dict(capabilities or {})
        unknown = overrides.keys() - DEFAULT_CAPABILITIES.keys()
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")
        self.capabilities = cast(ProviderCapabilities, {**DEFAULT_CAPABILITIES, **overrides})
        self.prompter: VoicePrompter = prompter or GptLivePrompter()
        self.prompt_context = prompt_context or PromptContext()
        self._steps = [self._parse_step(number, step) for number, step in enumerate(script, start=1)]
        self._record_path = record_path

        self._calls: list[SentCall] = []
        self._events: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self._changed = asyncio.Condition()
        self._player: asyncio.Task[None] | None = None
        self._connected = False
        self._next_step = 0
        self._calls_before_step = 0  # len(calls) when the previous step fired; on_sent looks after it
        self._input_samples = 0

    @classmethod
    def from_config(
        cls, config: ProviderConfig, secrets: Secrets, prompt_context: PromptContext
    ) -> FakeProvider:
        """Build the fake from the ``provider`` config section; it needs no secrets.

        Raises ``ConfigError`` on ``provider.script`` when the script cannot be read or is malformed.
        """
        try:
            script = load_script(config.script) if config.script is not None else []
            return cls(
                script,
                capabilities=config.capabilities,
                record_path=config.record_path,
                prompt_context=prompt_context,
            )
        except (OSError, ScriptError) as error:
            raise ConfigError("provider.script", f"{config.script}: {error}") from None

    @property
    def calls(self) -> tuple[SentCall, ...]:
        """Every recorded call, oldest first."""
        return tuple(self._calls)

    @property
    def input_ms(self) -> int:
        """Milliseconds of input audio received since the latest ``connect``."""
        return self._input_samples * 1000 // self.capabilities["input_rate"]

    async def connect(self, instructions: str, voice: str | None, history_summary: str | None) -> None:
        await self._record(
            "connect", {"instructions": instructions, "voice": voice, "history_summary": history_summary}
        )
        self._stop_player()
        self._connected = True
        self._input_samples = 0
        self._events.put_nowait(SessionReady())
        self._player = asyncio.create_task(self._play())

    async def send_audio(self, pcm16: bytes) -> None:
        self._input_samples += len(pcm16) // 2
        async with self._changed:
            self._changed.notify_all()

    async def events(self) -> AsyncIterator[ProviderEvent]:
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, Closed):
                return

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._record("truncate", {"item_id": item_id, "audio_end_ms": audio_end_ms})

    async def send_function_output(self, call_id: str, payload: Mapping[str, Any]) -> bool:
        self._respond()
        await self._record("send_function_output", {"call_id": call_id, "payload": dict(payload)})
        return True

    async def report_progress(self, ref: DelegationRef, text: str) -> None:
        await self._record("report_progress", {"ref": dataclasses.asdict(ref), "text": text})

    async def deliver_result(self, ref: DelegationRef, text: str) -> None:
        self._respond()
        await self._record("deliver_result", {"ref": dataclasses.asdict(ref), "text": text})

    async def say(self, text: str) -> None:
        self._respond()
        await self._record("say", {"text": text})

    async def update_instructions(self, text: str) -> None:
        """Record the call, whatever ``replaces_instructions`` says, so a test sees a misuse."""
        await self._record("update_instructions", {"text": text})

    async def close(self) -> None:
        await self._record("close", {})
        self._stop_player()
        self._end_connection("closed by client")

    async def _play(self) -> None:
        """Fire the remaining script steps in order until the script or the connection ends."""
        while self._next_step < len(self._steps):
            trigger, event = self._steps[self._next_step]
            await self._wait_for(trigger)
            self._next_step += 1
            self._calls_before_step = len(self._calls)
            if isinstance(event, Closed):
                self._player = None
                self._end_connection(event.reason)
                return
            self._events.put_nowait(event)

    async def _wait_for(self, trigger: _Trigger) -> None:
        match trigger:
            case _AfterMs(ms):
                await asyncio.sleep(ms / 1000)
            case _AtInputMs(ms):
                async with self._changed:
                    await self._changed.wait_for(lambda: self.input_ms >= ms)
            case _OnSent(method):
                async with self._changed:
                    await self._changed.wait_for(
                        lambda: any(call.method == method for call in self._calls[self._calls_before_step :])
                    )

    def _respond(self) -> None:
        """Emit an empty response to a call that makes a real model respond, when speech events are on."""
        if self._connected and self.capabilities["emits_speech_events"]:
            self._events.put_nowait(ResponseStarted())
            self._events.put_nowait(OutputDone())

    async def _record(self, method: str, args: dict[str, Any]) -> None:
        call = SentCall(method, args)
        self._calls.append(call)
        if self._record_path is not None:
            with self._record_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"method": method, "args": args}) + "\n")
        async with self._changed:
            self._changed.notify_all()

    def _stop_player(self) -> None:
        if self._player is not None:
            self._player.cancel()
            self._player = None

    def _end_connection(self, reason: str) -> None:
        """Emit ``Closed`` once for the open connection; a no-op when none is open."""
        if self._connected:
            self._connected = False
            self._events.put_nowait(Closed(reason))

    def _parse_step(self, number: int, step: Mapping[str, Any]) -> tuple[_Trigger, ProviderEvent]:
        triggers = [key for key in _TRIGGERS if key in step]
        unknown = step.keys() - {*_TRIGGERS, "event"}
        if len(triggers) != 1 or unknown:
            raise ScriptError(f"step {number}: needs exactly one of {', '.join(_TRIGGERS)} and an event")
        key = triggers[0]
        value = step[key]
        trigger: _Trigger
        if key == "on_sent":
            if value not in RECORDED_METHODS:
                raise ScriptError(
                    f"step {number}: on_sent must be one of {', '.join(sorted(RECORDED_METHODS))}"
                )
            trigger = _OnSent(value)
        else:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScriptError(f"step {number}: {key} must be a non-negative integer")
            trigger = _AtInputMs(value) if key == "at_input_ms" else _AfterMs(value)

        event = step.get("event")
        if not isinstance(event, dict):
            raise ScriptError(f"step {number}: event must be a JSON object")
        try:
            return trigger, self._parse_event(event)
        except (KeyError, TypeError, ValueError) as error:
            raise ScriptError(f"step {number}: invalid {event.get('type')!r} event ({error!r})") from None

    def _parse_event(self, event: Mapping[str, Any]) -> ProviderEvent:
        """Build the ``ProviderEvent`` an event object describes; raises KeyError on a missing field."""
        kind = event.get("type")
        if kind not in _EVENT_FIELDS:
            raise ScriptError(f"unknown event type {kind!r}")
        unknown = event.keys() - _EVENT_FIELDS[kind] - {"type"}
        if unknown:
            raise ScriptError(f"unknown fields {', '.join(sorted(unknown))}")
        match kind:
            case "speech_started":
                return SpeechStarted()
            case "speech_stopped":
                return SpeechStopped()
            case "response_started":
                return ResponseStarted()
            case "output_done":
                return OutputDone()
            case "output_audio":
                samples = self.capabilities["output_rate"] * int(event["ms"]) // 1000
                return OutputAudio(pcm=bytes(2 * samples), item_id=event.get("item_id"))
            case "input_transcript":
                return InputTranscript(delta=event["delta"], final=event.get("final", True))
            case "output_transcript":
                return OutputTranscript(delta=event["delta"])
            case "delegation":
                default_target = (
                    DELEGATE_TOOL if self.capabilities["delegation_style"] == "tool" else "client"
                )
                return DelegationRequested(
                    id=event["id"],
                    offset_ms=event.get("offset_ms"),
                    target=event.get("target", default_target),
                    tool_args=event.get("tool_args"),
                )
            case "error":
                return ProviderError(message=event["message"], code=event.get("code"))
            case _:  # "close"
                return Closed(reason=event.get("reason", "script"))
