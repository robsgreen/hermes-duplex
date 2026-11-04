"""End to end against a real Hermes gateway: the plugin installed, its service started by the gateway.

Voice comes in over the in-gateway web transport; the fake provider replays
``delegate_portfolio.jsonl`` (the portfolio question at 1.5 s of input, the Tesla follow-up at
4.5 s); the knn router uses the real model2vec model; runs go to Hermes's API server, whose
profiles answer from ``tests/fakes/fake_llm.py``. Nobody switches profile, so every request runs on
its person's default profile, ``general``. The ``real_hermes`` fixture is in conftest.py.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import numpy as np
import pytest

from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import TOKENS, RealHermes

pytestmark = pytest.mark.e2e

CHUNK_MS = 20
SESSION_ID = "duplex-rob-general"
"""The Hermes session every general run of rob's shares."""

GENERAL_REPLY = "general-default says: done"
"""What the fake LLM answers the general profile's default model with
(``tests/fixtures/hermes_home/profiles/general/config.yaml``)."""


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


async def test_rob_runs_on_his_default_profile_on_hermes(real_hermes: RealHermes) -> None:
    tasks_before = real_hermes.tasks().keys()
    decisions_before = len(real_hermes.route_decisions())
    results_before = len(real_hermes.provider_calls("deliver_result"))

    async with voice_call(real_hermes, "rob") as socket:
        await speak(socket, 5000)
        results = (await provider_calls(real_hermes, "deliver_result", results_before + 2))[results_before:]

    tasks = [task for uuid, task in real_hermes.tasks().items() if uuid not in tasks_before]
    assert [(task["person"], task["profile"], task["text"]) for task in tasks] == [
        ("rob", "general", "how is my portfolio doing"),
        ("rob", "general", "and what about Tesla"),
    ]
    assert real_hermes.route_decisions()[decisions_before:] == [("rob", "general")] * 2

    for task in tasks:
        assert task["model"] is None, "general has no model table: the Hermes profile's own model"
        # The run exists on the general profile: /p/general/ answers for it with that key.
        status = real_hermes.run_status("general", task["run_id"])
        assert status["status"] == "completed"
        assert status["session_id"] == SESSION_ID
        assert status["output"] == GENERAL_REPLY

    assert [result["text"] for result in results] == [
        frame_result("general", GENERAL_REPLY, RESULT_CAP_TOKENS)
    ] * 2
    assert "Report from the general agent" in results[0]["text"]


async def test_alex_runs_on_general_the_only_profile_he_may_use(real_hermes: RealHermes) -> None:
    tasks_before = real_hermes.tasks().keys()
    said_before = len(real_hermes.provider_calls("say"))

    async with voice_call(real_hermes, "alex") as socket:
        await speak(socket, 2000)
        said = (await provider_calls(real_hermes, "say", said_before + 1))[said_before:]

    assert said[0]["text"] == 'Started "how is my portfolio doing" on the general agent.'
    [task] = [task for uuid, task in real_hermes.tasks().items() if uuid not in tasks_before]
    assert (task["person"], task["profile"]) == ("alex", "general")
    assert real_hermes.run_status("general", task["run_id"])["session_id"] == "duplex-alex-general"
