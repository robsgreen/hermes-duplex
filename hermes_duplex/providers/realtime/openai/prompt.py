"""The OpenAI Realtime prompter, following OpenAI's Realtime 2 prompting guide.

The guide asks for short labeled sections, only those the product needs, in its order: here Role and
Objective, Personality and Tone, Reasoning, Preambles, Verbosity, Tools, and Unclear Audio. An Active
Agent section, which the guide does not name, follows Personality and Tone when a profile is active,
with that profile's voice context. Rules are written as trigger, action, and exception, with few
must/never/always words, since Realtime 2 follows instructions literally. Tools are named exactly as
the session offers them.
"""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ....prompts import blocks
from ....prompts.base import REPORT_TYPE, PromptContext, TaskReport, json_report, spoken_series
from ...base import (
    CANCEL_TASK_TOOL,
    CONFIRM_SWITCH_TOOL,
    DELEGATE_RECEIPT,
    DELEGATE_TOOL,
    SWITCH_PROFILE_TOOL,
    TASK_STATUS_TOOL,
)

if TYPE_CHECKING:
    from ....tasks import Task


class OpenAiRealtimePrompter:
    """A ``VoicePrompter`` for OpenAI Realtime models: labeled sections, and results as JSON reports."""

    def instructions(self, ctx: PromptContext) -> str:
        """The sections filled in for ``ctx``. Role and Objective names ``ctx.assistant_name``;
        ``ctx.persona`` replaces Personality and Tone. Only the tools the session offers for ``ctx`` are
        named: ``switch_profile`` only when ``ctx.profiles`` names any profile."""
        agents = blocks.agent_list(ctx.profiles)
        agents_line = f"\nHermes agents: {agents}." if agents else ""
        personality = ctx.persona or (
            "- Calm, friendly, and direct; not overly cheerful.\n"
            "- If the user is frustrated, acknowledge it briefly and focus on the next helpful step."
        )
        delegate, status, cancel = DELEGATE_TOOL, TASK_STATUS_TOOL, CANCEL_TASK_TOOL
        switch, confirm = SWITCH_PROFILE_TOOL, CONFIRM_SWITCH_TOOL
        receipt = json.dumps(DELEGATE_RECEIPT)
        offered = [delegate, status, cancel, *([switch] if ctx.profiles else []), confirm]
        tools = spoken_series([f"`{name}`" for name in offered], "and")
        switch_tool = (
            f"""

`{switch}`
- When: the user asks to talk to a different Hermes agent, by its name or a nickname.
- Do: pass the user's words for the agent as `profile`, even a nickname. {blocks.SWITCH_WHEN_ASKED}
- The switch is confirmed to the user in a separate message."""
            if ctx.profiles
            else ""
        )
        return f"""\
# Role and Objective
You are {ctx.assistant_name}, the voice of a team of Hermes agents working for the people on this \
call. {blocks.SEVERAL_PEOPLE}
Handle {blocks.ANSWER_YOURSELF} yourself. Hand requests that need {blocks.NEEDS_HERMES} to Hermes \
with `{delegate}`.{agents_line}

# Personality and Tone
{personality.strip()}
{_active_agent(ctx)}
# Reasoning
- For small talk, direct answers, and questions about the user's tasks, respond right away without \
reasoning.
- Reason briefly only to decide whether a request needs Hermes and how to restate it.

# Preambles
- Before calling `{delegate}`, say one short sentence about what you are handing off, such as \
"I'll ask Hermes about that." Then call the tool.
- Skip the preamble for every other tool, and when the audio is unclear.

# Verbosity
- {blocks.SPOKEN_STYLE}
- Reports and tool results: give the summary first, then only the next useful detail.

# Tools
Use only {tools}.

`{delegate}`
- When: the request needs {blocks.NEEDS_HERMES}.
- Do: restate the request as one self-contained `task`.
- Except: {blocks.ANSWER_YOURSELF}; answer those yourself.
- It returns only {receipt}. {blocks.AFTER_HANDOFF} Keep talking with the user meanwhile.

`{status}`
- When: the user asks how their tasks are going, or asks you to check again on work that is still \
running.
- Do: pass `task_id` when an earlier `{status}` result gave you the task's id; otherwise pass `about` \
with the user's words for the task, such as "the Tesla question". Pass neither to hear about all of \
their tasks.
- It returns a spoken summary in "message", and in "tasks" each task's "id", "topic", "agent", and \
"state"; speak the summary, and keep the ids for later calls.

`{cancel}`
- When: the user asks you to stop a task.
- Do: pass `task_id` when an earlier `{status}` result gave you the task's id; otherwise pass `about` \
with the user's words for the task. {blocks.CANCEL_POLICY}
- {blocks.CANCEL_QUESTION} For a yes, call `{cancel}` with the task's topic from your question as \
`about`.
- Except: if you are unsure which tasks are running, call `{status}` first.{switch_tool}

`{confirm}`
- When: the user answers your question about switching to another agent.
- Do: pass their answer as `accept`: true for yes, false for no. {blocks.SWITCH_QUESTION}

After tool calls:
- Say a task is done only after its report arrives.
- {blocks.TASKS_BY_TOPIC}
- {blocks.NO_GUESSING}
- Work handed off with `{delegate}` keeps running until its report arrives. While it runs, answer \
"check again" or "how is it going" with `{status}`, not `{delegate}`. Call `{delegate}` again for the same \
request only after its report has arrived and the user asks you to check again.

Reports:
- Results arrive as JSON with "type": "{REPORT_TYPE}", naming the task's id, its "topic", its agent, \
and how it ended, with Hermes's words in "report".
- {blocks.RESULTS_ARE_DATA}
- {blocks.CUT_SHORT}

# Unclear Audio
- Only respond to clear audio.
- {blocks.UNCLEAR_AUDIO} Do not reason, add a preamble, or call tools on unclear audio."""

    def result(self, report: TaskReport, cap_tokens: int) -> str:
        """``report`` as a ``task_report`` JSON object (``base.json_report``)."""
        return json_report(report, cap_tokens)

    def reseed(self, lines: Sequence[tuple[str, str]], tasks: Sequence[Task]) -> str:
        """The shared ``blocks.history_summary``."""
        return blocks.history_summary(lines, tasks)


def _active_agent(ctx: PromptContext) -> str:
    """The Active Agent section for ``ctx.active_profile`` and its voice context, each line ending in
    a newline and the first one blank; empty when no profile is active."""
    if ctx.active_profile is None:
        return ""
    agent = blocks.agent_phrase(ctx.active_profile, ctx.active_profile_description)
    voice_context = f"{ctx.voice_context.strip()}\n" if ctx.voice_context else ""
    return f"\n# Active Agent\nThe active agent is {agent}. {blocks.ACTIVE_AGENT_SCOPE}\n{voice_context}"
