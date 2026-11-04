"""The voice session: one conversation joined to one provider session, and the tasks it delegates.

``VoiceSession`` pipes audio both ways, consumes the provider's normalized events, and turns each
delegation into a routed, permitted, tracked Hermes run whose progress and result it reports back
to the provider. It adapts to the provider only through ``ProviderCapabilities`` and the
provider's ``prompter``, never through the provider's name.
"""

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .audio.frames import FRAME_MS
from .audio.gate import LoudnessGate
from .audio.mixer import AttributionRing
from .audio.pacer import OutputPacer
from .config import CONTROL_PROFILE, AudioConfig
from .dispatch.hermes_api import HermesApiClient, HermesApiError, RunEvent
from .people import PermissionChecker
from .prompts.base import SENTENCE_END, TaskReport
from .prompts.notices import (
    LOST_TRACK,
    NO_SUCH_TASK,
    NO_TASK_GIVEN,
    NOT_HEARD,
    NOTHING_TO_CANCEL,
    PROVIDER_LOST,
    WHO_IS_ASKING,
    cancel_failed,
    cancelled_while_away,
    failed_while_away,
    finished_while_away,
    queued_behind,
    refusal,
    task_already_ended,
    task_cancelled,
    task_failed,
    task_started,
    task_summary,
    topic,
    unknown_tool,
    which_to_cancel,
)
from .providers.base import (
    CANCEL_TASK_TOOL,
    DELEGATE_RECEIPT,
    DELEGATE_TOOL,
    TASK_STATUS_TOOL,
    Closed,
    DelegationRef,
    DelegationRequested,
    InputTranscript,
    OutputAudio,
    OutputDone,
    OutputTranscript,
    ProviderError,
    RealtimeProvider,
    ResponseStarted,
    SpeechStarted,
    SpeechStopped,
)
from .providers.wire import WireError, retry_connect
from .router.base import RouteDecision
from .tasks import TERMINAL_STATES, Task, TaskState, TaskTracker
from .transports.base import Conversation

logger = logging.getLogger(__name__)

ATTRIBUTION_WINDOW_MS = 4000
"""A request is credited to whoever was voiced most in this many ms of input before it."""

LIVE_CREDIT_AFTER_MS = 500
"""A live delegation also counts who was voiced this many ms of input after its offset."""

TRANSCRIPT_GRACE_MS = 1500
"""A live delegation's words include input transcribed up to this many ms after its offset."""

TRANSCRIPT_LOOKBACK_MS = 30_000
"""A live delegation's words start at the previous delegation, or at most this many ms before it."""

SPLIT_WITHIN_MS = 1000
"""A live delegation followed by another within this many ms gets no grace: its words end at its
offset, and the next one's begin there."""

TRANSCRIPT_KEEP_MS = 60_000
"""How much timed transcript the session keeps, in ms of the input timeline."""

PROGRESS_INTERVAL_S = 5.0
"""At most one progress report per delegation in this many seconds."""

RESULT_CAP_TOKENS = 500
"""Default token cap for a Hermes result as the provider's prompter writes it."""

HISTORY_LINES = 10
"""How many recent transcript lines the session keeps for routing and to re-seed a reconnect."""

LINE_MAX_CHARS = 500
"""A transcript line this long ends there, so a long monologue still yields bounded lines."""

PROVIDER_LOST_REASON = "the voice provider closed and could not be reconnected"
"""Why the session ends a conversation whose provider session ended for good."""

ANNOUNCEMENT_MAX_WAIT_S = 20.0
"""An announcement waits at most this long for the floor, then is spoken anyway, so a missed
``SpeechStopped`` cannot hold announcements back for good."""

SHORT_PROVIDER_SESSION_S = 5.0
"""A provider session that ends sooner than this after it opened counts as short-lived."""

MAX_SHORT_PROVIDER_SESSIONS = 3
"""After this many short-lived provider sessions in a row the session stops reconnecting: a provider
that accepts the connection and drops it at once would otherwise be reconnected forever."""

_NAMED_TASK = re.compile(r"\btask\s+(?:number\s+|#\s*)?(\d+)\b", re.IGNORECASE)
"""A task named by its number: "task 2", "task number 2", "task #2". Accepted, never advertised."""

_WORD = re.compile(r"[a-z0-9]+")
"""One word of a request, once lowercased; apostrophes and other punctuation split words."""

_FILLER_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "be",
        "can",
        "cancel",
        "check",
        "could",
        "do",
        "does",
        "doing",
        "for",
        "going",
        "has",
        "hey",
        "how",
        "i",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "one",
        "please",
        "question",
        "request",
        "so",
        "status",
        "still",
        "stop",
        "task",
        "tell",
        "that",
        "the",
        "thing",
        "this",
        "to",
        "want",
        "was",
        "what",
        "which",
        "you",
        "your",
        "d",
        "ll",
        "m",
        "re",
        "s",
        "t",
        "ve",
    }
)
"""Words that say nothing about a task's topic: the ones status and cancel requests are made of, and
the ends of contractions ("what's", "don't")."""


class Router(Protocol):
    """What the session needs from the router (``CompositeRouter``, configured before use)."""

    async def route(
        self,
        text: str,
        speaker: str,
        *,
        history: Sequence[str] = (),
        hint: str | None = None,
        permitted: Callable[[str], bool] | None = None,
    ) -> RouteDecision: ...


@dataclass(frozen=True)
class _TimedText:
    start_ms: int
    text: str


class _TimedTranscript:
    """Transcript deltas placed on the input timeline, so a span of it can be read back as text.

    Keeps the deltas that start within ``TRANSCRIPT_KEEP_MS`` of the latest one.
    """

    def __init__(self) -> None:
        self._deltas: deque[_TimedText] = deque()

    def add(self, start_ms: int, delta: str) -> None:
        """Remember ``delta``, whose audio started at ``start_ms``."""
        self._deltas.append(_TimedText(start_ms, delta))
        while self._deltas[0].start_ms < start_ms - TRANSCRIPT_KEEP_MS:
            self._deltas.popleft()

    def text(self, start_ms: int, end_ms: int) -> str:
        """The deltas that start in [start_ms, end_ms), in arrival order, with whitespace normalized."""
        joined = "".join(d.text for d in self._deltas if start_ms <= d.start_ms < end_ms)
        return " ".join(joined.split())


class _Floor:
    """Who holds the floor of the call, as far as the provider's speech and response events tell.

    The model holds it from the moment a response is due (the session asked for one, or a person's
    turn just ended) or it starts one on its own, until that response is done; a person holds it from
    ``SpeechStarted`` to ``SpeechStopped``.
    A response still running when a person starts speaking counts as interrupted: the provider cancels
    it, and a function call it delivers before it is done is stale. Counts never go below zero, so a
    stray or missed event cannot wedge the floor for longer than until the next matching one.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget everything; a new provider session starts with a free floor."""
        self._speaking = False
        self._requested = 0
        """Responses the session asked for that have not started yet."""
        self._running = 0
        self._interrupted = 0
        """How many of the running responses a person interrupted."""

    @property
    def free(self) -> bool:
        """Whether nobody is speaking and the model has no response requested or running."""
        return not self._speaking and self._requested == 0 and self._running == 0

    @property
    def takes_calls(self) -> bool:
        """Whether a function call arriving now can be acted on: no interrupted response is running."""
        return self._interrupted == 0

    def response_requested(self) -> None:
        """The session asked the model for a response (an announcement, or after a function output)."""
        self._requested += 1

    def response_shared(self) -> None:
        """A function output counted by ``response_requested`` shares a response already due instead."""
        self._requested = max(0, self._requested - 1)

    def response_refused(self) -> None:
        """A response the session asked for will not come: the provider reported an error (taken to
        refuse the request) or dropped the reply due after a cancelled response."""
        self._requested = max(0, self._requested - 1)

    def response_started(self) -> None:
        self._requested = max(0, self._requested - 1)
        self._running += 1

    def response_done(self) -> None:
        self._running = max(0, self._running - 1)
        self._interrupted = min(max(0, self._interrupted - 1), self._running)

    def speech_started(self) -> None:
        self._speaking = True
        self._interrupted = self._running

    def speech_stopped(self) -> None:
        """A person stopped speaking; the provider's turn detection answers the turn with a response."""
        self._speaking = False
        self.response_requested()


@dataclass(frozen=True)
class _Announcement:
    """Something for the model to speak: a delegation's result, or a notice."""

    text: str
    ref: DelegationRef | None
    """The delegation ``text`` is the result of, or None for a notice."""


@dataclass(frozen=True)
class _Ending:
    """How a task ended, and the two ways to tell it."""

    state: TaskState
    notice: str
    """What the people in the conversation that followed the task to its end are told."""

    away_notice: str
    """What its person is told in a later conversation ("While you were away, …")."""


class VoiceSession:
    """Runs one conversation against one provider session until either ends.

    Input frames go to the provider, and each frame's voiced people are remembered to credit
    requests. Output audio is paced to the conversation. Each delegation the provider requests is
    credited to a person, routed, checked against that person's profiles, and run on Hermes; tasks
    on different profiles run concurrently. The session reports the task's start (or its place in
    the queue), throttled progress, and its result or failure back to the provider. ``_control``
    requests are answered locally from the ``TaskTracker`` and never reach Hermes. The model's
    instructions, each result, and the reconnect summary come from the provider's ``prompter``.

    Tool-style providers get exactly one function output per call. A ``delegate`` call is answered
    at once with ``DELEGATE_RECEIPT``, before routing, so the model can go on talking; the spoken
    start notice follows once the task is routed and permitted. ``task_status`` and ``cancel_task``
    calls are answered from the ``TaskTracker`` in the function output itself, and name the task by
    ``task_id`` or by the user's words (``about``); see ``_resolve_task``. People hear a task only by
    its topic (``notices.topic``); its number is an internal id. A call nobody can be credited with is
    answered with ``"not_started"`` and a question for the model to ask.

    Results and notices are announcements, spoken one at a time in the order they arise. When the
    provider emits speech events, each waits for the floor: the model idle, playback drained, and
    nobody speaking, for at most ``ANNOUNCEMENT_MAX_WAIT_S`` on ``clock``. When someone starts speaking
    over the model's output, the session flushes the output at once, tells the provider how much of
    the interrupted item was heard (where it supports truncation), and ignores function calls from the
    interrupted response.

    A live delegation carries no words, so the session takes them from the input transcript around
    the delegation's offset, once the input has passed it far enough to credit the speaker and catch
    late words. When the provider emits no speech events, the session detects barge-in itself: a
    person on ``conversation``'s input louder than ``gate_threshold_dbfs`` (dBFS) who starts talking
    while output is held or playing flushes that output at once.

    When the provider session ends on its own (it expired, dropped, or was closed by the provider),
    the session reconnects with backoff and re-seeds the new provider session with the latest
    transcript lines and the open tasks. Input that arrives while reconnecting is not sent. Each
    provider session has its own input timeline, starting at 0 when it connects, so speaker credit
    and transcript windows start afresh. Tasks keep running across a reconnect and report to the new
    provider session; one requested by an earlier provider session has its result spoken as a notice,
    since only the session that requested a delegation can answer it. When reconnecting fails, the
    session tells the conversation's user interface and closes the conversation. It does the same
    after ``MAX_SHORT_PROVIDER_SESSIONS`` provider sessions in a row each ended within
    ``SHORT_PROVIDER_SESSION_S`` of opening.

    Tasks outlive their conversation. When the session ends, its open tasks are left unattended in
    the ``TaskTracker`` (their Hermes runs go on), and each task whose end it had not announced yet is
    left untold. The session adopts a person's unattended and untold tasks when it first hears that
    person and whenever another session leaves some for a person it has heard, and follows each to its
    end like its own. An end the person missed is announced as news ("While you were away, "how is my
    portfolio doing" finished: …"); so is the end of an adopted run whose status could not be read.

    ``clock`` returns seconds and times the progress throttle and the announcements' wait for the
    floor. ``sleep`` is how the session waits between reconnect attempts (tests pass a recorder).
    """

    def __init__(
        self,
        conversation: Conversation,
        provider: RealtimeProvider,
        *,
        router: Router,
        hermes: HermesApiClient,
        tracker: TaskTracker,
        permissions: PermissionChecker,
        voice: str | None = None,
        pacer_lead_ms: int = 200,
        gate_threshold_dbfs: float = AudioConfig.gate_threshold_dbfs,
        result_cap_tokens: int = RESULT_CAP_TOKENS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._conversation = conversation
        self._provider = provider
        self._router = router
        self._hermes = hermes
        self._tracker = tracker
        self._permissions = permissions
        self._voice = voice
        self._result_cap_tokens = result_cap_tokens
        self._clock = clock
        self._sleep = sleep

        self._capabilities = provider.capabilities
        self._pacer = OutputPacer(self._capabilities["output_rate"], lead_ms=pacer_lead_ms)
        self._attribution = AttributionRing()
        self._gate = None if self._capabilities["emits_speech_events"] else LoudnessGate(gate_threshold_dbfs)
        self._provider_open = asyncio.Event()
        """Set while a provider session is open; clear while connecting or reconnecting."""
        self._connection = 0
        """Counts provider sessions opened, so work begun on an earlier one can tell it ended."""
        self._sent_ms = 0
        """Milliseconds of input sent to the current provider session: its input timeline's now."""
        self._input_advanced = asyncio.Condition()
        """Notified whenever ``_sent_ms`` or ``_connection`` moves on."""
        self._input_transcript = _TimedTranscript()
        self._output_transcript = _TimedTranscript()
        self._live_offsets: deque[int] = deque(maxlen=32)
        """Offsets of the latest live delegations, to split their transcript windows."""
        self._delegation_ids: set[str] = set()
        """Ids of the delegations the current provider session requested; only it can answer them."""
        self._history: deque[tuple[Literal["user", "assistant"], str]] = deque(maxlen=HISTORY_LINES)
        """The latest transcript lines as (speaker, text), oldest first."""
        self._input_line = ""
        self._output_line = ""
        self._task_numbers: list[int] = []
        """Numbers of the tasks this session follows: the ones it started and the ones it adopted."""
        self._people_heard: set[str] = set()
        """People voiced in this session so far, whose unattended and untold tasks it adopts."""
        self._untold: dict[int, str] = {}
        """Per task whose end is queued to be announced but not spoken yet, its ``away_notice``."""
        self._delegations: set[asyncio.Task[None]] = set()
        self._floor = _Floor()
        self._announcements: asyncio.Queue[_Announcement] = asyncio.Queue()
        self._opened_s = 0.0
        """``time.monotonic()`` when the current provider session opened."""

    async def run(self) -> None:
        """Connect the provider and run until the conversation or the provider session ends.

        On the way out, leaves this session's tasks unattended (``TaskTracker.leave_unattended``:
        their Hermes runs continue until a later session adopts them, tasks queued without a run id
        become ``unknown``, and ends not spoken yet are left untold), closes the provider, and closes
        the conversation when the provider ended and could not be reconnected. Cancelling ``run``
        does the same. Raises ``WireError`` when the first connect fails.
        """
        pumps: tuple[asyncio.Task[None], ...] = ()
        try:
            await self._connect(None)
            events = asyncio.create_task(self._handle_events())
            pumps = (
                asyncio.create_task(self._pump_input()),
                events,
                asyncio.create_task(self._pump_output()),
                asyncio.create_task(self._pump_announcements()),
                asyncio.create_task(self._pump_adoptions()),
            )
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for pump in done:
                pump.result()  # re-raises a pump's error
            if events in done:
                await self._conversation.close(PROVIDER_LOST_REASON)
        finally:
            for task in (*pumps, *self._delegations):
                task.cancel()
            await asyncio.gather(*pumps, *self._delegations, return_exceptions=True)
            await self._tracker.leave_unattended(self._task_numbers, self._untold)
            await self._provider.close()

    async def _connect(self, summary: str | None) -> None:
        """Open a provider session re-seeded with ``summary`` (None: a new call) on a fresh timeline.

        Everything placed on the previous provider session's timeline is forgotten, and work waiting
        for that timeline to reach a point is woken to find it ended.
        """
        await self._provider.connect(
            self._provider.prompter.instructions(self._provider.prompt_context), self._voice, summary
        )
        async with self._input_advanced:
            self._connection += 1
            self._sent_ms = 0
            self._input_advanced.notify_all()
        self._attribution.clear()
        self._input_transcript = _TimedTranscript()
        self._output_transcript = _TimedTranscript()
        self._live_offsets.clear()
        self._delegation_ids.clear()
        self._floor.reset()
        self._opened_s = time.monotonic()
        self._provider_open.set()

    async def _reconnect(self) -> bool:
        """Replace the provider session that just ended; return False when that keeps failing.

        Retries with backoff (``retry_connect``).
        """
        self._provider_open.clear()
        await self._end_input_line()
        await self._end_output_line()
        open_tasks = [
            task
            for number in self._task_numbers
            if (task := self._tracker.get(number)) and task.state not in TERMINAL_STATES
        ]
        summary = self._provider.prompter.reseed(self._history, open_tasks)
        try:
            await retry_connect(lambda: self._connect(summary), sleep=self._sleep)
        except WireError as error:
            logger.error("could not reconnect the voice provider: %s", error.reason)
            return False
        logger.info("reconnected the voice provider")
        return True

    async def _pump_input(self) -> None:
        input_rate = self._capabilities["input_rate"]
        async for frame in self._conversation.frames():
            if frame.rate != input_rate:
                raise ValueError(f"input frames are {frame.rate} Hz, the provider takes {input_rate} Hz")
            if self._gate is not None:
                # The transport gives only the mix, so each voiced person is gated on its level.
                talking = self._gate.trip(dict.fromkeys(frame.voiced_speakers, frame.pcm16_mono))
                if talking and self._output_pending():
                    await self._barge_in()
            for person in frame.voiced_speakers - self._people_heard:
                self._people_heard.add(person)
                self._adopt_tasks(person)
            if not self._provider_open.is_set():
                continue  # reconnecting: the new provider session starts with the input after it opens
            # Placed on the provider session's own timeline: the transport's t_ms never restarts.
            connection, t_ms = self._connection, self._sent_ms
            self._attribution.record(t_ms, frame.voiced_speakers)
            await self._provider.send_audio(frame.pcm16_mono)
            async with self._input_advanced:
                if connection == self._connection:
                    self._sent_ms = t_ms + FRAME_MS
                    self._input_advanced.notify_all()

    async def _pump_adoptions(self) -> None:
        """Adopt the tasks other sessions leave unattended or untold for the people heard here."""
        handovers = 0
        while True:
            handovers = await self._tracker.unattended_after(handovers)
            for person in self._people_heard:
                self._adopt_tasks(person)

    def _adopt_tasks(self, person: str) -> None:
        """Take over ``person``'s unattended tasks and follow each in the background, and announce how
        their untold tasks ended."""
        adoption = self._tracker.adopt(person)
        for task in adoption.open_tasks:
            logger.info("adopted task %d of %s (Hermes run %s)", task.number, person, task.run_id)
            self._task_numbers.append(task.number)
            self._spawn(self._follow_adopted(task))
        for task, announcement in adoption.untold:
            logger.info("adopted the untold end of task %d of %s", task.number, person)
            self._task_numbers.append(task.number)
            self._announce_end(task.number, announcement, announcement, _ref(task))

    def _output_pending(self) -> bool:
        """Whether output audio is held here or still playing at the conversation."""
        return self._pacer.held_ms > 0 or self._pacer.ahead_ms > 0

    async def _barge_in(self) -> None:
        """Discard the output the user has not heard yet; the provider decides whether to go on.

        Where the provider supports it, the interrupted item is truncated to what the user heard, as
        the pacer counts it: the conversation's own count spans its playback, not the provider's items.
        """
        item_id, heard_ms = self._pacer.item_id, round(self._pacer.item_played_ms)
        # Drop before flushing: the output pump must not release held audio while the flush runs.
        dropped_ms = self._pacer.drop()
        await self._conversation.flush_playback()
        logger.info(
            "barge-in: flushed playback after %d ms of the item, dropped %.0f ms held", heard_ms, dropped_ms
        )
        if item_id is not None and self._capabilities["supports_truncate"]:
            await self._provider.truncate(item_id, heard_ms)

    async def _input_reached(self, t_ms: int, connection: int) -> bool:
        """Wait until provider session ``connection`` has been sent the input frame starting at
        ``t_ms``; return False instead once that provider session has ended."""
        async with self._input_advanced:
            await self._input_advanced.wait_for(
                lambda: self._sent_ms > t_ms or self._connection != connection
            )
            return self._connection == connection

    async def _pump_output(self) -> None:
        output_rate = self._capabilities["output_rate"]
        while True:
            if pcm := self._pacer.release():
                await self._conversation.play(pcm, output_rate)
            await asyncio.sleep(FRAME_MS / 1000)

    async def _handle_events(self) -> None:
        """Handle provider events, reconnecting each time the provider session ends.

        Returns, after telling the conversation's user interface that the call is ending, when a
        provider session ended and could not be replaced, or when too many in a row were short-lived.
        Every end seen here is one the session did not ask for: it closes the provider only after
        cancelling this handler. A dropped connection ends the provider session like any other close.
        """
        # A provider without speech events marks no utterance ends either, so turn-taking ends lines.
        lines_end_on_turns = not self._capabilities["emits_speech_events"]
        short_sessions = 0
        while True:
            async for event in self._provider.events():
                match event:
                    case SpeechStarted():
                        self._floor.speech_started()
                        if self._output_pending():
                            await self._barge_in()
                    case SpeechStopped():
                        self._floor.speech_stopped()
                    case ResponseStarted():
                        self._floor.response_started()
                    case OutputAudio(pcm=pcm, item_id=item_id):
                        self._pacer.push(pcm, item_id)
                    case InputTranscript(delta=delta, final=final, start_ms=start_ms):
                        self._input_transcript.add(self._sent_ms if start_ms is None else start_ms, delta)
                        if final or lines_end_on_turns:
                            await self._end_output_line()
                        self._input_line += delta
                        if final or len(self._input_line) >= LINE_MAX_CHARS:
                            await self._end_input_line()
                    case OutputTranscript(delta=delta, start_ms=start_ms):
                        self._output_transcript.add(self._sent_ms if start_ms is None else start_ms, delta)
                        if lines_end_on_turns:
                            await self._end_input_line()
                        self._output_line += delta
                        if len(self._output_line) >= LINE_MAX_CHARS:
                            await self._end_output_line()
                    case OutputDone(reply_dropped=reply_dropped):
                        self._floor.response_done()
                        if reply_dropped:
                            self._floor.response_refused()
                        await self._end_output_line()
                    case DelegationRequested() if not self._floor.takes_calls:
                        logger.info(
                            "ignored call %s (%s): its response was interrupted", event.id, event.target
                        )
                    case DelegationRequested():
                        self._on_delegation(event)
                    case ProviderError(message=message, code=code):
                        self._floor.response_refused()
                        logger.warning("voice provider error %s: %s", code, message)
                    case Closed(reason=reason):
                        logger.info("voice provider session closed: %s", reason)
                    case _:
                        pass  # SessionReady needs no handling
            short_sessions = short_sessions + 1 if self._ended_soon() else 0
            if short_sessions >= MAX_SHORT_PROVIDER_SESSIONS:
                logger.error(
                    "the voice provider ended %d sessions in a row within %g s of opening; giving up",
                    short_sessions,
                    SHORT_PROVIDER_SESSION_S,
                )
                break
            if not await self._reconnect():
                break
        # The conversation has no voice of its own to say it with.
        await self._conversation.notify({"transcript": {"speaker": "assistant", "text": PROVIDER_LOST}})

    def _ended_soon(self) -> bool:
        """Whether the provider session that just ended was short-lived."""
        return time.monotonic() - self._opened_s < SHORT_PROVIDER_SESSION_S

    async def _end_input_line(self) -> None:
        """Record the user's speech since their last line as one transcript line."""
        line, self._input_line = self._input_line, ""
        await self._add_transcript_line("user", line)

    async def _end_output_line(self) -> None:
        """Record the model's output since its last line as one transcript line."""
        line, self._output_line = self._output_line, ""
        await self._add_transcript_line("assistant", line)

    async def _add_transcript_line(self, speaker: Literal["user", "assistant"], text: str) -> None:
        text = text.strip()
        if text:
            self._history.append((speaker, text))
            await self._conversation.notify({"transcript": {"speaker": speaker, "text": text}})

    def _on_delegation(self, request: DelegationRequested) -> None:
        """Credit a tool call to a person now, at arrival, and handle the rest in the background.

        A live delegation (no ``tool_args``) is credited and worded later, by ``_delegate_live``.
        """
        self._delegation_ids.add(request.id)
        anchor_ms = request.offset_ms if request.offset_ms is not None else self._sent_ms
        if request.tool_args is None:
            self._live_offsets.append(anchor_ms)
            self._spawn(self._delegate_live(request, anchor_ms, self._connection))
            return
        speaker = self._attribution.credit(anchor_ms - ATTRIBUTION_WINDOW_MS, anchor_ms)
        self._spawn(self._handle_tool_call(request, request.tool_args, speaker))

    async def _handle_tool_call(
        self, request: DelegationRequested, args: Mapping[str, Any], speaker: str | None
    ) -> None:
        """Act on the function call ``request`` with arguments ``args``, credited to ``speaker`` (None:
        nobody was voiced before it), and answer it with exactly one function output."""
        if speaker is None:
            logger.info("call %s: nobody was voiced before it; asking who it was", request.id)
            await self._answer(request, {"status": "not_started", "message": WHO_IS_ASKING})
            return
        task_id = args.get("task_id")
        number = task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None
        about = args.get("about")
        words = about if isinstance(about, str) else ""
        if request.target == DELEGATE_TOOL:
            text = args.get("task")
            if not isinstance(text, str) or not text.strip():
                logger.warning("delegation %s carries no task text", request.id)
                await self._answer(request, {"status": "not_started", "message": NO_TASK_GIVEN})
                return
            await self._answer(request, DELEGATE_RECEIPT)
            hint = args.get("profile_hint")
            await self._delegate(request, text.strip(), speaker, hint if isinstance(hint, str) else None)
        elif request.target == TASK_STATUS_TOOL:
            message = self._status(speaker, words, number)
            tasks = [_tool_entry(task) for task in self._tracker.active_for(speaker)]
            await self._answer(request, {"status": "ok", "message": message, "tasks": tasks})
        elif request.target == CANCEL_TASK_TOOL:
            await self._answer(
                request, {"status": "ok", "message": await self._cancel(speaker, words, number)}
            )
        else:
            logger.warning("call %s is to the unknown tool %r", request.id, request.target)
            await self._answer(request, {"status": "not_started", "message": unknown_tool(request.target)})

    async def _answer(self, request: DelegationRequested, payload: Mapping[str, Any]) -> None:
        """Send ``payload`` as the function output for the call ``request``, after which the model
        responds; dropped when the provider session that made the call has ended."""
        if not self._provider_open.is_set() or request.id not in self._delegation_ids:
            logger.info("call %s was made by an earlier provider session; not answered", request.id)
            return
        # Counted before sending, so the reply's events cannot arrive ahead of the count.
        self._floor.response_requested()
        if not await self._provider.send_function_output(request.id, payload):
            self._floor.response_shared()

    async def _delegate_live(self, request: DelegationRequested, offset_ms: int, connection: int) -> None:
        """Handle a live delegation at ``offset_ms`` on the input timeline of provider session
        ``connection``.

        Credits whoever was voiced most from ``ATTRIBUTION_WINDOW_MS`` before the offset to
        ``LIVE_CREDIT_AFTER_MS`` after it, once the input has passed that point. The words are the
        input transcript of the delegation's window, read once the input has passed the grace, with
        the assistant's last sentence before the offset as context. When the provider session ends
        before then, what was heard is gone, so the session asks the people to say it again.
        """
        if not await self._input_reached(offset_ms + LIVE_CREDIT_AFTER_MS, connection):
            self._announce(NOT_HEARD)
            return
        speaker = self._attribution.credit(
            offset_ms - ATTRIBUTION_WINDOW_MS, offset_ms + LIVE_CREDIT_AFTER_MS
        )
        if speaker is None:
            logger.info("delegation %s: nobody was voiced around it; asking who it was", request.id)
            self._announce(WHO_IS_ASKING)
            return

        if not await self._input_reached(offset_ms + TRANSCRIPT_GRACE_MS, connection):
            self._announce(NOT_HEARD)
            return
        text = self._input_transcript.text(*self._transcript_window(offset_ms))
        if not text:
            logger.warning("delegation %s: no input transcript around it; asking again", request.id)
            self._announce(NOT_HEARD)
            return
        said = self._output_transcript.text(offset_ms - TRANSCRIPT_LOOKBACK_MS, offset_ms)
        context = SENTENCE_END.split(said)[-1] if said else None
        await self._delegate(request, text, speaker, None, context)

    def _transcript_window(self, offset_ms: int) -> tuple[int, int]:
        """The span [start_ms, end_ms) of input transcript that words the live delegation at ``offset_ms``.

        It starts at the previous delegation's offset, or ``TRANSCRIPT_LOOKBACK_MS`` before, whichever
        is later, and ends ``TRANSCRIPT_GRACE_MS`` after the offset, or at the offset when the next
        delegation follows within ``SPLIT_WITHIN_MS``.
        """
        earlier = [o for o in self._live_offsets if o < offset_ms]
        start_ms = max([offset_ms - TRANSCRIPT_LOOKBACK_MS, *earlier])
        split = any(0 < o - offset_ms < SPLIT_WITHIN_MS for o in self._live_offsets)
        return start_ms, offset_ms if split else offset_ms + TRANSCRIPT_GRACE_MS

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._delegations.add(task)
        task.add_done_callback(self._delegation_done)

    def _delegation_done(self, task: asyncio.Task[None]) -> None:
        self._delegations.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("handling a delegation failed", exc_info=error)

    async def _delegate(
        self,
        request: DelegationRequested,
        text: str,
        speaker: str,
        hint: str | None,
        context: str | None = None,
    ) -> None:
        """Route ``text``, check the speaker may use the profile, and run it as a task.

        ``context`` is what the assistant said just before the request, or None; the router sees it
        as the latest history line and Hermes gets it after the request.
        """
        lines = [text for _, text in self._history]
        history = (*lines, context) if context else tuple(lines)
        person = self._permissions.by_name(speaker)

        def permitted(profile: str) -> bool:
            return person is not None and self._permissions.allowed(person, profile)

        decision = await self._router.route(text, speaker, history=history, hint=hint, permitted=permitted)
        if decision.profile == CONTROL_PROFILE:
            if decision.control_intent == "cancel":
                self._announce(await self._cancel(speaker, text))
            else:
                self._announce(self._status(speaker, text))
            return

        if not permitted(decision.profile):
            logger.warning("refused a task for %s: not allowed to use profile %s", speaker, decision.profile)
            self._announce(refusal(decision.profile))
            return

        live = self._capabilities["delegation_style"] == "live"
        task = await self._tracker.create(
            person=speaker,
            profile=decision.profile,
            effort=decision.effort,
            model=decision.model,
            text=text,
            delegation_id=request.id if live else None,
            call_id=None if live else request.id,
        )
        self._task_numbers.append(task.number)
        ref = _ref(task)
        await self._run_task(task, ref, _with_context(text, context))

    async def _run_task(self, task: Task, ref: DelegationRef, hermes_input: str) -> None:
        """Start ``task`` on Hermes with ``hermes_input``, announce it, and follow it to its end."""
        try:
            start = await self._hermes.start_run(
                task.profile,
                hermes_input,
                f"duplex-{task.person}-{task.profile}",
                task.model,
                task.effort,
                task.uuid,
            )
        except HermesApiError as error:
            await self._end(task.number, _failure(task, error.reason), ref, away=False)
            return

        await self._transition(task.number, "queued" if start.queued else "running", run_id=start.run_id)
        ahead = self._tracker.ahead_of(task.number)
        announcement = queued_behind(task, ahead) if ahead else task_started(task)
        if self._capabilities["delegation_style"] == "tool":
            self._announce(announcement)  # the call's receipt said only that it started; this says what
        else:
            await self._report_progress(ref, announcement)
        if start.run_id is None:
            # Hermes queued it without an id: it cannot be followed, and the tracker expires it.
            return
        await self._follow(task, ref, start.run_id)

    async def _follow_adopted(self, task: Task) -> None:
        """Follow ``task``, adopted from an earlier conversation, to its end.

        Reads the run's status first: a run that ended while its person was away is announced as news
        (``finished_while_away`` and the like); one still going is followed as ``_follow`` does. When
        the status cannot be read, the run may have ended meanwhile, so it is followed with its end
        worded as news.
        """
        assert task.run_id is not None, "only tasks with a Hermes run are left unattended"
        await self._notify_tasks()
        ref = _ref(task)
        try:
            ended = await self._hermes.run_status(task.profile, task.run_id)
        except HermesApiError:
            await self._follow(task, ref, task.run_id, away=True)
            return
        if ended is None:
            await self._follow(task, ref, task.run_id)
        else:
            await self._end(task.number, self._ending(task, ended), ref, away=True)

    async def _follow(self, task: Task, ref: DelegationRef, run_id: str, *, away: bool = False) -> None:
        """Follow ``task``'s Hermes run ``run_id`` to its end: its start, throttled progress, and how it
        ended, each reported for ``ref``; ``away`` words the end as news (``_end``)."""
        last_progress_s: float | None = None
        try:
            async for event in self._hermes.events(task.profile, run_id):
                match event.kind:
                    case "started":
                        if self._state(task.number) == "queued":
                            await self._transition(task.number, "running")
                    case "progress" | "message" if event.text:
                        now = self._clock()
                        if last_progress_s is None or now - last_progress_s >= PROGRESS_INTERVAL_S:
                            last_progress_s = now
                            await self._report_progress(ref, event.text)
                    case "completed" | "failed" | "cancelled" | "unknown":
                        await self._end(task.number, self._ending(task, event), ref, away=away)
                    case _:
                        pass  # "queued" and empty progress add nothing to what was announced
        except HermesApiError as error:
            await self._end(task.number, _failure(task, error.reason), ref, away=away)

    def _ending(self, task: Task, event: RunEvent) -> _Ending:
        """How ``task`` ended, as the terminal ``event`` reports it: the prompter's result, or a
        failure or cancel notice."""
        match event.kind:
            case "completed":
                report = TaskReport(
                    task.number, topic(task.text), task.profile, "completed", event.text or ""
                )
                result = self._provider.prompter.result(report, self._result_cap_tokens)
                return _Ending("completed", result, finished_while_away(task, result))
            case "cancelled":
                return _Ending("cancelled", task_cancelled(task), cancelled_while_away(task))
            case "failed":
                return _failure(task, event.text or "Hermes reported an error")
            case _:
                return _Ending("unknown", task_failed(task, LOST_TRACK), failed_while_away(task, LOST_TRACK))

    async def _end(self, number: int, ending: _Ending, ref: DelegationRef, *, away: bool) -> None:
        """Move task ``number`` to ``ending``'s terminal state and announce it for ``ref``: its
        ``away_notice`` when ``away`` (its person may have missed the end), else its ``notice``.

        Does nothing when the task has ended already: a speaker's cancel ends it first and is
        confirmed on its own, and the run's later events must not announce it again. A cancelled end
        that arrives while a speaker's cancel waits for Hermes is recorded without a notice, since
        that cancel confirms it.
        """
        if self._state(number) in TERMINAL_STATES:
            return
        if ending.state == "completed" and self._state(number) == "queued":
            # Hermes may report the end of a queued run without a start; the tracker needs one.
            await self._transition(number, "running")
            if self._state(number) in TERMINAL_STATES:
                return
        await self._transition(number, ending.state)
        if not (ending.state == "cancelled" and self._tracker.stopping(number)):
            self._announce_end(number, ending.away_notice if away else ending.notice, ending.away_notice, ref)

    def _status(self, speaker: str, words: str, task_id: int | None = None) -> str:
        """The spoken status of the task of ``speaker``'s that ``task_id`` or ``words`` names
        (``_resolve_task``), or of all their open tasks when they name none."""
        active = self._tracker.active_for(speaker)
        number = _resolve_task(active, words, task_id)
        if number is None:
            return task_summary(active)
        task = self._tracker.get(number)
        if task is None or task.person != speaker:
            return NO_SUCH_TASK
        return task_summary([task])

    async def _cancel(self, speaker: str, words: str, task_id: int | None = None) -> str:
        """Cancel the task of ``speaker``'s that ``task_id`` or ``words`` names (``_resolve_task``), and
        return the notice that says how it went; when they name none of several open tasks, cancel
        nothing and ask which."""
        active = self._tracker.active_for(speaker)
        number = _resolve_task(active, words, task_id)
        if number is None:
            return which_to_cancel(active) if active else NOTHING_TO_CANCEL
        try:
            task = await self._tracker.cancel(speaker, self._hermes.stop, number)
        except HermesApiError as error:
            return cancel_failed(error.reason)
        if task is None:
            return NOTHING_TO_CANCEL
        await self._notify_tasks()
        return task_cancelled(task) if task.state == "cancelled" else task_already_ended(task)

    def _announce(self, text: str, ref: DelegationRef | None = None) -> None:
        """Queue ``text`` for the model to speak: ``ref``'s result, or a notice when ``ref`` is None.

        Announcements are spoken in order by ``_pump_announcements``.
        """
        self._announcements.put_nowait(_Announcement(text, ref))

    def _announce_end(self, number: int, text: str, away_notice: str, ref: DelegationRef) -> None:
        """Queue ``text``, how task ``number`` ended, for ``ref``; until it is spoken, the task is
        untold, and ``away_notice`` is what a later conversation tells its person instead."""
        self._untold[number] = away_notice
        self._announce(text, ref)

    async def _pump_announcements(self) -> None:
        """Speak each queued announcement once the floor allows, one at a time, to the open provider
        session.

        A result goes to the delegation it answers when the open provider session requested it, and
        is spoken as a notice otherwise. A failure to speak one is logged, and the next one goes on.
        """
        while True:
            announcement = await self._announcements.get()
            await self._wait_for_floor()
            self._floor.response_requested()
            try:
                if announcement.ref is not None and self._requested_here(announcement.ref):
                    await self._provider.deliver_result(announcement.ref, announcement.text)
                else:
                    await self._provider.say(announcement.text)
                if announcement.ref is not None:
                    self._untold.pop(announcement.ref.task_id, None)
            except Exception:
                logger.exception("could not announce %r", announcement.text)

    async def _wait_for_floor(self) -> None:
        """Wait until a provider session is open and, when the provider emits speech events, the floor
        is free: the model idle, playback drained, and nobody speaking.

        Stops waiting for the floor after ``ANNOUNCEMENT_MAX_WAIT_S`` on ``clock``, which it reads only
        while the floor is taken.
        """
        gated = self._capabilities["emits_speech_events"]
        waiting_since_s: float | None = None
        while True:
            await self._provider_open.wait()
            if not gated or (self._floor.free and not self._output_pending()):
                return
            now_s = self._clock()
            if waiting_since_s is None:
                waiting_since_s = now_s
            elif now_s - waiting_since_s >= ANNOUNCEMENT_MAX_WAIT_S:
                logger.warning("the floor stayed taken for %g s; announcing anyway", ANNOUNCEMENT_MAX_WAIT_S)
                return
            await asyncio.sleep(FRAME_MS / 1000)

    async def _report_progress(self, ref: DelegationRef, text: str) -> None:
        """Report progress on ``ref``'s task to the open provider session, tied to the delegation
        only when that session requested it."""
        await self._provider_open.wait()
        if not self._requested_here(ref):
            ref = DelegationRef(delegation_id=None, call_id=None, task_id=ref.task_id)
        await self._provider.report_progress(ref, text)

    def _requested_here(self, ref: DelegationRef) -> bool:
        """Whether the open provider session requested ``ref``'s delegation."""
        return (ref.delegation_id or ref.call_id) in self._delegation_ids

    async def _transition(self, number: int, state: TaskState, *, run_id: str | None = None) -> None:
        await self._tracker.transition(number, state, run_id=run_id)
        await self._notify_tasks()

    def _state(self, number: int) -> TaskState:
        task = self._tracker.get(number)
        assert task is not None, f"task {number} is not tracked"
        return task.state

    async def _notify_tasks(self) -> None:
        """Send this session's task list to the conversation's user interface."""
        tasks = [task for number in self._task_numbers if (task := self._tracker.get(number))]
        await self._conversation.notify(
            {
                "tasks": [
                    {"number": t.number, "profile": t.profile, "state": t.state, "text": t.text}
                    for t in tasks
                ]
            }
        )


def _with_context(text: str, context: str | None) -> str:
    """The Hermes input for the request ``text``, followed by what the assistant said before it."""
    if not context:
        return text
    return f'{text}\n\n(Context: just before this request, the voice assistant said: "{context}")'


def _ref(task: Task) -> DelegationRef:
    """The reference to ``task``'s own delegation."""
    return DelegationRef(delegation_id=task.delegation_id, call_id=task.call_id, task_id=task.number)


def _failure(task: Task, reason: str) -> _Ending:
    """``task`` failed; ``reason`` is a short spoken explanation."""
    return _Ending("failed", task_failed(task, reason), failed_while_away(task, reason))


def _tool_entry(task: Task) -> dict[str, Any]:
    """``task`` as a ``task_status`` function output lists it: its internal ``id`` for later calls,
    and the ``topic``, ``agent``, and ``state`` the model speaks from."""
    return {"id": task.number, "topic": topic(task.text), "agent": task.profile, "state": task.state}


def _resolve_task(active: Sequence[Task], words: str, task_id: int | None = None) -> int | None:
    """The number of the task a status or cancel request means, or None when it cannot tell.

    ``active`` are the speaker's queued and running tasks; ``words`` are what the request says about
    the task (the user's request, or a tool call's ``about``), and ``task_id`` a tool call's id. In
    order: ``task_id``; a "task N" in ``words`` (other numbers, as in "top 5 stocks", are not task
    numbers); the one task in ``active`` whose request shares more topic words with ``words`` than any
    other does; the only task in ``active``. The first two may name a task that is not active.
    """
    if task_id is not None:
        return task_id
    if named := _NAMED_TASK.search(words):
        return int(named.group(1))
    wanted = _topic_words(words)
    scores = sorted(((len(wanted & _topic_words(task.text)), task.number) for task in active), reverse=True)
    if len(scores) > 1 and scores[0][0] > scores[1][0]:
        return scores[0][1]  # a clear winner: more shared words than any other task
    return active[0].number if len(active) == 1 else None


def _topic_words(text: str) -> set[str]:
    """The words of ``text`` that can tell one task's topic from another's: lowercased, without
    ``_FILLER_WORDS``, and with a plural "s" dropped ("stocks"); "Tesla's" is "tesla" and a filler "s"."""
    words = (word for word in _WORD.findall(text.lower()) if word not in _FILLER_WORDS)
    return {word.removesuffix("s") if len(word) > 3 else word for word in words} - _FILLER_WORDS
