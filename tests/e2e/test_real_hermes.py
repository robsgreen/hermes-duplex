"""End to end against a real Hermes gateway: the plugin installed, its service started by the gateway.

Voice comes in over the in-gateway web transport; the fake provider replays
``delegate_portfolio.jsonl`` (the portfolio question at 1.5 s of input, the Tesla follow-up at
4.5 s); the knn router uses the real model2vec model; runs go to Hermes's API server, whose
profiles answer from ``tests/fakes/fake_llm.py``. The ``real_hermes`` fixture is in conftest.py.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import numpy as np
import pytest

from hermes_duplex.prompts.notices import refusal
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import LLM_REPLIES, TOKENS, RealHermes

pytestmark = pytest.mark.e2e

CHUNK_MS = 20
SESSION_ID = "duplex-rob-stock-picker"
"""The Hermes session every stock-picker run of rob's shares."""


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


async def test_rob_is_routed_to_stock_picker_and_runs_on_hermes(real_hermes: RealHermes) -> None:
    tasks_before = real_hermes.tasks().keys()
    decisions_before = len(real_hermes.route_decisions())
    results_before = len(real_hermes.provider_calls("deliver_result"))

    async with voice_call(real_hermes, "rob") as socket:
        await speak(socket, 5000)
        results = (await provider_calls(real_hermes, "deliver_result", results_before + 2))[results_before:]

    tasks = [task for uuid, task in real_hermes.tasks().items() if uuid not in tasks_before]
    assert [(task["person"], task["profile"], task["text"]) for task in tasks] == [
        ("rob", "stock-picker", "how is my portfolio doing"),
        ("rob", "stock-picker", "and what about Tesla"),
    ]
    assert real_hermes.route_decisions()[decisions_before:] == [
        ("rob", "stock-picker", False),
        ("rob", "stock-picker", True),
    ]

    replies = []
    for task in tasks:
        model = real_hermes.model_table[task["effort"]]
        assert task["model"] == model
        # The run exists on the stock-picker profile: /p/stock-picker/ answers for it with that key.
        status = real_hermes.run_status("stock-picker", task["run_id"])
        assert status["status"] == "completed"
        assert status["session_id"] == SESSION_ID
        assert status["runtime"]["requested"] == model
        # No run status field carries the reasoning effort; the model request Hermes sent does.
        sent = [r for r in real_hermes.llm_requests() if r["model"] == model["model"]]
        assert task["effort"] in {request.get("reasoning_effort") for request in sent}
        replies.append(LLM_REPLIES[model["model"]])
        assert status["output"] == replies[-1]

    assert [result["text"] for result in results] == [
        frame_result("stock-picker", reply, RESULT_CAP_TOKENS) for reply in replies
    ]
    assert "Report from the stock-picker agent" in results[0]["text"]


async def test_alex_is_refused_stock_picker_and_no_run_starts(real_hermes: RealHermes) -> None:
    tasks_before = real_hermes.tasks()
    llm_requests_before = len(real_hermes.llm_requests())
    said_before = len(real_hermes.provider_calls("say"))

    async with voice_call(real_hermes, "alex") as socket:
        await speak(socket, 2000)
        said = (await provider_calls(real_hermes, "say", said_before + 1))[said_before:]

    assert [call["text"] for call in said] == [refusal("stock-picker")]
    new_runs = [
        task
        for uuid, task in real_hermes.tasks().items()
        if task["run_id"] is not None and task["run_id"] != tasks_before.get(uuid, {}).get("run_id")
    ]
    assert new_runs == []
    assert real_hermes.llm_requests()[llm_requests_before:] == []
