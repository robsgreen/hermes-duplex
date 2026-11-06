"""The GPT-Live adapter: OpenAI's Live API over a primary WebSocket, with client delegation.

The model hands work to this app with ``session.delegation.created``, which carries an id and a
timeline offset but no task text; the session reconstructs the request from the transcripts. Progress
goes back as silent ``session.thinking.append`` and results as spoken ``session.commentary.append``,
both tied to the delegation id. GPT-Live sends no speech-started or response-done events and no item
ids, so the session detects barge-in itself.

Event shapes follow the openai 3.42.1 SDK (``resources/live``, ``types/live/*``), which this package
does not depend on.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Self

from ...config import ConfigError, ProviderConfig, Secrets
from ...prompts.base import PromptContext, VoicePrompter, fit_text, split_text
from ..base import (
    Closed,
    DelegationRef,
    DelegationRequested,
    InputTranscript,
    OutputAudio,
    OutputTranscript,
    ProviderCapabilities,
    ProviderError,
    ProviderEvent,
    SessionReady,
)
from ..wire import CONNECT_TIMEOUT_S, WireClient, WireError
from .prompt import GptLivePrompter

logger = logging.getLogger(__name__)

GPT_LIVE_URL = "wss://api.openai.com/v1/live/sessions"
DEFAULT_MODEL = "gpt-live-1"
DEFAULT_VOICE = "marin"
SAMPLE_RATE = 24000
CONTENT_CAP_TOKENS = 500
"""GPT-Live's limit on the content of one thinking or commentary append."""

APPEND_MAX_CHARS = CONTENT_CAP_TOKENS
"""The most characters one append carries: one per token, a conservative bound that holds for
numbers, symbols, and CJK text too, unlike the 2.5 characters per token estimate results are cut to
(``CHARS_PER_TOKEN``). Longer commentary is split across appends."""

CAPABILITIES: ProviderCapabilities = {
    "input_rate": SAMPLE_RATE,
    "output_rate": SAMPLE_RATE,
    "supports_truncate": False,
    "supports_out_of_band_response": False,
    "emits_speech_events": False,
    "delegation_style": "live",
    "max_session_s": None,
    "replaces_instructions": False,
}


class GptLiveProvider:
    """A ``RealtimeProvider`` for GPT-Live. Each ``connect`` opens a new Live session."""

    name = "gpt-live"
    capabilities = CAPABILITIES
    prompter: VoicePrompter = GptLivePrompter()

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        url: str = GPT_LIVE_URL,
        start_timeout_s: float = CONNECT_TIMEOUT_S,
        prompt_context: PromptContext | None = None,
    ) -> None:
        """Prepare a provider that connects to ``url`` with ``api_key`` and runs ``model``.

        ``start_timeout_s`` bounds the wait for ``session.started`` after the WebSocket opens.
        ``prompt_context`` is the deployment its instructions describe; None means the defaults.
        """
        self.prompt_context = prompt_context or PromptContext()
        self._api_key = api_key
        self._model = model
        self._url = url
        self._start_timeout_s = start_timeout_s
        self._wire: WireClient | None = None
        self._incoming: AsyncIterator[dict[str, Any]] | None = None

    @classmethod
    def from_config(cls, config: ProviderConfig, secrets: Secrets, prompt_context: PromptContext) -> Self:
        """Build the provider from the ``provider`` config section; the key comes from ``api_key_env``.

        Raises ``ConfigError`` on ``provider.api_key_env`` when it is unset (config loading already
        rejects that for ``gpt-live``).
        """
        if config.api_key_env is None:
            raise ConfigError("provider.api_key_env", "is required for provider 'gpt-live'")
        return cls(
            secrets.get(config.api_key_env),
            model=config.model or DEFAULT_MODEL,
            prompt_context=prompt_context,
        )

    async def connect(self, instructions: str, voice: str | None, history_summary: str | None) -> None:
        """Start a Live session and return once the server sends ``session.started``.

        Closes any session this provider still has open first. ``voice`` None means ``marin``.
        ``history_summary`` is given to the model as a developer message in the session's initial
        history. Raises ``WireError`` when the WebSocket cannot be opened, or when the server rejects
        the session, ends the connection, or does not start the session within ``start_timeout_s``;
        the connection is closed in every such case.
        """
        await self.close()
        self._wire = self._incoming = None
        session: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "audio": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "output": {"voice": voice or DEFAULT_VOICE},
            },
            "delegation": {"type": "client"},
        }
        if history_summary is not None:
            session["input"] = [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": history_summary}],
                }
            ]

        wire = await WireClient.connect(self._url, self._api_key)
        incoming = wire.events()
        try:
            await wire.send({"type": "session.start", "session": session})
            async with asyncio.timeout(self._start_timeout_s):
                await _wait_for_start(incoming)
        except TimeoutError:
            await wire.close()
            raise WireError("the voice provider did not start the session") from None
        except BaseException:
            await wire.close()
            raise
        self._wire = wire
        self._incoming = incoming
        logger.info("GPT-Live session started (model %s)", self._model)

    async def send_audio(self, pcm16: bytes) -> None:
        """Send mono PCM16LE audio at 24 kHz; dropped once the session has ended."""
        await self._open_wire().send(
            {"type": "session.input_audio.append", "audio": base64.b64encode(pcm16).decode("ascii")}
        )

    async def events(self) -> AsyncIterator[ProviderEvent]:
        """Yield ``SessionReady``, then the session's events, ending with exactly one ``Closed``.

        ``Closed`` carries the ``session.closed`` reason, or the connection's ``close_reason`` when the
        connection ended without one. Server events this adapter does not map, and malformed ones, are
        skipped.
        """
        wire = self._open_wire()
        assert self._incoming is not None
        yield SessionReady()
        async for raw in self._incoming:
            if raw.get("type") == "session.closed":
                reason = str(raw.get("reason", "unknown"))
                logger.info("GPT-Live session closed: %s", reason)
                await wire.close()  # the session is over; nothing more will be sent on it
                yield Closed(reason)
                return
            try:
                event = _map_event(raw)
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("Skipped a malformed GPT-Live %s event: %r", raw.get("type"), error)
                continue
            if event is not None:
                yield event
        yield Closed(wire.close_reason or "connection_lost")

    async def send_function_output(self, call_id: str, payload: Mapping[str, Any]) -> bool:
        """A no-op that returns False: GPT-Live delegates natively and makes no function calls."""
        return False

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """A no-op: GPT-Live has no output items to truncate."""

    async def report_progress(self, ref: DelegationRef, text: str) -> None:
        """Append ``text`` as silent thinking for the delegation, cut to ``APPEND_MAX_CHARS``."""
        await self._append("session.thinking.append", fit_text(text, APPEND_MAX_CHARS), ref.delegation_id)

    async def deliver_result(self, ref: DelegationRef, text: str) -> None:
        """Append ``text`` as commentary for the model to speak for the delegation (``_commentary``)."""
        await self._commentary(text, ref.delegation_id)

    async def say(self, text: str, *, expects_answer: bool = False) -> None:
        """Append ``text`` as commentary for the model to speak, tied to no delegation (``_commentary``).

        Commentary is part of the conversation, so a question (``expects_answer``) needs nothing more."""
        await self._commentary(text, None)

    async def update_instructions(self, text: str) -> None:
        """Send nothing and raise ``NotImplementedError``: GPT-Live cannot replace a session's
        instructions (``replaces_instructions`` is false), so new instructions need a new session."""
        raise NotImplementedError("gpt-live does not support replaces_instructions; reconnect instead")

    async def close(self) -> None:
        """Ask the server to close the session, then close the connection; safe to call more than once."""
        if self._wire is None:
            return
        if self._wire.close_reason is None:
            await self._wire.send({"type": "session.close"})
        await self._wire.close()

    async def _commentary(self, text: str, delegation_id: str | None) -> None:
        """Append ``text`` as commentary in consecutive parts of at most ``APPEND_MAX_CHARS``, cut at
        sentence or word ends (``split_text``), all for ``delegation_id``. The parts join back into
        ``text``, so a framed result's frame opens in the first part and closes in the last."""
        for part in split_text(text, APPEND_MAX_CHARS):
            await self._append("session.commentary.append", part, delegation_id)

    async def _append(self, event_type: str, content: str, delegation_id: str | None) -> None:
        await self._open_wire().send({"type": event_type, "content": content, "delegation_id": delegation_id})

    def _open_wire(self) -> WireClient:
        if self._wire is None:
            raise RuntimeError("GptLiveProvider.connect() has not succeeded yet")
        return self._wire


async def _wait_for_start(incoming: AsyncIterator[Mapping[str, Any]]) -> None:
    """Consume server events up to ``session.started``.

    Raises ``WireError`` on an ``error`` event or when the connection ends first.
    """
    async for raw in incoming:
        match raw.get("type"):
            case "session.started":
                return
            case "error":
                message = _error_message(raw)
                logger.warning("GPT-Live rejected the session: %s", message)
                raise WireError(f"the voice provider rejected the session: {message}")
            case "session.closed":
                break
            case _:
                pass  # pings and info events may come first
    raise WireError("the voice provider closed the connection before the session started")


def _map_event(raw: Mapping[str, Any]) -> ProviderEvent | None:
    """Translate one GPT-Live server event, or return None for a type this adapter ignores.

    Raises ``KeyError``, ``TypeError``, or ``ValueError`` when a mapped event lacks a field or holds
    one of the wrong kind.
    """
    match raw.get("type"):
        case "session.output_audio.delta":
            return OutputAudio(pcm=base64.b64decode(raw["delta"], validate=True), item_id=None)
        case "session.input_transcript.delta":
            # GPT-Live sends no transcript-done event, so no delta completes an utterance.
            return InputTranscript(
                delta=raw["delta"], final=False, start_ms=raw.get("start_ms"), end_ms=raw.get("end_ms")
            )
        case "session.output_transcript.delta":
            return OutputTranscript(
                delta=raw["delta"], start_ms=raw.get("start_ms"), end_ms=raw.get("end_ms")
            )
        case "session.delegation.created":
            delegation = raw["delegation"]
            target = delegation["target"]
            if target == "client":
                logger.info("GPT-Live delegation %s created for target %s", delegation["id"], target)
            else:
                logger.warning(
                    "GPT-Live delegation %s has target %r, expected 'client'", delegation["id"], target
                )
            return DelegationRequested(
                id=delegation["id"], offset_ms=int(raw["offset_ms"]), target=target, tool_args=None
            )
        case "error":
            error = raw.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            return ProviderError(message=_error_message(raw), code=None if code is None else str(code))
        case _:
            return None


def _error_message(raw: Mapping[str, Any]) -> str:
    """The human-readable message of a GPT-Live ``error`` event, or "unknown error" when it has none."""
    error = raw.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    return "unknown error" if message is None else str(message)
