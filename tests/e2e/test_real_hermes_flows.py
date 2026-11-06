"""Against a real Hermes gateway with the plugin installed: a switch suggestion answered yes, a
status request that never reaches Hermes, ``doctor`` checking the live API server, and an active
profile that outlives a restart.

The gateway runs its own provider script (written by the fixture): the portfolio question at 1.5 s of
input, a ``confirm_switch`` call that accepts at 3.5 s, and "what are you working on" at 5.5 s. The
knn router uses the real model2vec model, which suggests stock-picker for the portfolio question
asked on general; both profiles answer from ``tests/fakes/fake_llm.py``. The fixture also checks
that the gateway is gone, and its home removed, once it ends.
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

from hermes_duplex.prompts.notices import suggest_switch, switched_to
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.session import RESULT_CAP_TOKENS
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import KEYS, REPO, TOKENS, RealHermes, hermes_command, run_real_hermes

pytestmark = pytest.mark.e2e

PORTFOLIO = "how is my portfolio doing"
SCRIPT = [
    {
        "at_input_ms": 1500,
        "event": {"type": "delegation", "id": "c1", "tool_args": {"task": PORTFOLIO}},
    },
    {
        "at_input_ms": 3500,
        "event": {
            "type": "delegation",
            "id": "c2",
            "target": "confirm_switch",
            "tool_args": {"accept": True},
        },
    },
    {
        "at_input_ms": 5500,
        "event": {"type": "delegation", "id": "c3", "tool_args": {"task": "what are you working on"}},
    },
]

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


async def said_until(hermes: RealHermes, text: str, said_before: int, timeout_s: float = 60.0) -> list[str]:
    """Wait until the fake provider was told to say ``text`` after its first ``said_before`` say calls,
    and return everything it was told to say since then."""
    async with asyncio.timeout(timeout_s):
        while text not in (said := [call["text"] for call in hermes.provider_calls("say")[said_before:]]):
            await asyncio.sleep(0.1)
    return said


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


async def test_a_yes_to_the_switch_suggestion_runs_the_request_on_stock_picker_and_status_stays_local(
    hermes: RealHermes,
) -> None:
    assert hermes.active_profiles() == {}, "rob starts on his default, general"

    async with voice_call(hermes, "rob") as socket:
        await speak(socket, 2000)
        await said_until(hermes, suggest_switch("stock-picker"), 0)
        await speak(socket, 2000)
        results = await provider_calls(hermes, "deliver_result", 1)
        llm_calls = len(hermes.llm_requests())
        await speak(socket, 2000)
        said = await said_until(hermes, "You have no tasks running.", 0)
        await asyncio.sleep(1)  # time for a wrongly dispatched run to show up

    tasks = list(hermes.tasks().values())
    assert [(t["person"], t["profile"], t["text"], t["state"]) for t in tasks] == [
        ("rob", "stock-picker", PORTFOLIO, "completed"),
    ], "the held request runs once, on the profile rob said yes to"
    # Routed: the portfolio question on general (suggesting stock-picker) and the status request on
    # stock-picker. The yes never reaches the router.
    assert hermes.route_decisions() == [("rob", "general"), ("rob", "stock-picker")]
    assert hermes.active_profiles() == {"rob": "stock-picker"}

    reply = hermes.assert_ran_on_stock_picker(tasks[0])
    assert results[0]["text"] == frame_result("stock-picker", reply, RESULT_CAP_TOKENS)
    [confirmed] = [call for call in hermes.provider_calls("send_function_output") if call["call_id"] == "c2"]
    assert confirmed["payload"]["status"] == "switched"
    assert said == [
        suggest_switch("stock-picker"),
        switched_to("stock-picker"),
        f'Started "{PORTFOLIO}" on the stock-picker agent.',
        "You have no tasks running.",
    ]
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


async def test_rob_keeps_his_active_profile_across_a_restart(hermes: RealHermes) -> None:
    # The page's picker switches rob, unless the suggestion above already did.
    async with voice_call(hermes, "rob") as socket:
        await socket.send_str(json.dumps({"type": "profile", "name": "stock-picker"}))
        async with asyncio.timeout(10):
            while hermes.active_profiles().get("rob") != "stock-picker":
                await asyncio.sleep(0.1)

    hermes.restart()
    tasks_before = hermes.tasks().keys()
    decisions_before = len(hermes.route_decisions())
    said_before = len(hermes.provider_calls("say"))
    results_before = len(hermes.provider_calls("deliver_result"))

    async with voice_call(hermes, "rob") as socket:
        status = await socket.receive_json(timeout=10)
        await speak(socket, 2000)
        await provider_calls(hermes, "deliver_result", results_before + 1)

    assert status == {
        "type": "status",
        "active_profile": "stock-picker",
        "profiles": ["general", "stock-picker"],
    }
    [task] = [task for uuid, task in hermes.tasks().items() if uuid not in tasks_before]
    assert (task["person"], task["profile"], task["text"]) == ("rob", "stock-picker", PORTFOLIO)
    hermes.assert_ran_on_stock_picker(task)
    assert hermes.route_decisions()[decisions_before:] == [("rob", "stock-picker")]
    said = [call["text"] for call in hermes.provider_calls("say")[said_before:]]
    assert suggest_switch("stock-picker") not in said, "the request ran at once, without a question"
