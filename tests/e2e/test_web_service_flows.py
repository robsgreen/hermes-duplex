"""The whole service as ``build_service`` wires it, driven from a browser socket: routing across
profiles, the model table, the page's profile picker, voice status and cancel, Hermes refusals and
outages, long results, uncredited requests, and shutdown.

Only the embedding model (a deterministic fake), the voice provider (the scripted fake), and the
Hermes API server (an aiohttp fake) stand in; config loading, the web transport, the voice session,
the knn router, the Hermes client, and the task history are the real ones. Audio goes in bursts,
one request at a time, so each test runs in about a second and requests reach the router in order.
"""

import asyncio
import json
import logging
import socket
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import pytest
import yaml

from hermes_duplex.config import load_config
from hermes_duplex.prompts.base import TRUNCATION_MARKER
from hermes_duplex.prompts.notices import WHO_IS_ASKING, switched_to
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.service import STOP_TIMEOUT_S, DuplexService, build_service
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE, WebTransport
from tests.fakes.fake_embedder import fake_embed
from tests.fakes.fake_hermes_api import (
    FakeHermesApi,
    Reply,
    RunScript,
    draining,
    idempotency_conflict,
    refusal,
    session_busy,
)

PHRASES = Path(__file__).resolve().parents[1] / "fixtures" / "router_eval" / "phrases.yaml"

KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
ENV = {
    "KEY_GENERAL": KEYS["general"],
    "KEY_STOCKS": KEYS["stock-picker"],
    "TOKEN_ROB": "rob-web-token",
    "TOKEN_ALEX": "alex-web-token",
}
ROB = ENV["TOKEN_ROB"]

STOCKS_HIGH = {"provider": "anthropic", "model": "stocks-deep"}
STOCKS_DEFAULT = {"provider": "openai", "model": "stocks-default"}

# A run that starts and never ends on its own, so it stays open for queueing, status, and cancel.
OPEN_RUN = RunScript(events=[{"event": "run.started"}])

ROUTE_FIELDS = {
    "speaker",
    "profile",
    "control",
    "effort",
    "model",
    "suggestion",
    "top3",
    "source",
    "latency_ms",
}


def _delegate(at_input_ms: int, call_id: str, task: str) -> dict[str, Any]:
    return {
        "at_input_ms": at_input_ms,
        "event": {"type": "delegation", "id": call_id, "tool_args": {"task": task}},
    }


def _completed(output: str) -> RunScript:
    return RunScript(events=[{"event": "run.started"}, {"event": "run.completed", "output": output}])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Stack:
    """A started service, the fake Hermes API it dispatches to (None while Hermes is down), and
    what its fake provider and task history recorded."""

    def __init__(self, service: DuplexService, api: FakeHermesApi | None, home: Path) -> None:
        self.service = service
        self.api = api
        self.home = home
        [web] = [t for t in service.transports if isinstance(t, WebTransport)]
        self.ws_url = f"http://127.0.0.1:{web.port}/ws"

    def recorded(self, method: str) -> list[dict[str, Any]]:
        """The arguments of each provider call to ``method`` so far, oldest first."""
        return [
            call["args"]
            for call in _read_jsonl(self.home / "provider-calls.jsonl")
            if call["method"] == method
        ]

    def said(self) -> list[str]:
        return [args["text"] for args in self.recorded("say")]

    async def wait_for(self, method: str, count: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """Wait until the provider recorded ``count`` calls to ``method``, and return them."""
        async with asyncio.timeout(timeout_s):
            while len(calls := self.recorded(method)) < count:
                await asyncio.sleep(0.02)
        return calls

    def history(self) -> list[dict[str, Any]]:
        """Every record in ``$HERMES_HOME/duplex/tasks.jsonl``, oldest first."""
        return _read_jsonl(self.home / "duplex" / "tasks.jsonl")

    def states(self, task: int) -> list[str]:
        """The states task ``task`` went through, as the history recorded them."""
        return [record["state"] for record in self.history() if record["task"] == task]


class Browser:
    """One page signed in on ``/ws``: sends audio and keeps every status frame the server sends."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self._socket = socket
        self.status_frames: list[dict[str, Any]] = []
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def speak(self, duration_ms: int, *, loud: bool = True) -> None:
        """Send ``duration_ms`` of a 440 Hz sine at about −20 dBFS (or of silence) at once."""
        samples = duration_ms * RATE // 1000
        amplitude = 3277.0 if loud else 0.0
        wave = amplitude * np.sin(2 * np.pi * 440 * np.arange(samples) / RATE)
        await self._socket.send_bytes(wave.astype("<i2").tobytes())

    async def choose_profile(self, name: str) -> None:
        """Pick ``name`` in the page's profile picker."""
        await self._socket.send_str(json.dumps({"type": "profile", "name": name}))

    def task_lists(self) -> list[list[dict[str, Any]]]:
        return [frame["tasks"] for frame in self.status_frames if "tasks" in frame]

    def active_profiles(self) -> list[str]:
        """The active profile of each profile status the page got, oldest first."""
        return [frame["active_profile"] for frame in self.status_frames if "active_profile" in frame]

    async def _read(self) -> None:
        async for message in self._socket:
            if message.type == aiohttp.WSMsgType.TEXT:
                frame = json.loads(message.data)
                if frame.get("type") == "status":
                    self.status_frames.append(frame)
        self.closed.set()


@asynccontextmanager
async def browser(stack: Stack, token: str) -> AsyncIterator[Browser]:
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(stack.ws_url, protocols=("bearer", token)) as ws,
    ):
        page = Browser(ws)
        yield page


def _write_config(home: Path, api_base: str, script: Sequence[Mapping[str, Any]]) -> Path:
    """A duplex.yaml for the fake provider replaying ``script``, the knn router over the phrase
    fixture's profiles, Hermes at ``api_base``, and the web transport on a free port. stock-picker
    has a model only for effort ``high`` plus a default model; general has none. rob may use every
    profile and starts on ``stock-picker``; alex may use only ``general``. The router suggests no
    switches."""
    script_path = home / "script.jsonl"
    script_path.write_text("".join(json.dumps(step) + "\n" for step in script), encoding="utf-8")
    profiles = yaml.safe_load(PHRASES.read_text(encoding="utf-8"))["profiles"]
    profiles["stock-picker"] |= {"models": {"high": STOCKS_HIGH}, "default_model": STOCKS_DEFAULT}
    raw = {
        "hermes": {
            "api_base": api_base,
            "profile_keys_env": {"general": "KEY_GENERAL", "stock-picker": "KEY_STOCKS"},
        },
        "provider": {"name": "fake", "script": str(script_path), "record_path": "provider-calls.jsonl"},
        # These flows dispatch at once; held requests and the switch question have their own tests.
        "router": {"backend": "knn", "suggest_switch": False},
        "profiles": profiles,
        "people": {
            "rob": {"web_token_env": "TOKEN_ROB", "profiles": ["*"], "default_profile": "stock-picker"},
            "alex": {"web_token_env": "TOKEN_ALEX", "profiles": ["general"]},
        },
        "transports": {"web": {"enabled": True, "port": 0}},
    }
    path = home / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@asynccontextmanager
async def running(
    home: Path, script: Sequence[Mapping[str, Any]], api: FakeHermesApi | None, api_base: str | None = None
) -> AsyncIterator[Stack]:
    """The service built from config for ``script``, dispatching to ``api`` (or to ``api_base``,
    where nothing may be listening yet); stopped within ``STOP_TIMEOUT_S`` at the end."""
    base = api.api_base if api is not None else api_base
    assert base is not None
    config = load_config(_write_config(home, base, script), env={**ENV, "HERMES_HOME": str(home)})
    service = build_service(config, embed=fake_embed)
    await service.start()
    try:
        yield Stack(service, api, home)
    finally:
        started = time.monotonic()
        await service.stop()
        assert time.monotonic() - started < STOP_TIMEOUT_S


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        yield fake


async def test_every_request_runs_on_the_active_profile_with_its_model(
    tmp_path: Path, api: FakeHermesApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_duplex.router.composite")
    api.run_scripts = [_completed("portfolio up 2%"), _completed("sunny, 21 degrees")]
    script = [
        _delegate(1500, "c1", "how is my portfolio doing"),
        _delegate(3500, "c2", "what's the weather tomorrow"),
    ]
    async with running(tmp_path, script, api) as stack, browser(stack, ROB) as page:
        await page.speak(2000)
        await stack.wait_for("deliver_result", 1)
        await page.speak(2000)
        results = await stack.wait_for("deliver_result", 2)

    portfolio, weather = api.requests_to("POST", "/runs")
    for start in (portfolio, weather):
        assert start.path == "/p/stock-picker/v1/runs"
        assert start.body["session_id"] == "duplex-rob-stock-picker"
        # knn gives no effort, so the request runs at medium, which has no cell: the default model.
        assert start.body["model_options"] == {"reasoning": {"effort": "medium"}}
        assert (start.body["provider"], start.body["model"]) == (
            STOCKS_DEFAULT["provider"],
            STOCKS_DEFAULT["model"],
        )
        assert start.headers["Authorization"] == f"Bearer {KEYS['stock-picker']}"
    assert weather.body["input"] == "what's the weather tomorrow"

    decisions = [r.__dict__["route_decision"] for r in caplog.records if "route_decision" in r.__dict__]
    assert all(decision.keys() == ROUTE_FIELDS for decision in decisions)
    assert [(d["speaker"], d["profile"], d["model"], d["source"], d["control"]) for d in decisions] == [
        ("rob", "stock-picker", "openai/stocks-default", "knn", None),
    ] * 2
    assert all(len(d["top3"]) <= 3 and d["latency_ms"] >= 0 for d in decisions)

    assert [result["text"] for result in results] == [
        frame_result("stock-picker", "portfolio up 2%", RESULT_CAP_TOKENS),
        frame_result("stock-picker", "sunny, 21 degrees", RESULT_CAP_TOKENS),
    ]


async def test_queue_status_and_cancel_by_voice(tmp_path: Path, api: FakeHermesApi) -> None:
    api.run_scripts = [OPEN_RUN, OPEN_RUN]
    script = [
        _delegate(1500, "c1", "how is my portfolio doing"),
        _delegate(3500, "c2", "and what about Tesla"),
        _delegate(5500, "c3", "what are you working on"),
        _delegate(7500, "c4", "cancel task 2"),
    ]
    async with running(tmp_path, script, api) as stack, browser(stack, ROB) as page:
        for spoken in range(1, 5):
            await page.speak(2000)
            await stack.wait_for("say", spoken)
        tasks_seen = page.task_lists()

    assert stack.said() == [
        'Started "how is my portfolio doing" on the stock-picker agent.',
        'Queued "and what about Tesla" behind "how is my portfolio doing".',
        (
            '"how is my portfolio doing" on the stock-picker agent is running, and I\'ll tell you when it '
            'finishes. "and what about Tesla" on the stock-picker agent is running, and I\'ll tell you when '
            "it finishes."
        ),
        'Cancelled "and what about Tesla".',
    ]
    # Every call got exactly one receipt; status and cancel were answered without a Hermes run.
    assert [args["payload"] for args in stack.recorded("send_function_output")] == [
        {"status": "received"}
    ] * 4
    first, second = api.requests_to("POST", "/runs")
    assert {first.body["session_id"], second.body["session_id"]} == {"duplex-rob-stock-picker"}
    second_run = list(api.runs.values())[1]
    assert [r.path for r in api.requests_to("POST", "/stop")] == [
        f"/p/stock-picker/v1/runs/{second_run.run_id}/stop"
    ]

    assert stack.states(1) == ["routing", "running"]
    assert stack.states(2) == ["routing", "running", "cancelled"]
    assert [record["run_id"] for record in stack.history() if record["task"] == 2][-1] == second_run.run_id
    assert tasks_seen[-1] == [
        {"number": 1, "profile": "stock-picker", "state": "running", "text": "how is my portfolio doing"},
        {"number": 2, "profile": "stock-picker", "state": "cancelled", "text": "and what about Tesla"},
    ]


async def test_busy_hermes_session_is_retried_with_the_same_session_and_key(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.start_replies = [session_busy()]
    api.run_scripts = [_completed("portfolio up 2%")]
    async with (
        running(tmp_path, [_delegate(1500, "c1", "how is my portfolio doing")], api) as stack,
        browser(stack, ROB) as page,
    ):
        await page.speak(2000)
        [result] = await stack.wait_for("deliver_result", 1)

    busy, accepted = api.requests_to("POST", "/runs")
    assert busy.headers["Idempotency-Key"] == accepted.headers["Idempotency-Key"]
    assert busy.body == accepted.body
    assert accepted.body["session_id"] == "duplex-rob-stock-picker"
    [task] = {record["uuid"] for record in stack.history()}
    assert accepted.headers["Idempotency-Key"] == task
    assert result["text"] == frame_result("stock-picker", "portfolio up 2%", RESULT_CAP_TOKENS)
    assert stack.states(1) == ["routing", "running", "completed"]


@pytest.mark.parametrize(
    ("replies", "reason", "attempts", "logged"),
    [
        (
            [refusal("permission_denied", 403)],
            "Hermes refused it (permission_denied)",
            1,
            "permission_denied",
        ),
        ([idempotency_conflict()], "Hermes rejected it as a duplicate task", 1, "Idempotency-Key conflict"),
        ([draining("0")] * 4, "Hermes is temporarily unavailable", 4, "runtime_draining"),
    ],
    ids=["refused", "duplicate-key", "draining"],
)
async def test_hermes_refusal_fails_the_task_with_a_spoken_reason(
    tmp_path: Path,
    api: FakeHermesApi,
    caplog: pytest.LogCaptureFixture,
    replies: list[Reply],
    reason: str,
    attempts: int,
    logged: str,
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_duplex.dispatch.hermes_api")
    api.start_replies = list(replies)
    async with (
        running(tmp_path, [_delegate(1500, "c1", "how is my portfolio doing")], api) as stack,
        browser(stack, ROB) as page,
    ):
        await page.speak(2000)
        [failure] = await stack.wait_for("deliver_result", 1)

    assert failure["text"] == f'Sorry, "how is my portfolio doing" failed: {reason}.'
    assert failure["ref"] == {"delegation_id": None, "call_id": "c1", "task_id": 1}
    assert len(api.requests_to("POST", "/runs")) == attempts
    assert api.runs == {}
    assert api.requests_to("GET", "/events") == []
    assert stack.states(1) == ["routing", "failed"]
    assert any(
        logged in record.getMessage() and record.levelno >= logging.WARNING for record in caplog.records
    )


async def test_hermes_down_fails_the_task_and_the_next_one_runs_once_it_is_back(tmp_path: Path) -> None:
    port = _free_port()
    script = [
        _delegate(1500, "c1", "how is my portfolio doing"),
        _delegate(3500, "c2", "how are my stocks doing"),
    ]
    async with (
        running(tmp_path, script, None, api_base=f"http://127.0.0.1:{port}") as stack,
        browser(stack, ROB) as page,
    ):
        await page.speak(2000)
        [failure] = await stack.wait_for("deliver_result", 1)
        assert failure["text"] == 'Sorry, "how is my portfolio doing" failed: Hermes is unreachable.'

        async with FakeHermesApi(KEYS, port=port) as api:
            api.run_scripts = [_completed("portfolio up 2%")]
            await page.speak(2000)
            result = (await stack.wait_for("deliver_result", 2))[1]

    assert [r.path for r in api.requests_to("POST", "/runs")] == ["/p/stock-picker/v1/runs"]
    assert result["ref"]["task_id"] == 2
    assert result["text"] == frame_result("stock-picker", "portfolio up 2%", RESULT_CAP_TOKENS)
    assert stack.states(1) == ["routing", "failed"]
    assert stack.states(2) == ["routing", "running", "completed"]


async def test_long_result_is_framed_and_cut_to_the_cap(tmp_path: Path, api: FakeHermesApi) -> None:
    report = " ".join(f"holding{n} is up {n}%" for n in range(2000))
    api.run_scripts = [_completed(report)]
    async with (
        running(tmp_path, [_delegate(1500, "c1", "how is my portfolio doing")], api) as stack,
        browser(stack, ROB) as page,
    ):
        await page.speak(2000)
        [result] = await stack.wait_for("deliver_result", 1)

    text = result["text"]
    assert text.startswith("Report from the stock-picker agent.")
    assert text.endswith(f"{TRUNCATION_MARKER}\n</report>")
    assert len(text) <= RESULT_CAP_TOKENS * 4
    assert "holding0 is up 0%" in text
    assert report not in text


async def test_request_nobody_was_voiced_for_is_asked_about_and_not_run(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with (
        running(tmp_path, [_delegate(1500, "c1", "how is my portfolio doing")], api) as stack,
        browser(stack, ROB) as page,
    ):
        await page.speak(2000, loud=False)
        [receipt] = await stack.wait_for("send_function_output", 1)

    assert receipt == {"call_id": "c1", "payload": {"status": "not_started", "message": WHO_IS_ASKING}}
    assert api.requests == []
    assert stack.history() == []


async def test_stop_with_a_running_task_closes_the_browser_and_the_provider_within_5_s(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts = [OPEN_RUN]
    async with running(tmp_path, [_delegate(1500, "c1", "how is my portfolio doing")], api) as stack:
        async with aiohttp.ClientSession() as http:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await http.ws_connect(stack.ws_url, protocols=("bearer", "not-a-token"))
            assert refused.value.status == 401

        async with browser(stack, ROB) as page:
            await page.speak(2000)
            await stack.wait_for("say", 1)
            assert stack.service.status().tasks[0].state == "running"

            started = time.monotonic()
            await stack.service.stop()
            assert time.monotonic() - started < STOP_TIMEOUT_S
            async with asyncio.timeout(1):
                await page.closed.wait()

        assert stack.recorded("close") == [{}]
        async with aiohttp.ClientSession() as http:
            with pytest.raises(aiohttp.ClientConnectionError):
                await http.ws_connect(stack.ws_url, protocols=("bearer", ROB))


async def test_the_page_picker_switches_the_live_conversation_and_the_next_request(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    api.run_scripts = [_completed("sunny, 21 degrees")]
    script = [_delegate(1500, "c1", "what's the weather tomorrow")]
    async with running(tmp_path, script, api) as stack, browser(stack, ROB) as page:
        await stack.wait_for("connect", 1)  # the session listens for rob's switches from here on
        await page.choose_profile("general")
        await stack.wait_for("say", 1)
        await page.speak(2000)
        await stack.wait_for("deliver_result", 1)
        profiles_shown = page.active_profiles()

    assert stack.said()[0] == switched_to("general")
    [instructions] = stack.recorded("update_instructions")
    assert "general" in instructions["text"]
    [start] = api.requests_to("POST", "/runs")
    assert start.path == "/p/general/v1/runs"
    assert start.body["session_id"] == "duplex-rob-general"
    assert profiles_shown == ["stock-picker", "general"]
    saved = json.loads((tmp_path / "duplex" / "active_profiles.json").read_text(encoding="utf-8"))
    assert saved == {"rob": "general"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
