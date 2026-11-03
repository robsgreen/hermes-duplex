"""In-process end to end: web socket → voice session (fake provider) → knn router → fake Hermes API.

The service is built by ``build_service`` from a config file, as both run modes build it. Only the
embedding model (a deterministic fake) and the Hermes API server (an aiohttp fake) stand in. Audio
is streamed at real-time pace, so each test takes about as long as the audio it sends.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import pytest
import yaml

from hermes_duplex.config import load_config
from hermes_duplex.prompts.notices import refusal
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.service import STOP_TIMEOUT_S, DuplexService, build_service
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE, WebTransport
from tests.fakes.fake_embedder import fake_embed
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SCRIPT = FIXTURES / "provider_scripts" / "delegate_portfolio.jsonl"
PHRASES = FIXTURES / "router_eval" / "phrases.yaml"

KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
ENV = {
    "KEY_GENERAL": KEYS["general"],
    "KEY_STOCKS": KEYS["stock-picker"],
    "TOKEN_ROB": "rob-web-token",
    "TOKEN_ALEX": "alex-web-token",
}
CHUNK_MS = 20
OUTPUTS = ["portfolio up 2%", "Tesla down 1%"]
"""What the two stock-picker runs report, in order."""


class Running:
    """A started service, the fake Hermes API it dispatches to, and the file its provider records to."""

    def __init__(self, service: DuplexService, api: FakeHermesApi, record_path: Path) -> None:
        self.service = service
        self.api = api
        self.record_path = record_path

    def recorded(self, method: str) -> list[dict[str, Any]]:
        """The arguments of each provider call to ``method`` so far, oldest first."""
        if not self.record_path.exists():
            return []
        calls = [json.loads(line) for line in self.record_path.read_text(encoding="utf-8").splitlines()]
        return [call["args"] for call in calls if call["method"] == method]

    async def wait_for(self, method: str, count: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """Wait until the provider recorded ``count`` calls to ``method``, and return them."""
        async with asyncio.timeout(timeout_s):
            while len(calls := self.recorded(method)) < count:
                await asyncio.sleep(0.05)
        return calls

    async def talk(self, token: str, duration_ms: int) -> None:
        """Sign in with ``token`` and send ``duration_ms`` of a −20 dBFS sine at real-time pace."""
        [web] = [t for t in self.service.transports if isinstance(t, WebTransport)]
        url = f"http://127.0.0.1:{web.port}/ws"
        chunk = _sine(CHUNK_MS)
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(url, protocols=("bearer", token)) as socket,
        ):
            started = time.monotonic()
            for number in range(duration_ms // CHUNK_MS):
                await asyncio.sleep(max(0.0, started + number * CHUNK_MS / 1000 - time.monotonic()))
                await socket.send_bytes(chunk)


def _sine(ms: int) -> bytes:
    """``ms`` of a 440 Hz sine at about −20 dBFS, as PCM16 at the web transport's rate."""
    t = np.arange(ms * RATE // 1000) / RATE
    return (3277.0 * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes()


def _write_config(tmp_path: Path, api_base: str) -> Path:
    """A duplex.yaml for the fake provider, the knn router over the phrase fixture's profiles, the
    fake Hermes API, and the web transport on a free port. rob may use every profile, alex only
    ``general``."""
    profiles = yaml.safe_load(PHRASES.read_text(encoding="utf-8"))["profiles"]
    raw = {
        "hermes": {
            "api_base": api_base,
            "profile_keys_env": {"general": "KEY_GENERAL", "stock-picker": "KEY_STOCKS"},
        },
        "provider": {"name": "fake", "script": str(SCRIPT), "record_path": "provider-calls.jsonl"},
        "router": {"backend": "knn"},
        "profiles": profiles,
        "people": {
            "rob": {"web_token_env": "TOKEN_ROB", "profiles": ["*"]},
            "alex": {"web_token_env": "TOKEN_ALEX", "profiles": ["general"]},
        },
        "transports": {"web": {"enabled": True, "port": 0}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    async with FakeHermesApi(KEYS) as api:
        api.run_scripts = [
            RunScript(events=[{"event": "run.started"}, {"event": "run.completed", "output": output}])
            for output in OUTPUTS
        ]
        config = load_config(_write_config(tmp_path, api.api_base), env={**ENV, "HERMES_HOME": str(tmp_path)})
        service = build_service(config, embed=fake_embed)
        await service.start()
        try:
            yield Running(service, api, tmp_path / "provider-calls.jsonl")
        finally:
            started = time.monotonic()
            await service.stop()
            assert time.monotonic() - started < STOP_TIMEOUT_S


async def test_rob_routes_to_stock_picker_and_the_follow_up_sticks(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_duplex.router.composite")

    # The script delegates the portfolio question at 1.5 s of input and the follow-up at 4.5 s.
    await running.talk("rob-web-token", 5000)
    results = await running.wait_for("deliver_result", 2)

    starts = running.api.requests_to("POST", "/runs")
    assert [start.path for start in starts] == ["/p/stock-picker/v1/runs"] * 2
    assert [start.body["input"] for start in starts] == ["how is my portfolio doing", "and what about Tesla"]
    assert {start.body["session_id"] for start in starts} == {"duplex-rob-stock-picker"}

    decisions = [r.__dict__["route_decision"] for r in caplog.records if "route_decision" in r.__dict__]
    assert [(d["speaker"], d["profile"], d["sticky"]) for d in decisions] == [
        ("rob", "stock-picker", False),
        ("rob", "stock-picker", True),
    ]

    assert [result["text"] for result in results] == [
        frame_result("stock-picker", output, RESULT_CAP_TOKENS) for output in OUTPUTS
    ]
    assert "Report from the stock-picker agent" in results[0]["text"]
    assert OUTPUTS[0] in results[0]["text"]


async def test_alex_is_refused_stock_picker_and_no_run_starts(running: Running) -> None:
    await running.talk("alex-web-token", 2000)
    [said] = await running.wait_for("say", 1)

    assert said["text"] == refusal("stock-picker")
    assert running.api.requests_to("POST", "/runs") == []
    assert running.recorded("deliver_result") == []
