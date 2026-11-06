"""Against a real Hermes gateway with the plugin installed: Hermes rewrites a profile's ``SOUL.md`` for
voice on that profile, the plugin caches the rewrite, and a call's instructions carry the cached text,
hand edits included.

The test writes stock-picker's ``SOUL.md`` (Hermes seeds general's); rob starts on stock-picker,
and the fake provider replays no events. The fake LLM answers the rewrite with stock-picker's default
model's reply.
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml

from tests.e2e.real_hermes import TOKENS, RealHermes, hermes_command, run_real_hermes

pytestmark = pytest.mark.e2e

SOUL = "SOUL-ORIGINAL: you are the stock-picker agent; you manage Rob's portfolio.\n"
REWRITE = "stock-picker-default says: done"
"""What the fake LLM answers stock-picker's default model with, and so the rewrite."""


def _configure(home: Path) -> None:
    soul = home / "profiles" / "stock-picker" / "SOUL.md"
    soul.write_text(SOUL, encoding="utf-8")
    script = home / "no-events.jsonl"
    script.write_text("", encoding="utf-8")
    duplex = home / "duplex.yaml"
    config = yaml.safe_load(duplex.read_text(encoding="utf-8"))
    config["hermes"]["voice_context_files"] = ["soul"]
    config["provider"]["script"] = str(script)
    config["people"]["rob"]["default_profile"] = "stock-picker"
    duplex.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


@pytest.fixture(scope="module")
def hermes() -> Iterator[RealHermes]:
    with run_real_hermes(hermes_command(), configure=_configure) as running:
        yield running


@asynccontextmanager
async def voice_call(hermes: RealHermes, person: str) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(f"{hermes.web_url}/ws", protocols=("bearer", TOKENS[person])) as socket,
    ):
        yield socket


def _last_user(request: dict[str, Any]) -> str:
    """The last user message of a chat completion request, as JSON."""
    users = [message for message in request["messages"] if message.get("role") == "user"]
    return json.dumps(users[-1]) if users else ""


async def connects(hermes: RealHermes, count: int, timeout_s: float = 30.0) -> list[dict[str, Any]]:
    async with asyncio.timeout(timeout_s):
        while len(calls := hermes.provider_calls("connect")) < count:
            await asyncio.sleep(0.1)
    return calls


async def test_the_soul_is_rewritten_on_its_own_profile_cached_and_used_with_hand_edits(
    hermes: RealHermes,
) -> None:
    cache = hermes.home / "duplex" / "voice_context" / "stock-picker.soul.md"
    async with asyncio.timeout(60):  # the service starts the rewrite in the background when it starts
        while not cache.with_suffix(".json").exists():
            await asyncio.sleep(0.2)

    assert cache.read_text(encoding="utf-8") == REWRITE
    sidecar = json.loads(cache.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["source_sha256"] == hashlib.sha256(SOUL.encode()).hexdigest()
    # The rewrite ran on stock-picker (its default model), with the file sent as data. general's
    # SOUL.md, which Hermes seeds, is rewritten on general.
    requests = [(request["model"], _last_user(request)) for request in hermes.llm_requests()]
    ours = {model for model, user in requests if "<document>\\nSOUL-ORIGINAL" in user}
    others = {model for model, user in requests if "<document>" in user and "SOUL-ORIGINAL" not in user}
    assert ours == {"stock-picker-default"}
    assert others <= {"general-default"}

    async with voice_call(hermes, "rob"):
        [first] = await connects(hermes, 1)
    assert f"<agent_background>\n{REWRITE}\n</agent_background>" in first["instructions"]
    assert "SOUL-ORIGINAL" not in first["instructions"]

    cache.write_text("Rob's own words: stock-picker trades only on his say-so.", encoding="utf-8")
    async with voice_call(hermes, "rob"):
        [_, second] = await connects(hermes, 2)
    assert "Rob's own words: stock-picker trades only on his say-so." in second["instructions"]
    assert cache.read_text(encoding="utf-8").startswith("Rob's own words")
