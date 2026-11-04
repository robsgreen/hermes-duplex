"""The interface every voice provider adapter implements, and the normalized events it emits.

The session talks to GPT-Live, the Realtime family, and the fake provider only through this module.
It branches on ``ProviderCapabilities``, never on a provider's name.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from ..prompts.base import PromptContext, VoicePrompter


class ProviderCapabilities(TypedDict):
    """What a provider supports; the session adapts its behavior to these flags."""

    input_rate: int
    """Sample rate in Hz of the mono PCM16 audio the provider accepts, e.g. 24000."""

    output_rate: int
    """Sample rate in Hz of the mono PCM16 audio the provider emits, e.g. 24000."""

    supports_truncate: bool
    """Whether ``RealtimeProvider.truncate`` tells the provider how much of an item was heard."""

    supports_out_of_band_response: bool
    """Whether announcements can be spoken as a response outside the conversation history."""

    emits_speech_events: bool
    """Whether the provider emits ``SpeechStarted``/``SpeechStopped``, ``ResponseStarted``/``OutputDone``,
    and item ids.

    When false, the session detects barge-in itself with a local loudness gate, and cannot tell when
    the model or the people on the call hold the floor.
    """

    delegation_style: Literal["live", "tool"]
    """``"live"``: the provider delegates natively (GPT-Live client delegation). ``"tool"``: the model
    calls the ``DELEGATE_TOOL``, ``TASK_STATUS_TOOL``, ``CANCEL_TASK_TOOL``, ``SWITCH_PROFILE_TOOL``, and
    ``CONFIRM_SWITCH_TOOL`` function tools."""

    max_session_s: int | None
    """Longest session the provider allows, in seconds, before it closes it; None means no limit."""

    replaces_instructions: bool
    """Whether ``RealtimeProvider.update_instructions`` replaces the open session's instructions in place.

    When false, new instructions take effect only through a new provider session (``connect``).
    """


DELEGATE_TOOL = "delegate"
"""Function tool that hands a request to Hermes; its one argument is ``task``."""

DELEGATE_RECEIPT: Mapping[str, str] = {"status": "received"}
"""The function output that answers an accepted ``DELEGATE_TOOL`` call at once, before routing. It
says only that the request arrived, not that a task started: the session has not yet decided where the
request runs. A start notice, or a question about switching profiles, follows as a separate message,
and so do the task's number and result."""

TASK_STATUS_TOOL = "task_status"
"""Function tool that asks how the user's tasks are going; its optional ``task_id`` names one."""

CANCEL_TASK_TOOL = "cancel_task"
"""Function tool that stops one of the user's tasks; its optional ``task_id`` names which."""

SWITCH_PROFILE_TOOL = "switch_profile"
"""Function tool that changes the speaker's active Hermes profile; its ``profile`` argument names the
profile (the model maps a spoken alias to the profile's name)."""

CONFIRM_SWITCH_TOOL = "confirm_switch"
"""Function tool that answers the assistant's question about switching profiles; its boolean ``accept``
is the user's yes or no."""


@dataclass(frozen=True)
class ModelChoice:
    """The model a Hermes run should use.

    Both fields are required: Hermes honors a bare model name only when ``allow_bare_model`` is set.
    """

    provider: str
    model: str


@dataclass(frozen=True)
class DelegationRef:
    """Identifies the delegation a progress update or result belongs to.

    ``delegation_id`` is set for live-style delegation and ``call_id`` for tool-style delegation;
    ``task_id`` is the TaskTracker number of the task the delegation started.
    """

    delegation_id: str | None
    call_id: str | None
    task_id: int


@dataclass(frozen=True)
class SessionReady:
    """The provider session is open and accepts audio."""


@dataclass(frozen=True)
class SpeechStarted:
    """The provider detected that the user started speaking (barge-in when output is playing)."""


@dataclass(frozen=True)
class SpeechStopped:
    """The provider detected that the user stopped speaking."""


@dataclass(frozen=True)
class ResponseStarted:
    """The model started a response: speech, a function call, or both. ``OutputDone`` ends it."""


@dataclass(frozen=True)
class OutputAudio:
    """A chunk of the model's spoken output.

    ``pcm`` is mono PCM16 at ``ProviderCapabilities.output_rate``. ``item_id`` names the output item
    the chunk belongs to, or is None for providers that emit no item ids.
    """

    pcm: bytes
    item_id: str | None


@dataclass(frozen=True)
class OutputDone:
    """The model finished a response, whether it completed, failed, or was interrupted.

    ``reply_dropped`` is true when the response was cancelled (a person cut in) and the provider
    dropped the reply that function outputs sent during it had asked for, instead of letting the
    model talk over the person; the response request ``send_function_output`` reported will not come.
    """

    reply_dropped: bool = False


@dataclass(frozen=True)
class InputTranscript:
    """Transcription of the user's speech.

    ``delta`` is new text; ``final`` is true when it completes the current utterance (always false for
    providers that never mark an utterance complete). ``start_ms`` and ``end_ms`` place the audio the
    text came from on the current provider session's input timeline (milliseconds of input audio sent
    to it), or are None when the provider does not say.
    """

    delta: str
    final: bool
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class OutputTranscript:
    """Transcription of the model's spoken output; ``delta`` is new text.

    ``start_ms`` and ``end_ms`` place it on the same timeline as ``InputTranscript``'s, or are None when
    the provider does not say.
    """

    delta: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class DelegationRequested:
    """The model asked for work to be done, by live delegation or by calling a function tool.

    ``id`` is the provider's delegation id or function call id. ``offset_ms`` is where the request
    happened on the current provider session's input timeline (milliseconds of input audio sent to it),
    or None when the provider does not say (the session then uses the time the event arrived). ``target`` is the
    provider's delegation target (e.g. ``"client"``) or the called tool's name. ``tool_args`` holds the
    parsed function call arguments, or is None for live delegation.
    """

    id: str
    offset_ms: int | None
    target: str
    tool_args: Mapping[str, Any] | None


@dataclass(frozen=True)
class ProviderError:
    """The provider reported an error; ``message`` is its human-readable description.

    ``code`` is the provider's error code when it sends one. An error does not end the session; a
    ``Closed`` event does.
    """

    message: str
    code: str | None = None


@dataclass(frozen=True)
class Closed:
    """The provider session ended; no events follow. ``reason`` says why, for logs."""

    reason: str


ProviderEvent = (
    SessionReady
    | SpeechStarted
    | SpeechStopped
    | ResponseStarted
    | OutputAudio
    | OutputDone
    | InputTranscript
    | OutputTranscript
    | DelegationRequested
    | ProviderError
    | Closed
)


class RealtimeProvider(Protocol):
    """One speech-to-speech provider session.

    The session calls ``connect`` once per provider session (again after a reconnect), then streams
    audio in with ``send_audio`` while consuming ``events``. It writes everything the model reads
    with ``prompter`` from ``prompt_context``.
    """

    name: str
    capabilities: ProviderCapabilities

    prompter: VoicePrompter
    """Writes this provider's instructions, task reports, and reconnect summaries."""

    prompt_context: PromptContext
    """The deployment the instructions describe; Realtime-family adapters also offer its profiles in
    the ``switch_profile`` tool."""

    async def connect(self, instructions: str, voice: str | None, history_summary: str | None) -> None:
        """Open a session with these instructions and voice (None: the provider default).

        ``history_summary`` re-seeds a reconnected session with what was said before, or is None.
        """
        ...

    async def send_audio(self, pcm16: bytes) -> None:
        """Send mono PCM16 audio at ``capabilities["input_rate"]``."""
        ...

    def events(self) -> AsyncIterator[ProviderEvent]:
        """Yield the session's events in order; the iterator ends after ``Closed``."""
        ...

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Tell the provider the user heard only the first ``audio_end_ms`` of ``item_id``.

        A no-op when ``capabilities["supports_truncate"]`` is false.
        """
        ...

    async def send_function_output(self, call_id: str, payload: Mapping[str, Any]) -> bool:
        """Answer the model's function call ``call_id`` with ``payload`` (JSON-serializable), after which
        the model responds. Answer each call once. A no-op for live-style providers, which make no calls.

        Returns whether the output asked the model for a response of its own; False when it shares one
        already due (outputs given while one response runs get one reply after it) or for a no-op.
        """
        ...

    async def report_progress(self, ref: DelegationRef, text: str) -> None:
        """Give the model a progress update for a running delegation; a no-op where unsupported."""
        ...

    async def deliver_result(self, ref: DelegationRef, text: str) -> None:
        """Give the model a delegation's final result (already framed as data) to tell the user."""
        ...

    async def say(self, text: str) -> None:
        """Have the model speak a short notice, such as a refusal or status, with tools disabled."""
        ...

    async def update_instructions(self, text: str) -> None:
        """Replace the open session's instructions with ``text``, keeping its tools, voice, and audio
        settings, and the conversation so far.

        Call it only when ``capabilities["replaces_instructions"]`` is true; otherwise it sends nothing
        and raises ``NotImplementedError``.
        """
        ...

    async def close(self) -> None:
        """Close the session; safe to call more than once."""
        ...
