"""The Realtime-family adapter: OpenAI Realtime GA (and, by dialect, compatible APIs) over a WebSocket.

The model hands work to this app by calling the ``delegate``, ``task_status``, ``cancel_task``,
``switch_profile``, and ``confirm_switch`` function tools; each call becomes a ``DelegationRequested``
whose ``target`` is the tool's name. The session answers a call with ``send_function_output``, which
lets the model go on talking once the response that made the call is done. Results and notices are
spoken by announcement responses with tools disabled: out of band (outside the conversation) where the
dialect supports it, otherwise through a system item that is deleted once spoken. A question the user
is to answer stays in the conversation instead, so the model understands the answer.

Dialects: ``openai-realtime`` (OpenAI Realtime GA, ``openai/``) and ``xai-realtime`` (xAI Grok Voice,
``xai/``, which speaks the GA event names with a flatter session shape and has no truncate or
out-of-band responses).

A ``Dialect`` holds everything that differs between Realtime-compatible providers: endpoint, defaults,
capabilities, prompter, and the shape of the session configuration. Event shapes follow the openai
3.42.1 SDK (``types/realtime/*``), which this package does not depend on.
"""

import asyncio
import base64
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlencode

from ...config import ConfigError, ProviderConfig, Secrets
from ...prompts.base import PromptContext, VoicePrompter
from ...prompts.blocks import agent_list
from ..base import (
    CANCEL_TASK_TOOL,
    CONFIRM_SWITCH_TOOL,
    DELEGATE_RECEIPT,
    DELEGATE_TOOL,
    SWITCH_PROFILE_TOOL,
    TASK_STATUS_TOOL,
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
from ..wire import CONNECT_TIMEOUT_S, WireClient, WireError

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
UPDATE_TIMEOUT_S = 5.0
"""Default bound on the wait for the server to confirm, or reject, an ``update_instructions``."""

_TASK_CHOICE_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "description": "The task's id from an earlier task_status result; an internal id, never said aloud.",
        },
        "about": {
            "type": "string",
            "description": 'The user\'s words for which task they mean, such as "the Tesla question".',
        },
    },
    "required": [],
}
"""Parameters of ``task_status`` and ``cancel_task``: which of the user's tasks is meant, by id or by
the user's words; neither means all of their tasks, or for a cancel the only open (queued or running) one."""


def session_tools(ctx: PromptContext) -> tuple[Mapping[str, Any], ...]:
    """The function tools every Realtime session offers, in the flat Realtime tool shape.

    Always ``delegate``, ``task_status``, ``cancel_task``, and ``confirm_switch``; ``switch_profile``
    too when ``ctx.profiles`` names any profile. Its description lists those profiles with their
    descriptions, and its ``profile`` argument is the user's own words for the agent, which the session
    resolves to a profile by name or alias.
    """
    delegate = (
        f"Hand a request to Hermes. Returns at once with only {json.dumps(DELEGATE_RECEIPT)}: the request "
        "arrived, but no task is running yet. A start notice, or a question about switching agents, "
        "follows later; keep talking with the user meanwhile."
    )
    tools: list[Mapping[str, Any]] = [
        {
            "type": "function",
            "name": DELEGATE_TOOL,
            "description": delegate,
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The request restated as one self-contained task.",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "type": "function",
            "name": TASK_STATUS_TOOL,
            "description": (
                "Report how the user's delegated tasks are going: a spoken summary, and each task's id, "
                "topic, agent, and state."
            ),
            "parameters": _TASK_CHOICE_PARAMETERS,
        },
        {
            "type": "function",
            "name": CANCEL_TASK_TOOL,
            "description": "Stop one of the user's delegated tasks.",
            "parameters": _TASK_CHOICE_PARAMETERS,
        },
    ]
    if ctx.profiles:
        tools.append(
            {
                "type": "function",
                "name": SWITCH_PROFILE_TOOL,
                "description": (
                    "Switch the Hermes agent that runs the user's requests from now on, when the user asks "
                    f"to. Hermes agents: {agent_list(ctx.profiles)}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile": {
                            "type": "string",
                            "description": (
                                "The agent the user asked for, in their words, such as a name or nickname."
                            ),
                        },
                    },
                    "required": ["profile"],
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "name": CONFIRM_SWITCH_TOOL,
            "description": (
                "Give the user's answer to the assistant's question about switching to another agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accept": {
                        "type": "boolean",
                        "description": "True when the user said yes to the switch, false when they said no.",
                    },
                },
                "required": ["accept"],
            },
        }
    )
    return tuple(tools)


@dataclass(frozen=True)
class SessionSettings:
    """What a session is configured with, independent of how a dialect spells it on the wire.

    ``turn_detection`` is the ``provider.turn_detection`` config mapping, or None for the dialect's
    default.
    """

    instructions: str
    voice: str
    turn_detection: Mapping[str, Any] | None
    transcription_model: str
    tools: Sequence[Mapping[str, Any]]
    """The function tools, in the flat Realtime tool shape (``session_tools``)."""


@dataclass(frozen=True)
class Dialect:
    """One Realtime-compatible provider's endpoint, defaults, capabilities, and session shape."""

    provider_name: str
    """The ``provider.name`` that selects this dialect, e.g. ``openai-realtime``."""

    url: str
    """The ``wss://`` endpoint; the model goes in the ``model`` query parameter."""

    default_model: str
    default_voice: str
    default_transcription_model: str
    capabilities: ProviderCapabilities

    prompter: VoicePrompter
    """Writes the instructions, task reports, and reconnect summaries for this provider's models."""

    session_config: Callable[[SessionSettings], dict[str, Any]]
    """Builds the ``session`` object of the ``session.update`` event."""

    instructions_config: Callable[[str], dict[str, Any]]
    """Builds the ``session`` object of a ``session.update`` that replaces only the instructions,
    leaving the tools and audio settings as they are."""


class RealtimeApiProvider:
    """A ``RealtimeProvider`` for a Realtime-compatible API. Each ``connect`` opens a new session."""

    def __init__(
        self,
        api_key: str,
        *,
        dialect: Dialect,
        model: str | None = None,
        url: str | None = None,
        turn_detection: Mapping[str, Any] | None = None,
        start_timeout_s: float = CONNECT_TIMEOUT_S,
        update_timeout_s: float = UPDATE_TIMEOUT_S,
        prompt_context: PromptContext | None = None,
    ) -> None:
        """Prepare a provider that speaks ``dialect`` to ``url`` with ``api_key`` and runs ``model``.

        ``model`` and ``url`` None mean the dialect's defaults. ``turn_detection`` is passed to the
        provider as the dialect spells it (OpenAI always turns ``interrupt_response`` on); None means
        the dialect's default.
        ``start_timeout_s`` bounds the wait for ``session.updated`` after the WebSocket opens, and
        ``update_timeout_s`` the wait for it after ``update_instructions``.
        ``prompt_context`` is the deployment the instructions describe (None: the defaults); its
        profiles are offered in the ``switch_profile`` tool.
        """
        self.name = dialect.provider_name
        self.capabilities = dialect.capabilities
        self.prompter = dialect.prompter
        self.prompt_context = prompt_context or PromptContext()
        self._dialect = dialect
        self._api_key = api_key
        self._model = model or dialect.default_model
        self._url = url or dialect.url
        self._turn_detection = turn_detection
        self._start_timeout_s = start_timeout_s
        self._update_timeout_s = update_timeout_s
        self._instructions = ""
        self._wire: WireClient | None = None
        self._received: asyncio.Queue[dict[str, Any] | None] | None = None
        """Server events for ``events()``, filled by ``_read``; None marks the connection's end."""
        self._reader: asyncio.Task[None] | None = None
        self._updates = 0
        self._pending_updates: deque[tuple[str, asyncio.Future[None]]] = deque()
        """Instruction updates sent and not yet confirmed or rejected, oldest first, by event id."""
        self._input_samples = 0
        self._input_transcripts: dict[str, str] = {}
        self._announcements = 0
        self._requested_announcements: list[str] = []
        self._speaking_announcements: list[str] = []
        self._response_requested = False
        """A ``response.create`` was sent and its ``response.created`` has not arrived yet."""
        self._response_running = False
        """A response has started (``response.created``) and is not done yet."""
        self._reply_due = False
        """Function outputs were sent while a response was active; a ``response.create`` follows its
        ``response.done`` unless that response was cancelled."""

    @classmethod
    def from_config(
        cls, config: ProviderConfig, secrets: Secrets, prompt_context: PromptContext, *, dialect: Dialect
    ) -> Self:
        """Build the provider for ``dialect`` from the ``provider`` config section; the key comes from
        ``api_key_env``.

        Raises ``ConfigError`` on ``provider.name`` when it is not the name that selects ``dialect``,
        and on ``provider.api_key_env`` when it is unset (config loading already rejects that).
        """
        if config.name != dialect.provider_name:
            raise ConfigError("provider.name", f"'{config.name}' is not the {dialect.provider_name} provider")
        if config.api_key_env is None:
            raise ConfigError("provider.api_key_env", f"is required for provider '{config.name}'")
        return cls(
            secrets.get(config.api_key_env),
            dialect=dialect,
            model=config.model,
            turn_detection=config.turn_detection,
            prompt_context=prompt_context,
        )

    async def connect(self, instructions: str, voice: str | None, history_summary: str | None) -> None:
        """Open a session, configure it, and return once the server sends ``session.updated``.

        Closes any session this provider still has open first. ``voice`` None means the dialect's
        default. ``history_summary`` is added to the conversation as a system message. Raises
        ``ConfigError``, before connecting, when the dialect cannot express the configured turn
        detection, and ``WireError`` when the WebSocket cannot be opened, or when the server rejects the
        configuration, ends the connection, or does not confirm it within ``start_timeout_s``; the
        connection is closed in every such case.
        """
        await self.close()
        self._wire = self._received = self._reader = None
        self._instructions = instructions
        self._input_samples = 0
        self._input_transcripts = {}
        self._requested_announcements = []
        self._speaking_announcements = []
        self._response_requested = self._response_running = self._reply_due = False
        session = self._dialect.session_config(
            SessionSettings(
                instructions=instructions,
                voice=voice or self._dialect.default_voice,
                turn_detection=self._turn_detection,
                transcription_model=self._dialect.default_transcription_model,
                tools=session_tools(self.prompt_context),
            )
        )

        wire = await WireClient.connect(f"{self._url}?{urlencode({'model': self._model})}", self._api_key)
        incoming = wire.events()
        try:
            await wire.send({"type": "session.update", "session": session})
            if history_summary is not None:
                await wire.send(
                    {"type": "conversation.item.create", "item": _system_message(history_summary)}
                )
            async with asyncio.timeout(self._start_timeout_s):
                await _wait_for_update(incoming)
        except TimeoutError:
            await wire.close()
            raise WireError("the voice provider did not start the session") from None
        except BaseException:
            await wire.close()
            raise
        self._wire = wire
        self._received = asyncio.Queue()
        self._reader = asyncio.create_task(self._read(incoming, self._received))
        logger.info("%s session started (model %s)", self.name, self._model)

    async def send_audio(self, pcm16: bytes) -> None:
        """Send mono PCM16LE audio at 24 kHz; dropped once the session has ended."""
        wire = self._open_wire()
        self._input_samples += len(pcm16) // 2
        await wire.send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16).decode("ascii")}
        )

    async def events(self) -> AsyncIterator[ProviderEvent]:
        """Yield ``SessionReady``, then the session's events, ending with exactly one ``Closed``.

        A function call's ``offset_ms`` is how much audio ``send_audio`` had sent this session when
        the call arrived. ``Closed`` carries the connection's ``close_reason``. Server events this
        adapter does not map (pings among them), and malformed ones, are skipped.
        """
        wire = self._open_wire()
        received = self._received
        assert received is not None
        yield SessionReady()
        while (raw := await received.get()) is not None:
            try:
                event = self._map_event(raw)
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("Skipped a malformed %s %s event: %r", self.name, raw.get("type"), error)
                continue
            await self._follow_announcements(raw.get("type"))
            if await self._follow_responses(raw):
                event = OutputDone(reply_dropped=True)
            if event is not None:
                yield event
        yield Closed(wire.close_reason or "connection_lost")

    async def send_function_output(self, call_id: str, payload: Mapping[str, Any]) -> bool:
        """Answer the function call ``call_id`` with ``payload`` (sent as JSON), and let the model respond.

        Answer each call once. The output is sent at once; the ``response.create`` that lets the model
        speak again waits until the active response (the one that made the call) is done, since a
        conversation runs one response at a time. Outputs sent during one response share the single
        ``response.create`` sent after it. When that response ends cancelled (a person cut in), the
        reply is dropped, so the model does not talk over the person: the outputs stay in the
        conversation for the person's own turn, and ``events()`` reports the drop as
        ``OutputDone(reply_dropped=True)``. Returns whether this output asked for a response of its
        own: False when it joins one already due.
        """
        wire = self._open_wire()
        active = self._response_requested or self._response_running
        asks = not (active and self._reply_due)
        if active:
            self._reply_due = True
        else:
            self._response_requested = True
        item = {"type": "function_call_output", "call_id": call_id, "output": json.dumps(payload)}
        await wire.send({"type": "conversation.item.create", "item": item})
        if not active:
            await wire.send({"type": "response.create"})
        return asks

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Trim output item ``item_id`` to the ``audio_end_ms`` the user heard; a no-op where unsupported."""
        if not self.capabilities["supports_truncate"]:
            return
        await self._open_wire().send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def report_progress(self, ref: DelegationRef, text: str) -> None:
        """A no-op: Realtime models have no channel for silent progress."""

    async def deliver_result(self, ref: DelegationRef, text: str) -> None:
        """Have the model speak ``text`` (a result the prompter already marked as data) as an announcement."""
        await self._announce(text)

    async def say(self, text: str, *, expects_answer: bool = False) -> None:
        """Have the model speak the notice ``text`` as an announcement (``_announce``)."""
        await self._announce(text, expects_answer=expects_answer)

    async def update_instructions(self, text: str) -> None:
        """Replace the open session's instructions with ``text`` by a ``session.update`` that carries
        only the instructions, so the tools, voice, audio settings, and conversation stay as they are,
        and return once the server confirms it with ``session.updated``.

        Later announcements use ``text`` too. Raises ``RuntimeError`` before ``connect`` has succeeded,
        and ``WireError`` when the server rejects the update (an ``error`` naming its event id, which
        ``events()`` then does not report), does not confirm it within ``update_timeout_s``, or ends
        the connection first; later announcements then keep the old instructions.
        """
        wire = self._open_wire()
        self._updates += 1
        event_id = f"duplex_instructions_{self._updates}"
        settled = asyncio.get_running_loop().create_future()
        self._pending_updates.append((event_id, settled))
        session = self._dialect.instructions_config(text)
        await wire.send({"type": "session.update", "event_id": event_id, "session": session})
        try:
            async with asyncio.timeout(self._update_timeout_s):
                await settled
        except TimeoutError:
            raise WireError("the voice provider did not confirm the new instructions") from None
        finally:
            if (event_id, settled) in self._pending_updates:
                self._pending_updates.remove((event_id, settled))
        self._instructions = text

    async def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._wire is not None:
            await self._wire.close()
        if self._reader is not None:
            await asyncio.wait([self._reader])  # it ends with the connection; unlike await, never cancels it

    async def _read(
        self, incoming: AsyncIterator[dict[str, Any]], received: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        """Pass the connection's server events to ``events()`` through ``received``, ending with None,
        except those that settle an ``update_instructions``.

        Reading here rather than in ``events()`` lets an update settle even while nobody iterates
        ``events()``, or while its consumer is busy with an earlier event. When the connection ends,
        updates still waiting fail.
        """
        try:
            async for raw in incoming:
                if not self._settle_update(raw):
                    received.put_nowait(raw)
        finally:
            for _, settled in self._pending_updates:
                settled.set_exception(WireError("the voice provider ended the session"))
            received.put_nowait(None)

    def _settle_update(self, raw: Mapping[str, Any]) -> bool:
        """Settle the waiting ``update_instructions`` that ``raw`` answers, and return whether it did.

        ``session.updated`` confirms the oldest waiting update, since the server answers session
        updates in order; an ``error`` whose ``event_id`` names one rejects it. Without waiting
        updates, nothing is settled.
        """
        match raw.get("type"):
            case "session.updated" if self._pending_updates:
                _, settled = self._pending_updates.popleft()
                settled.set_result(None)
                return True
            case "error":
                error = raw.get("error")
                event_id = error.get("event_id") if isinstance(error, Mapping) else None
                rejected = next(
                    (pending for pending in self._pending_updates if pending[0] == event_id), None
                )
                if rejected is None:
                    return False
                self._pending_updates.remove(rejected)
                message = _error_message(raw)
                logger.warning("The voice provider rejected the new instructions: %s", message)
                rejected[1].set_exception(
                    WireError(f"the voice provider rejected the new instructions: {message}")
                )
                return True
            case _:
                return False

    async def _announce(self, text: str, *, expects_answer: bool = False) -> None:
        """Start a response, with tools disabled, that speaks ``text`` without the model answering it.

        Out of band, the response has the session's instructions and ``text`` as its only input and
        leaves the conversation untouched. Otherwise ``text`` goes into the conversation as a system
        item that ``events()`` deletes when the response is done (see ``_follow_announcements``).

        When ``expects_answer`` is true, what is said stays in the conversation, so the model
        understands the user's answer to it: with out-of-band support the response belongs to the
        default conversation (it still reads only the instructions and ``text``, and what it speaks is
        added there), and otherwise the system item is kept.
        """
        wire = self._open_wire()
        self._response_requested = True
        if self.capabilities["supports_out_of_band_response"]:
            response = {
                **({} if expects_answer else {"conversation": "none"}),
                "tools": [],
                "output_modalities": ["audio"],
                "instructions": self._instructions,
                "input": [_system_message(text)],
            }
            await wire.send({"type": "response.create", "response": response})
            return
        self._announcements += 1
        item_id = f"duplex_announcement_{self._announcements}"
        await wire.send(
            {"type": "conversation.item.create", "item": {"id": item_id, **_system_message(text)}}
        )
        await wire.send({"type": "response.create", "response": {"tool_choice": "none"}})
        if not expects_answer:
            self._requested_announcements.append(item_id)

    async def _follow_announcements(self, event_type: object) -> None:
        """Delete fallback announcement items once the response that spoke them is done.

        Responses in a conversation run one at a time, so the first response created after an
        announcement was requested is taken to be the one speaking it. This relies only on the order
        of ``response.created`` and ``response.done``, not on the provider echoing response metadata,
        which not every dialect does. When the provider refuses the request because a response is
        already running, the item is deleted after the next response instead.
        """
        match event_type:
            case "response.created":
                self._speaking_announcements += self._requested_announcements
                self._requested_announcements = []
            case "response.done":
                for item_id in self._speaking_announcements:
                    await self._open_wire().send({"type": "conversation.item.delete", "item_id": item_id})
                self._speaking_announcements = []
            case _:
                pass

    async def _follow_responses(self, raw: Mapping[str, Any]) -> bool:
        """Track whether a response is active, and send the reply due to function outputs once it is done.

        Returns True when the response that just ended was cancelled and the due reply was dropped
        instead of sent. An ``error`` is taken to refuse the ``response.create`` still waiting to
        start, if any, as the session's floor does.
        """
        match raw.get("type"):
            case "response.created":
                self._response_requested = False
                self._response_running = True
            case "response.done":
                self._response_running = False
                if self._reply_due:
                    self._reply_due = False
                    response = raw.get("response")
                    if isinstance(response, Mapping) and response.get("status") == "cancelled":
                        logger.info(
                            "%s: dropped the reply to a cancelled response's function call", self.name
                        )
                        return True
                    self._response_requested = True
                    await self._open_wire().send({"type": "response.create"})
            case "error":
                self._response_requested = False
            case _:
                pass
        return False

    def _map_event(self, raw: Mapping[str, Any]) -> ProviderEvent | None:
        """Translate one server event, or return None for a type this adapter ignores.

        Raises ``KeyError``, ``TypeError``, or ``ValueError`` when a mapped event lacks a field or holds
        one of the wrong kind.
        """
        match raw.get("type"):
            case "input_audio_buffer.speech_started":
                return SpeechStarted()
            case "input_audio_buffer.speech_stopped":
                return SpeechStopped()
            case "response.created":
                return ResponseStarted()
            case "response.output_audio.delta":
                return OutputAudio(pcm=base64.b64decode(raw["delta"], validate=True), item_id=raw["item_id"])
            case "response.output_audio_transcript.delta":
                return OutputTranscript(delta=raw["delta"])
            case "conversation.item.input_audio_transcription.delta":
                delta = raw.get("delta") or ""
                item_id = raw["item_id"]
                self._input_transcripts[item_id] = self._input_transcripts.get(item_id, "") + delta
                return InputTranscript(delta=delta, final=False)
            case "conversation.item.input_audio_transcription.completed":
                return InputTranscript(
                    delta=self._transcript_rest(raw["item_id"], raw["transcript"]), final=True
                )
            case "response.function_call_arguments.done":
                arguments = json.loads(raw["arguments"] or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError(f"function arguments are not a JSON object: {raw['arguments']!r}")
                name = raw["name"]
                logger.info("%s function call %s: %s", self.name, raw["call_id"], name)
                offset_ms = self._input_samples * 1000 // self.capabilities["input_rate"]
                return DelegationRequested(
                    id=raw["call_id"], offset_ms=offset_ms, target=name, tool_args=arguments
                )
            case "response.done":
                return OutputDone()
            case "error":
                error = raw.get("error")
                code = error.get("code") if isinstance(error, Mapping) else None
                return ProviderError(message=_error_message(raw), code=None if code is None else str(code))
            case _:
                return None

    def _transcript_rest(self, item_id: str, transcript: str) -> str:
        """The part of ``item_id``'s completed ``transcript`` its deltas have not already delivered.

        When the completed text revises what the deltas said, the revision is dropped: the deltas were
        already passed on, and repeating the whole utterance would duplicate it.
        """
        streamed = self._input_transcripts.pop(item_id, "")
        if transcript.startswith(streamed):
            return transcript[len(streamed) :]
        logger.debug("Input transcript %s was revised on completion; kept the streamed text", item_id)
        return ""

    def _open_wire(self) -> WireClient:
        if self._wire is None:
            raise RuntimeError(f"{type(self).__name__}.connect() has not succeeded yet")
        return self._wire


def _system_message(text: str) -> dict[str, Any]:
    """A system message conversation item holding ``text``."""
    return {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}


async def _wait_for_update(incoming: AsyncIterator[Mapping[str, Any]]) -> None:
    """Consume server events up to ``session.updated``.

    Raises ``WireError`` on an ``error`` event or when the connection ends first.
    """
    async for raw in incoming:
        match raw.get("type"):
            case "session.updated":
                return
            case "error":
                message = _error_message(raw)
                logger.warning("The voice provider rejected the session: %s", message)
                raise WireError(f"the voice provider rejected the session: {message}")
            case _:
                pass  # session.created and pings come first
    raise WireError("the voice provider closed the connection before the session started")


def _error_message(raw: Mapping[str, Any]) -> str:
    """The human-readable message of an ``error`` event, or "unknown error" when it has none."""
    error = raw.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    return "unknown error" if message is None else str(message)
