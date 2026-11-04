"""Against a real Hermes gateway with the plugin installed: requests on two topics that both run on
the speaker's default profile, a status request that never reaches Hermes, and ``doctor`` checking the
live API server.

The gateway runs its own provider script (written by the fixture): the portfolio question at 1.5 s of
input, "what's the weather tomorrow" at 3.5 s, and "what are you working on" at 5.5 s. The knn
router uses the real model2vec model; both profiles answer from ``tests/fakes/fake_llm.py``. The
fixture also checks that the gateway is gone, and its home removed, once it ends.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError

import aiohttp
import numpy as np
import pytest
import yaml

from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import KEYS, REPO, TOKENS, RealHermes, hermes_command, run_real_hermes

pytestmark = pytest.mark.e2e

SCRIPT = [
    {
        "at_input_ms": 1500,
        "event": {"type": "delegation", "id": "c1", "tool_args": {"task": "how is my portfolio doing"}},
    },
    {
        "at_input_ms": 3500,
        "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "what's the weather tomorrow"}},
    },
    {
        "at_input_ms": 5500,
        "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "what are you working on"}},
    },
]
GENERAL_MODEL = "general-default"
"""The general profile's default model in ``tests/fixtures/hermes_home/profiles/general/config.yaml``."""

DOCTOR_ENV = {
    "DUPLEX_E2E_KEY_GENERAL": KEYS["general"],
    "DUPLEX_E2E_KEY_STOCKS": KEYS["stock-picker"],
    "DUPLEX_E2E_TOKEN_ROB": TOKENS["rob"],
    "DUPLEX_E2E_TOKEN_ALEX": TOKENS["alex"],
}


def _use_script(home: Path) -> None:
    script = home / "topic-switch.jsonl"
    script.write_text("".join(json.dumps(step) + "\n" for step in SCRIPT), encoding="utf-8")
    duplex = home / "duplex.yaml"
    config = yaml.safe_load(duplex.read_text(encoding="utf-8"))
    config["provider"]["script"] = str(script)
    duplex.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


@pytest.fixture(scope="module")
def hermes() -> Iterator[RealHermes]:
    with run_real_hermes(hermes_command(), configure=_use_script) as running:
        home, api_base = running.home, running.api_base
        yield running
    assert not home.exists()
    with pytest.raises((URLError, OSError)):
        urllib.request.urlopen(f"{api_base}/p/general/v1/models", timeout=2)


@asynccontextmanager
async def voice_call(hermes: RealHermes, person: str) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(f"{hermes.web_url}/ws", protocols=("bearer", TOKENS[person])) as socket,
    ):
        yield socket


async def speak(socket: aiohttp.ClientWebSocketResponse, duration_ms: int) -> None:
    """Send ``duration_ms`` of a 440 Hz sine at about −20 dBFS in one message."""
    t = np.arange(duration_ms * RATE // 1000) / RATE
    await socket.send_bytes((3277.0 * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes())


async def provider_calls(hermes: RealHermes, method: str, count: int, timeout_s: float = 60.0) -> list[Any]:
    async with asyncio.timeout(timeout_s):
        while len(calls := hermes.provider_calls(method)) < count:
            await asyncio.sleep(0.1)
    return calls


def doctor(config: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``hermes-duplex doctor --config config`` with only ``env``'s secrets set."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "DUPLEX_"))}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_duplex.cli import main; main()",
            "doctor",
            "--config",
            str(config),
        ],
        cwd=REPO,
        env=clean | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def test_both_topics_run_on_the_default_profile_and_status_stays_local(hermes: RealHermes) -> None:
    async with voice_call(hermes, "rob") as socket:
        await speak(socket, 2000)
        await provider_calls(hermes, "deliver_result", 1)
        await speak(socket, 2000)
        results = await provider_calls(hermes, "deliver_result", 2)
        llm_calls = len(hermes.llm_requests())
        said_before = len(hermes.provider_calls("say"))
        await speak(socket, 2000)
        said = (await provider_calls(hermes, "say", said_before + 1))[said_before:]
        await asyncio.sleep(1)  # time for a wrongly dispatched run to show up

    tasks = list(hermes.tasks().values())
    assert [(t["person"], t["profile"], t["text"], t["state"]) for t in tasks] == [
        ("rob", "general", "how is my portfolio doing", "completed"),
        ("rob", "general", "what's the weather tomorrow", "completed"),
    ]
    assert hermes.route_decisions()[:2] == [("rob", "general")] * 2

    for task in tasks:
        assert task["model"] is None
        status = hermes.run_status("general", task["run_id"])
        assert status["status"] == "completed"
        assert status["session_id"] == "duplex-rob-general"
        assert status["output"] == f"{GENERAL_MODEL} says: done"
    assert GENERAL_MODEL in {request["model"] for request in hermes.llm_requests()}

    assert results[1]["text"] == frame_result("general", f"{GENERAL_MODEL} says: done", RESULT_CAP_TOKENS)
    assert [call["text"] for call in said] == ["You have no tasks running."]
    assert len(hermes.llm_requests()) == llm_calls


def test_doctor_passes_against_the_gateway_and_names_what_fails(hermes: RealHermes, tmp_path: Path) -> None:
    config = hermes.home / "duplex.yaml"

    passed = doctor(config, DOCTOR_ENV)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    rows = passed.stdout.splitlines()
    assert any(row.startswith("hermes profile general") and "pass" in row for row in rows)
    assert any(row.startswith("hermes profile stock-picker") and "pass" in row for row in rows)
    assert "FAIL" not in passed.stdout

    wrong_key = "wrong-stocks-key-0123456789"
    rejected = doctor(config, DOCTOR_ENV | {"DUPLEX_E2E_KEY_STOCKS": wrong_key})
    assert rejected.returncode == 1
    [row] = [row for row in rejected.stdout.splitlines() if row.startswith("hermes profile stock-picker")]
    assert "FAIL" in row and "DUPLEX_E2E_KEY_STOCKS" in row and "HTTP 401" in row
    assert wrong_key not in rejected.stdout + rejected.stderr
    assert KEYS["general"] not in rejected.stdout + rejected.stderr

    offline = yaml.safe_load(config.read_text(encoding="utf-8"))
    offline["hermes"]["api_base"] = "http://127.0.0.1:9"
    offline_config = tmp_path / "duplex.yaml"
    offline_config.write_text(yaml.safe_dump(offline), encoding="utf-8")
    started = time.monotonic()
    unreachable = doctor(offline_config, DOCTOR_ENV)
    assert unreachable.returncode == 1
    assert "cannot reach http://127.0.0.1:9/p/general/v1/models" in unreachable.stdout
    assert time.monotonic() - started < 30
