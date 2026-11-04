"""End to end against a real Hermes gateway: the plugin installed, its service started by the gateway.

Voice comes in over the in-gateway web transport; the fake provider replays
``switch_to_stock_picker.jsonl`` (a weather question at 1.5 s of input, "switch to stocks", an alias
of stock-picker, at 3.5 s, and the portfolio question at 5.5 s); the knn router uses the real
model2vec model; runs go to Hermes's API server, whose profiles answer from
``tests/fakes/fake_llm.py``. Both people start on their default profile, ``general``; rob may switch
to stock-picker, alex may not. The ``real_hermes`` fixture is in conftest.py.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import numpy as np
import pytest

from hermes_duplex.prompts.notices import switch_refused, switched_to
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import TOKENS, RealHermes

pytestmark = pytest.mark.e2e

CHUNK_MS = 20

WEATHER = "what's the weather tomorrow"
PORTFOLIO = "how is my portfolio doing"

GENERAL_MODEL = "general-default"
"""The general profile's default model in ``tests/fixtures/hermes_home/profiles/general/config.yaml``."""
GENERAL_REPLY = f"{GENERAL_MODEL} says: done"
"""What the fake LLM answers ``GENERAL_MODEL`` with."""


@asynccontextmanager
async def voice_call(hermes: RealHermes, person: str) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    """A web transport connection signed in as ``person``, open until the block ends."""
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(f"{hermes.web_url}/ws", protocols=("bearer", TOKENS[person])) as socket,
    ):
        yield socket


async def speak(socket: aiohttp.ClientWebSocketResponse, duration_ms: int) -> None:
    """Send ``duration_ms`` of a 440 Hz sine at about −20 dBFS, at real-time pace."""
    t = np.arange(CHUNK_MS * RATE // 1000) / RATE
    chunk = (3277.0 * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes()
    started = time.monotonic()
    for number in range(duration_ms // CHUNK_MS):
        await asyncio.sleep(max(0.0, started + number * CHUNK_MS / 1000 - time.monotonic()))
        await socket.send_bytes(chunk)


async def provider_calls(hermes: RealHermes, method: str, count: int, timeout_s: float = 60.0) -> list[Any]:
    """Wait until the fake provider recorded ``count`` calls to ``method`` in all, and return them."""
    async with asyncio.timeout(timeout_s):
        while len(calls := hermes.provider_calls(method)) < count:
            await asyncio.sleep(0.1)
    return calls


async def said_until(hermes: RealHermes, text: str, said_before: int, timeout_s: float = 60.0) -> list[str]:
    """Wait until the fake provider was told to say ``text`` after its first ``said_before`` say calls,
    and return everything it was told to say since then."""
    async with asyncio.timeout(timeout_s):
        while text not in (said := [call["text"] for call in hermes.provider_calls("say")[said_before:]]):
            await asyncio.sleep(0.1)
    return said


def assert_ran_on_general(hermes: RealHermes, task: dict[str, Any]) -> None:
    """Assert that ``task`` completed on the general profile, in its person's general session, with
    the Hermes profile's own model."""
    assert task["model"] is None, "general has no model table: the Hermes profile's own model"
    # The run exists on the general profile: /p/general/ answers for it with that key.
    status = hermes.run_status("general", task["run_id"])
    assert status["status"] == "completed"
    assert status["session_id"] == f"duplex-{task['person']}-general"
    assert status["output"] == GENERAL_REPLY


async def test_rob_switches_to_stock_picker_by_voice_and_his_portfolio_runs_there(
    real_hermes: RealHermes,
) -> None:
    tasks_before = real_hermes.tasks().keys()
    decisions_before = len(real_hermes.route_decisions())
    calls_before = {
        method: len(real_hermes.provider_calls(method))
        for method in ("connect", "say", "update_instructions", "deliver_result")
    }

    async with voice_call(real_hermes, "rob") as socket:
        await speak(socket, 6000)
        results = await provider_calls(real_hermes, "deliver_result", calls_before["deliver_result"] + 2)

    def since(method: str) -> list[dict[str, Any]]:
        return real_hermes.provider_calls(method)[calls_before[method] :]

    tasks = [task for uuid, task in real_hermes.tasks().items() if uuid not in tasks_before]
    assert [(task["person"], task["profile"], task["text"]) for task in tasks] == [
        ("rob", "general", WEATHER),
        ("rob", "stock-picker", PORTFOLIO),
    ], "the weather runs on rob's default, the portfolio on the profile he switched to"
    # The switch request is never routed: one decision per request, each on the profile active then.
    assert real_hermes.route_decisions()[decisions_before:] == [("rob", "general"), ("rob", "stock-picker")]
    assert real_hermes.active_profiles()["rob"] == "stock-picker"

    assert_ran_on_general(real_hermes, tasks[0])
    stock_picker_reply = real_hermes.assert_ran_on_stock_picker(tasks[1])

    said = [call["text"] for call in since("say")]
    assert said.index(switched_to("stock-picker")) < said.index(
        f'Started "{PORTFOLIO}" on the stock-picker agent.'
    )
    # The voice prompt follows the switch: it started with general's voice context and was replaced
    # in place with stock-picker's.
    [connect] = since("connect")
    assert "Covers everyday questions such as the weather" in connect["instructions"]
    [update] = since("update_instructions")
    assert "Covers Rob's portfolio, holdings, and trades" in update["text"]

    assert [result["text"] for result in results[calls_before["deliver_result"] :]] == [
        frame_result("general", GENERAL_REPLY, RESULT_CAP_TOKENS),
        frame_result("stock-picker", stock_picker_reply, RESULT_CAP_TOKENS),
    ]


async def test_alex_runs_on_his_default_and_his_switch_is_refused_without_a_run(
    real_hermes: RealHermes,
) -> None:
    tasks_before = real_hermes.tasks().keys()
    said_before = len(real_hermes.provider_calls("say"))
    results_before = len(real_hermes.provider_calls("deliver_result"))
    llm_requests_before = len(real_hermes.llm_requests())

    async with voice_call(real_hermes, "alex") as socket:
        # The weather question and the switch request; the script's portfolio question comes later.
        await speak(socket, 4000)
        await provider_calls(real_hermes, "deliver_result", results_before + 1)
        said = await said_until(real_hermes, switch_refused("stock-picker"), said_before)
        await asyncio.sleep(1)  # time for a wrongly started run to show up

    assert said[0] == f'Started "{WEATHER}" on the general agent.'
    [task] = [task for uuid, task in real_hermes.tasks().items() if uuid not in tasks_before]
    assert (task["person"], task["profile"], task["text"]) == ("alex", "general", WEATHER)
    assert_ran_on_general(real_hermes, task)
    assert "alex" not in real_hermes.active_profiles()
    llm_models = {request["model"] for request in real_hermes.llm_requests()[llm_requests_before:]}
    assert llm_models == {GENERAL_MODEL}, "only the weather run reached a model"
