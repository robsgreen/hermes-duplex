"""Short spoken notices: refusals, task starts and ends, statuses, profile switches, and function-call
reasons.

Every provider speaks the same wording; only results and instructions differ by provider.
"""

import re
from collections.abc import Sequence

from ..profiles import SwitchOutcome
from ..tasks import ACTIVE_STATES, Task, TaskState
from .base import fit_text, spoken_series


def refusal(profile: str) -> str:
    """Notice for a person who may not use the ``profile`` agent."""
    return f"Sorry, you don't have access to the {profile} agent, so I didn't send that."


WHO_IS_ASKING = "Sorry, who's asking? I couldn't tell who said that."
"""Notice for a request no allowed person can be credited with."""

NOT_HEARD = "Sorry, I didn't catch what you asked for. Could you say it again?"
"""Notice for a request whose words did not reach the transcript."""


TOPIC_MAX_CHARS = 40
"""Longest a task's topic is; a longer request is cut at a word with "…"."""


_POLITE_LEAD_IN = re.compile(
    r"(?:hey|please|(?:could|can|would|will) you|find out|look up|tell me|let me know|for me)\b[\s,]*",
    re.IGNORECASE,
)
"""One polite phrase a request may open with ("could you", "please", "find out", "for me"), and the
spaces and commas after it; it says nothing about the task."""


def topic(text: str) -> str:
    """How every notice names the task whose request is ``text``: the request with its whitespace
    collapsed and its polite lead-ins dropped, at most ``TOPIC_MAX_CHARS`` long ("how is my portfolio
    doing"; "could you please find out for me what the weather is" is "what the weather is").

    Dropping the lead-ins first keeps requests that open alike from sharing one cut-short topic. A
    request that is nothing but lead-ins keeps them. People hear tasks only by topic; a task's number
    is an internal id for tools and logs.
    """
    request = " ".join(text.split())
    rest = request
    while lead_in := _POLITE_LEAD_IN.match(rest):
        rest = rest[lead_in.end() :]
    return fit_text(rest or request, TOPIC_MAX_CHARS, marker="…")


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
"""Notice for a cancel request when the person has no queued or running task."""


def which_to_cancel(tasks: Sequence[Task]) -> str:
    """Question for a cancel request that could not tell which of the person's open ``tasks`` (two or
    more) it means, naming each by topic so they can say which."""
    choices = spoken_series([f'"{topic(task.text)}"' for task in tasks], "or")
    return f"Which one should I cancel: {choices}?"


def only_one_running(task: Task) -> str:
    """Yes-or-no question for a cancel request whose words name another topic than ``task``'s, the
    person's only open task, so they can say whether to cancel it."""
    return f'The only one running is "{topic(task.text)}". Cancel that?'


def task_already_ended(task: Task) -> str:
    """Notice for a cancel request whose ``task`` ended on its own while it was being stopped."""
    return f'"{topic(task.text)}" had already finished.'


NO_SUCH_TASK = "I couldn't find that task."
"""Notice for a status request naming a task id, or "task N", the person has no task under, and for a
cancel request naming one that is not open while others are."""


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


def switched_to(profile: str) -> str:
    """Notice that the speaker's active profile is now ``profile``."""
    return f"You're now talking to {profile}."


def switching_to(profile: str) -> str:
    """Notice said just before the voice service pauses to restart on ``profile``, the speaker's new
    active profile; it confirms the switch, so the restarted session does not say ``switched_to``."""
    return f"Switching you to {profile}."


def switch_refused(profile: str) -> str:
    """Notice for a switch to ``profile``, which the speaker may not use; the active profile stays."""
    return f"Sorry, you don't have access to the {profile} agent, so I didn't switch."


def already_active(profile: str) -> str:
    """Notice for a switch to ``profile`` while it is already the speaker's active profile."""
    return f"You're already talking to {profile}."


def which_profile(permitted: Sequence[str]) -> str:
    """Question for a switch request that names no profile, or one that fits several, listing the
    ``permitted`` profiles (at least one) the speaker may switch to."""
    return f"Sorry, which agent do you mean? You can switch to {spoken_series(permitted, 'or')}."


def switch_reply(outcome: SwitchOutcome, profile: str | None, permitted: Sequence[str]) -> str:
    """What a switch request to ``profile`` is answered with, by its ``outcome``: ``switched_to``,
    ``already_active``, ``switch_refused``, or, for ``unknown`` (``profile`` may then be None: the
    words named no single profile), ``which_profile`` listing the ``permitted`` profiles."""
    match outcome:
        case "switched" if profile is not None:
            return switched_to(profile)
        case "already_active" if profile is not None:
            return already_active(profile)
        case "refused" if profile is not None:
            return switch_refused(profile)
        case _:
            return which_profile(permitted)


def suggest_switch(profile: str) -> str:
    """Yes-or-no question for a request that suits ``profile`` better than the active one, asking
    whether to switch to it first."""
    return f"That sounds like a {profile} question. Want me to switch?"


NO_QUESTION_PENDING = "There's no question waiting for your answer right now."
"""Notice, or function output message for a ``confirm_switch`` call, for a yes or no while no question
of the speaker's waits on their answer."""


def question_was_for(person: str) -> str:
    """Notice for a yes or no from someone with no question of their own, while ``person``'s question
    waits or has just ended: it is not taken as an answer, nor run as a request."""
    return f"That question was for {person}."


def fallback_to_default(profile: str) -> str:
    """Notice that the speaker's saved profile can no longer be used, so they are back on their
    default ``profile``."""
    return f"You're back on {profile}."


PROVIDER_LOST = (
    "I lost the connection to the voice service and couldn't get it back, so I'm ending the call. "
    "Hermes keeps working on your tasks."
)
"""Last notice of a conversation whose voice provider could not be reconnected."""
