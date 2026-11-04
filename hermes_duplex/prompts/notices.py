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


TOPIC_MAX_CHARS = 40
"""Longest a task's topic is; a longer request is cut at a word with "…"."""


def topic(text: str) -> str:
    """How every notice names the task whose request is ``text``: the request with its whitespace
    collapsed, at most ``TOPIC_MAX_CHARS`` long ("how is my portfolio doing").

    People hear tasks only by topic; a task's number is an internal id for tools and logs.
    """
    return fit_text(" ".join(text.split()), TOPIC_MAX_CHARS, marker="…")


def task_started(task: Task) -> str:
    """Notice that ``task`` started on its agent."""
    return f'Started "{topic(task.text)}" on the {task.profile} agent.'


def queued_behind(task: Task, ahead: Task) -> str:
    """Notice that ``task`` waits for ``ahead``, on the same agent, to finish."""
    return f'Queued "{topic(task.text)}" behind "{topic(ahead.text)}".'


def task_failed(task: Task, reason: str) -> str:
    """Notice that ``task`` failed; ``reason`` is a short spoken explanation.

    For a Hermes API failure the reason is ``HermesApiError.reason``, which words it ("Hermes is
    unreachable", "the general profile is not configured").
    """
    return f'Sorry, "{topic(task.text)}" failed: {reason}.'


def task_cancelled(task: Task) -> str:
    """Notice that ``task`` was cancelled."""
    return f'Cancelled "{topic(task.text)}".'


LOST_TRACK = "Hermes stopped reporting on it, so I don't know how it ended"
"""Reason for ``task_failed`` when Hermes stopped answering before the task ended."""

NOTHING_TO_CANCEL = "You have no running task to cancel."
"""Notice for a cancel request when the person has no matching queued or running task."""


def which_to_cancel(tasks: Sequence[Task]) -> str:
    """Question for a cancel request that could not tell which of the person's open ``tasks`` (two or
    more) it means, naming each by topic so they can say which."""
    topics = [f'"{topic(task.text)}"' for task in tasks]
    choices = f"{', '.join(topics[:-1])}, or {topics[-1]}" if len(topics) > 2 else " or ".join(topics)
    return f"Which one should I cancel: {choices}?"


def task_already_ended(task: Task) -> str:
    """Notice for a cancel request whose ``task`` ended on its own while it was being stopped."""
    return f'"{topic(task.text)}" had already finished.'


NO_SUCH_TASK = "I couldn't find that task."
"""Notice for a status request naming a task id, or "task N", the person has no task under."""


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
    """Neutral description of ``tasks``, oldest first, for a model's context only: each one's topic,
    internal id (its number), agent, and state, with no promise to anyone; empty for no tasks."""
    return " ".join(f"{_describe(task, with_id=True)}." for task in tasks)


def task_summary(tasks: Sequence[Task]) -> str:
    """Spoken summary of a person's tasks, oldest first, by topic, agent, and state; a queued or running
    task is promised a report when it finishes."""
    if not tasks:
        return "You have no tasks running."
    return " ".join(
        f"{_describe(task)}{_REPORT_PROMISE if task.state in ACTIVE_STATES else ''}." for task in tasks
    )


def _describe(task: Task, *, with_id: bool = False) -> str:
    """The task's topic, agent, and state as one clause without a full stop; ``with_id`` adds its
    internal id after the topic, for the model's eyes only."""
    internal_id = f" (internal id {task.number})" if with_id else ""
    return f'"{topic(task.text)}"{internal_id} on the {task.profile} agent {_STATE_PHRASES[task.state]}'


def finished_while_away(task: Task, result: str) -> str:
    """Announcement of ``task``'s ``result`` (as the prompter wrote it) for a person who was not in any
    conversation when the task finished."""
    return f'While you were away, "{topic(task.text)}" finished: {result}'


def failed_while_away(task: Task, reason: str) -> str:
    """Notice that ``task`` failed while its person was away; ``reason`` as in ``task_failed``."""
    return f'While you were away, "{topic(task.text)}" failed: {reason}.'


def cancelled_while_away(task: Task) -> str:
    """Notice that ``task`` was cancelled on Hermes while its person was away."""
    return f'While you were away, "{topic(task.text)}" was cancelled.'


PROVIDER_LOST = (
    "I lost the connection to the voice service and couldn't get it back, so I'm ending the call. "
    "Hermes keeps working on your tasks."
)
"""Last notice of a conversation whose voice provider could not be reconnected."""
