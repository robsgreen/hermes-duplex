"""Short spoken notices: refusals, task starts and ends, statuses, and function-call reasons.

Every provider speaks the same wording; only results and instructions differ by provider.
"""

from collections.abc import Sequence

from ..tasks import ACTIVE_STATES, Task, TaskState
from .base import fit_text


def refusal(profile: str) -> str:
    """Notice for a person who may not use the ``profile`` agent."""
    return f"Sorry, you don't have access to the {profile} agent, so I didn't send that."


WHO_IS_ASKING = "Sorry, who's asking? I couldn't tell who said that."
"""Notice for a request no allowed person can be credited with."""

NOT_HEARD = "Sorry, I didn't catch what you asked for. Could you say it again?"
"""Notice for a request whose words did not reach the transcript."""


GIST_MAX_CHARS = 80
"""Longest a task's request is quoted in a notice; longer ones are cut at a word with "…"."""


def gist(text: str) -> str:
    """A task's request as notices quote it: whitespace collapsed, at most ``GIST_MAX_CHARS`` long."""
    return fit_text(" ".join(text.split()), GIST_MAX_CHARS, marker="…")


def task_started(task_id: int, profile: str, text: str) -> str:
    """Notice that task ``task_id``, the request ``text``, started on the ``profile`` agent.

    The request is quoted (``gist``) so the model can tell which number a task described in words has.
    """
    return f'Task {task_id} started on the {profile} agent: "{gist(text)}".'


def queued_behind(task_id: int, text: str, ahead_task_id: int) -> str:
    """Notice that task ``task_id``, the request ``text``, waits for task ``ahead_task_id`` on the same
    agent to finish; the request is quoted as in ``task_started``."""
    return f'Task {task_id}, "{gist(text)}", is queued behind task {ahead_task_id}.'


def task_failed(task_id: int, reason: str) -> str:
    """Notice that task ``task_id`` failed; ``reason`` is a short spoken explanation.

    For a Hermes API failure the reason is ``HermesApiError.reason``, which words it ("Hermes is
    unreachable", "the general profile is not configured").
    """
    return f"Sorry, task {task_id} failed: {reason}."


def task_cancelled(task_id: int) -> str:
    """Notice that task ``task_id`` was cancelled."""
    return f"Task {task_id} was cancelled."


LOST_TRACK = "Hermes stopped reporting on it, so I don't know how it ended"
"""Reason for ``task_failed`` when Hermes stopped answering before the task ended."""

NOTHING_TO_CANCEL = "You have no running task to cancel."
"""Notice for a cancel request when the person has no matching queued or running task."""


def which_to_cancel(tasks: Sequence[Task]) -> str:
    """Question for a cancel request that names no task while the person has the running ``tasks``
    (two or more), quoting each one's request (``gist``) so they can say which."""
    choices = "; ".join(f'task {task.number}, "{gist(task.text)}"' for task in tasks)
    return f"You have several tasks running: {choices}. Which one should I cancel?"


def task_already_ended(task_id: int) -> str:
    """Notice for a cancel request whose task ``task_id`` ended on its own while it was being stopped."""
    return f"Task {task_id} had already finished."


def no_such_task(task_id: int) -> str:
    """Notice for a status request naming a task number the person has no task under."""
    return f"You have no task {task_id}."


NO_TASK_GIVEN = "Nothing was handed over: the request had no task in it."
"""Function output reason for a ``delegate`` call that carries no task text."""


def unknown_tool(name: str) -> str:
    """Function output reason for a call to a tool this app does not offer."""
    return f"There is no {name} tool."


def cancel_failed(reason: str) -> str:
    """Notice that a cancel request failed; ``reason`` is a short spoken explanation."""
    return f"Sorry, I couldn't cancel that: {reason}."


_STATE_PHRASES: dict[TaskState, str] = {
    "routing": "is being handed to Hermes",
    "queued": "is queued",
    "running": "is running",
    "completed": "has finished",
    "failed": "failed",
    "cancelled": "was cancelled",
    "unknown": "ended, but I don't know how",
}
"""How ``task_list`` and ``task_summary`` say each state."""

_REPORT_PROMISE = ", and I'll tell you when it finishes"
"""What ``task_summary`` adds to a queued or running task's state."""


def task_list(tasks: Sequence[Task]) -> str:
    """Neutral description of ``tasks``, oldest first, for a model's context: each one's number,
    agent, request (``gist``), and state, with no promise to anyone; empty for no tasks."""
    return " ".join(f"{_describe(task)}." for task in tasks)


def task_summary(tasks: Sequence[Task]) -> str:
    """Spoken summary of a person's tasks, oldest first, as ``task_list`` gives it, except that a queued
    or running task is promised a report when it finishes."""
    if not tasks:
        return "You have no tasks running."
    return " ".join(
        f"{_describe(task)}{_REPORT_PROMISE if task.state in ACTIVE_STATES else ''}." for task in tasks
    )


def _describe(task: Task) -> str:
    """The task's number, agent, request, and state as one clause, without a full stop."""
    return (
        f'Task {task.number} on the {task.profile} agent, "{gist(task.text)}", {_STATE_PHRASES[task.state]}'
    )


def finished_while_away(task_id: int, result: str) -> str:
    """Announcement of task ``task_id``'s ``result`` (as the prompter wrote it) for a person who was
    not in any conversation when the task finished."""
    return f"While you were away, task {task_id} finished: {result}"


def failed_while_away(task_id: int, reason: str) -> str:
    """Notice that task ``task_id`` failed while its person was away; ``reason`` as in ``task_failed``."""
    return f"While you were away, task {task_id} failed: {reason}."


def cancelled_while_away(task_id: int) -> str:
    """Notice that task ``task_id`` was cancelled on Hermes while its person was away."""
    return f"While you were away, task {task_id} was cancelled."


PROVIDER_LOST = (
    "I lost the connection to the voice service and couldn't get it back, so I'm ending the call. "
    "Hermes keeps working on your tasks."
)
"""Last notice of a conversation whose voice provider could not be reconnected."""
