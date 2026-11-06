"""Task tracking: every delegated task from routing to its end, with an append-only history on disk."""

import asyncio
import dataclasses
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args

from .providers.base import ModelChoice
from .router.base import Effort

logger = logging.getLogger(__name__)

TaskState = Literal["routing", "queued", "running", "completed", "failed", "cancelled", "unknown"]

_ALL_STATES: frozenset[str] = frozenset(get_args(TaskState))

TERMINAL_STATES: frozenset[TaskState] = frozenset({"completed", "failed", "cancelled", "unknown"})

# States in which Hermes has admitted the run, so the task sits in its profile's session queue.
ACTIVE_STATES: frozenset[TaskState] = frozenset({"queued", "running"})

_NEXT_STATES: dict[TaskState, frozenset[TaskState]] = {
    "routing": frozenset({"queued", "running", "failed", "cancelled"}),
    "queued": frozenset({"running", "failed", "cancelled", "unknown"}),
    "running": frozenset({"completed", "failed", "cancelled", "unknown"}),
}

DEFAULT_UNADMITTED_TIMEOUT_S = 600.0

DEFAULT_RESTORE_MAX_AGE_H = 24.0


class IllegalTransitionError(ValueError):
    """Raised when a task is asked to move to a state its current state cannot reach."""


@dataclass(frozen=True)
class Task:
    """A snapshot of one task. The tracker replaces it on every transition; snapshots never change.

    ``model`` is the router's model choice, or ``None`` for the Hermes profile default. ``run_id``
    is ``None`` until Hermes reports one, and stays ``None`` for a run admitted to Hermes's queue
    without an id. ``delegation_id`` and ``call_id`` are the provider's hand-off ids (GPT-Live
    delegation, or Realtime function call); either may be ``None``.
    """

    number: int
    uuid: str
    person: str
    profile: str
    effort: Effort
    model: ModelChoice | None
    text: str
    run_id: str | None
    state: TaskState
    created_at: datetime
    updated_at: datetime
    delegation_id: str | None = None
    call_id: str | None = None


@dataclass(frozen=True)
class Adoption:
    """What a session takes over from a person's earlier conversations (``TaskTracker.adopt``)."""

    open_tasks: tuple[Task, ...]
    """The person's unattended queued or running tasks, oldest first, to follow from now on."""

    untold: tuple[tuple[Task, str], ...]
    """The person's tasks that ended while no conversation told them how, oldest first, each with the
    announcement that tells it."""


class TaskTracker:
    """Tracks every task of the service and records each transition in a JSONL history.

    Every transition (including creation) appends one JSON object to ``$HERMES_HOME/duplex/tasks.jsonl``,
    and the coroutine that makes the transition returns only after that line is flushed to disk, so a
    caller can announce a state as soon as the call returns. Writes run in a worker thread, in
    transition order. Task numbers start at 1, or after the highest number ``restore`` brought back.

    A task Hermes queued without a run id can never be stopped or followed, so it moves to
    ``unknown`` after ``unadmitted_timeout_s`` seconds, or earlier through ``leave_unattended``. Call
    ``close()`` at shutdown to cancel those timers.

    A task is *unattended* while no session follows it: its conversation ended (``leave_unattended``)
    or a previous service run left it open (``restore``). Its Hermes run goes on and it keeps its last
    state, and it still counts as active for the per-person queries and ``cancel``. A task whose end
    its conversation had not yet announced when it ended is left *untold* the same way. A session that
    hears the person takes both over with ``adopt``; ``unattended_after`` wakes sessions when there is
    something new to take over.

    ``restore_max_age_h`` is how old, in hours since its creation, an open task in the history may be
    for ``restore`` to bring it back.
    """

    def __init__(
        self,
        hermes_home: Path,
        *,
        unadmitted_timeout_s: float = DEFAULT_UNADMITTED_TIMEOUT_S,
        restore_max_age_h: float = DEFAULT_RESTORE_MAX_AGE_H,
    ) -> None:
        self.history_path = hermes_home / "duplex" / "tasks.jsonl"
        self._unadmitted_timeout_s = unadmitted_timeout_s
        self._restore_max_age = timedelta(hours=restore_max_age_h)
        self._tasks: dict[int, Task] = {}
        self._write_lock = asyncio.Lock()
        self._expiry_timers: dict[int, asyncio.Task[None]] = {}
        self._next_number = 1
        self._unattended: set[int] = set()
        """Numbers of the queued or running tasks no session follows; a task leaves it when a session
        adopts it or when it ends."""
        self._untold: dict[int, str] = {}
        """Per ended task whose person has not heard how it ended, the announcement that tells it."""
        self._handed_over = asyncio.Condition()
        """Notified when ``_handovers`` moves on."""
        self._handovers = 0
        """Counts the times tasks were left unattended or untold, for ``unattended_after``."""
        self._stopping: set[int] = set()
        """Numbers of the tasks a ``cancel`` is waiting for Hermes to stop."""

    async def create(
        self,
        *,
        person: str,
        profile: str,
        effort: Effort,
        model: ModelChoice | None,
        text: str,
        delegation_id: str | None = None,
        call_id: str | None = None,
    ) -> Task:
        """Record a new routed task in state ``routing``, with the next number and a fresh uuid."""
        now = datetime.now(UTC)
        task = Task(
            number=self._next_number,
            uuid=str(uuid.uuid4()),
            person=person,
            profile=profile,
            effort=effort,
            model=model,
            text=text,
            run_id=None,
            state="routing",
            created_at=now,
            updated_at=now,
            delegation_id=delegation_id,
            call_id=call_id,
        )
        self._tasks[task.number] = task
        self._next_number += 1
        await self._append(task)
        return task

    async def transition(self, number: int, state: TaskState, *, run_id: str | None = None) -> Task:
        """Move task ``number`` to ``state`` and return the new snapshot once it is on disk.

        ``run_id``, when given, records the Hermes run id; ``None`` keeps the current one. Moving a
        task to the terminal state it is already in returns it unchanged and writes nothing, because
        the cancel path and the run's own event stream both report the same end.

        Raises ``KeyError`` for an unknown number and ``IllegalTransitionError`` for any other move
        the state machine does not allow (``routing → queued → running → completed | failed |
        cancelled | unknown``, where ``queued`` may be skipped).
        """
        task = self._tasks[number]
        if state == task.state and state in TERMINAL_STATES:
            return task
        if state not in _NEXT_STATES.get(task.state, frozenset()):
            raise IllegalTransitionError(f"task {number}: {task.state} -> {state} is not allowed")

        task = dataclasses.replace(
            task, state=state, run_id=task.run_id if run_id is None else run_id, updated_at=datetime.now(UTC)
        )
        self._tasks[number] = task
        if state in TERMINAL_STATES:
            self._unattended.discard(number)
        self._update_expiry_timer(task)
        await self._append(task)
        return task

    def get(self, number: int) -> Task | None:
        """Return task ``number``, or ``None`` if this service run has no such task."""
        return self._tasks.get(number)

    def active_for(self, person: str) -> list[Task]:
        """Return the person's queued and running tasks, attended or not, oldest first."""
        return sorted(
            (task for task in self._tasks.values() if task.person == person and task.state in ACTIVE_STATES),
            key=lambda task: task.number,
        )

    def latest_running(self, person: str) -> Task | None:
        """Return the person's most recently created running task, or ``None``."""
        running = [task for task in self.active_for(person) if task.state == "running"]
        return running[-1] if running else None

    def ahead_of(self, number: int) -> Task | None:
        """Return the queued or running task directly ahead of task ``number`` in Hermes's queue.

        Hermes runs one person's tasks on one profile in order, so that is the newest active task
        created before ``number`` for the same person and profile. ``None`` means nothing is ahead.
        """
        task = self._tasks[number]
        ahead = [
            other
            for other in self.active_for(task.person)
            if other.profile == task.profile and other.number < number
        ]
        return ahead[-1] if ahead else None

    async def cancel(
        self,
        person: str,
        stop: Callable[[str, str], Awaitable[None]],
        number: int | None = None,
    ) -> Task | None:
        """Cancel the person's task ``number``, or their most recent running task when it is ``None``.

        Returns the cancelled task, or ``None`` when the person has no matching active task (see
        ``active_for``). With a run id, awaits ``stop(profile, run_id)`` and marks the task
        ``cancelled`` once it returns; an error from ``stop`` propagates and leaves the task as it was.
        If the run ended on its own while ``stop`` was in flight (``stopping`` is true meanwhile), that
        end stands and the task is returned as it is. Without a run id there is nothing to stop, so the
        task is marked ``cancelled`` locally.
        """
        if number is None:
            task = self.latest_running(person)
        else:
            task = next((task for task in self.active_for(person) if task.number == number), None)
        if task is None:
            return None

        if task.run_id is None:
            logger.warning(
                "task %d cancelled locally: Hermes queued it without a run id and may still run it",
                task.number,
            )
        else:
            self._stopping.add(task.number)
            try:
                await stop(task.profile, task.run_id)
            finally:
                self._stopping.discard(task.number)
            task = self._tasks[task.number]
            if task.state in TERMINAL_STATES:
                return task
        return await self.transition(task.number, "cancelled")

    def stopping(self, number: int) -> bool:
        """Whether a ``cancel`` is waiting for Hermes to stop task ``number``; its caller then reports
        how the task ended, so an end seen meanwhile need not be reported again."""
        return number in self._stopping

    async def leave_unattended(self, numbers: Iterable[int], untold: Mapping[int, str] | None = None) -> None:
        """Record that no session follows the listed tasks any more; a session calls this with its
        task numbers when it ends.

        Each listed task still queued without a run id moves to ``unknown``, since nothing can follow
        it. Each other one still queued or running becomes unattended (see the class docstring) until
        a session ``adopt``s it. ``untold`` maps each task whose end the session had not yet
        announced to the announcement that tells it, worded for a person who was away; the next
        session that adopts the person's tasks announces it. Numbers this tracker does not know are
        ignored.
        """
        for number in numbers:
            await self._expire_if_unadmitted(number, reason="the session ended")
            task = self._tasks.get(number)
            if task is not None and task.state in ACTIVE_STATES:
                self._unattended.add(number)
                logger.info("task %d is unattended; its Hermes run %s goes on", number, task.run_id)
        for number, announcement in (untold or {}).items():
            if number in self._tasks:
                self._untold[number] = announcement
                logger.info("task %d ended untold; its person hears it in their next conversation", number)
        await self._hand_over()

    def adopt(self, person: str) -> Adoption:
        """Hand the calling session the person's unattended and untold tasks (see the class docstring).

        Each task is handed out once: a later call, from this session or another, does not return it
        again until it is left unattended or untold again. Other people's tasks stay as they are.
        """
        open_tasks = tuple(task for task in self.active_for(person) if task.number in self._unattended)
        self._unattended.difference_update(task.number for task in open_tasks)
        untold = tuple(
            (self._tasks[number], self._untold.pop(number))
            for number in sorted(self._untold)
            if self._tasks[number].person == person
        )
        return Adoption(open_tasks, untold)

    async def unattended_after(self, handovers: int) -> int:
        """Wait until tasks have been left unattended or untold more than ``handovers`` times since
        this tracker was made, and return that count; pass the last value it returned (first 0)."""
        async with self._handed_over:
            await self._handed_over.wait_for(lambda: self._handovers > handovers)
            return self._handovers

    async def restore(self) -> None:
        """Bring back the tasks a previous service run left queued or running with a run id, as
        unattended, from the history file; call it once, before the first ``create``.

        Each task comes back in the state its last history line records, with its run id, person, and
        profile; it keeps its number unless a task restored before it holds that number, and then
        takes the next free one. A task queued without a run id cannot be followed, so it is left out.
        An open task created more than ``restore_max_age_h`` ago is not brought back: a line marking it
        ``unknown`` is appended instead, so it stays out from then on, and nobody is told.

        A bad history never stops the service: a line that is not valid UTF-8 or JSON, or not a task, is
        skipped with a warning, a timestamp without a time zone is read as UTC, and a history file that
        is missing or cannot be read restores nothing.
        """
        latest: dict[str, Task] = {}
        try:
            lines = await asyncio.to_thread(_read_lines, self.history_path)
        except OSError as error:
            logger.error("could not read %s, so no tasks are restored: %r", self.history_path, error)
            return
        for line in lines:
            try:
                task = _task_from_record(json.loads(line.decode("utf-8")))
            except (ValueError, TypeError, KeyError) as error:  # UnicodeDecodeError is a ValueError
                logger.warning("skipped a malformed line in %s: %r", self.history_path, error)
                continue
            latest[task.uuid] = task
        open_tasks = sorted(
            (task for task in latest.values() if task.state in ACTIVE_STATES and task.run_id is not None),
            key=lambda task: task.created_at,
        )
        oldest_restored = datetime.now(UTC) - self._restore_max_age
        for task in open_tasks:
            if task.created_at < oldest_restored:
                logger.info(
                    "marked task %d of %s unknown without restoring it: created %s, more than %g h ago",
                    task.number,
                    task.person,
                    task.created_at.isoformat(),
                    self._restore_max_age.total_seconds() / 3600,
                )
                await self._append(dataclasses.replace(task, state="unknown", updated_at=datetime.now(UTC)))
                continue
            if task.number in self._tasks:
                task = dataclasses.replace(task, number=max(self._tasks) + 1)
            self._tasks[task.number] = task
            self._unattended.add(task.number)
            self._next_number = max(self._next_number, task.number + 1)
            logger.info(
                "restored task %d for %s on %s (Hermes run %s, %s)",
                task.number,
                task.person,
                task.profile,
                task.run_id,
                task.state,
            )
        await self._hand_over()

    async def close(self) -> None:
        """Cancel the pending expiry timers; tasks keep their current state."""
        timers = list(self._expiry_timers.values())
        self._expiry_timers.clear()
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)

    def _update_expiry_timer(self, task: Task) -> None:
        """Start the expiry timer when a task is queued without a run id; stop it when it leaves."""
        unadmitted = task.state == "queued" and task.run_id is None
        if unadmitted and task.number not in self._expiry_timers:
            self._expiry_timers[task.number] = asyncio.create_task(self._expire_after_timeout(task.number))
        elif not unadmitted and (timer := self._expiry_timers.pop(task.number, None)) is not None:
            timer.cancel()

    async def _expire_after_timeout(self, number: int) -> None:
        await asyncio.sleep(self._unadmitted_timeout_s)
        # Drop the entry first so the transition does not cancel this running timer.
        self._expiry_timers.pop(number, None)
        await self._expire_if_unadmitted(number, reason=f"no run id after {self._unadmitted_timeout_s:g} s")

    async def _expire_if_unadmitted(self, number: int, reason: str) -> None:
        task = self._tasks.get(number)
        if task is None or task.state != "queued" or task.run_id is not None:
            return
        logger.warning("task %d marked unknown: queued by Hermes without a run id, and %s", number, reason)
        await self.transition(number, "unknown")

    async def _hand_over(self) -> None:
        """Wake the sessions waiting in ``unattended_after``."""
        async with self._handed_over:
            self._handovers += 1
            self._handed_over.notify_all()

    async def _append(self, task: Task) -> None:
        # The line is built before waiting for the lock, and asyncio locks are FIFO, so lines land in
        # transition order even when transitions overlap.
        line = json.dumps(_history_record(task), default=_encode_json) + "\n"
        async with self._write_lock:
            await asyncio.to_thread(_append_line, self.history_path, line)


def _history_record(task: Task) -> dict[str, object]:
    # asdict also turns ``model`` (a ModelChoice) into a plain mapping.
    record = dataclasses.asdict(task)
    record["task"] = record.pop("number")
    return record


def _task_from_record(record: dict[str, Any]) -> Task:
    """The ``Task`` a history line records (``_history_record``'s inverse).

    A timestamp without a time zone is read as UTC. Raises ``KeyError``, ``TypeError``, or
    ``ValueError`` when the record lacks a field or holds one of the wrong kind.
    """
    model = record["model"]
    state = record["state"]
    if state not in _ALL_STATES:
        raise ValueError(f"unknown task state {state!r}")
    return Task(
        number=int(record["task"]),
        uuid=str(record["uuid"]),
        person=str(record["person"]),
        profile=str(record["profile"]),
        effort=record["effort"],
        model=None if model is None else ModelChoice(provider=model["provider"], model=model["model"]),
        text=str(record["text"]),
        run_id=None if record["run_id"] is None else str(record["run_id"]),
        state=state,
        created_at=_utc(datetime.fromisoformat(record["created_at"])),
        updated_at=_utc(datetime.fromisoformat(record["updated_at"])),
        delegation_id=record.get("delegation_id"),
        call_id=record.get("call_id"),
    )


def _utc(moment: datetime) -> datetime:
    """``moment``, read as UTC when it has no time zone."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _read_lines(path: Path) -> list[bytes]:
    """The non-blank lines of ``path``, undecoded, or none when it does not exist."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return [line for line in data.splitlines() if line.strip()]


def _encode_json(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot write {type(value).__name__} to the task history")


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(line)
        file.flush()
        os.fsync(file.fileno())
