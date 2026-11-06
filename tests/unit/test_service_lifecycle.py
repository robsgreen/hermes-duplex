"""DuplexService lifecycle: standalone startup and shutdown through the CLI, and the 5 s stop bound."""

import argparse
import asyncio
import json
import os
import signal
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
import yaml
from aiohttp import web

from hermes_duplex import cli, service
from hermes_duplex.config import Config, ContextKind, ProviderConfig, Secrets, load_config
from hermes_duplex.hermes_context import HermesContextFiles
from hermes_duplex.prompts.base import PromptContext
from hermes_duplex.providers.base import RealtimeProvider
from hermes_duplex.providers.fake.adapter import FakeProvider
from hermes_duplex.router.base import RouterBackend, RouteRequest
from hermes_duplex.router.decisions import DecisionsBackend
from hermes_duplex.router.factory import build_backends
from hermes_duplex.router.knn import Embedder
from hermes_duplex.service import STOP_TIMEOUT_S, DuplexService, build_service
from hermes_duplex.tasks import Task, TaskTracker
from hermes_duplex.transports.discord.bot import DiscordTransport
from hermes_duplex.transports.web.server import WebTransport
from tests.fakes.fake_embedder import fake_embed

ENV = {"TOKEN_ROB": "rob-web-token", "BOT_TOKEN": "discord-bot-token", "DECISIONS_KEY": "sk-decisions"}


def _config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "hermes": {"api_base": "http://127.0.0.1:9"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {"description": "Anything", "examples": ["what's the weather"]}},
        "people": {"rob": {"web_token_env": "TOKEN_ROB", "profiles": ["*"]}},
        "transports": {"web": {"enabled": True, "port": 0}},
        **overrides,
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={**ENV, "HERMES_HOME": str(tmp_path)})


def _web_url(duplex: DuplexService, path: str) -> str:
    [web] = [t for t in duplex.transports if isinstance(t, WebTransport)]
    return f"http://127.0.0.1:{web.port}{path}"


async def _wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.01)


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    cli.add_commands(parser)
    return parser.parse_args(argv)


async def test_serve_runs_until_sigterm_then_stops(tmp_path: Path) -> None:
    built: list[DuplexService] = []

    def build(config: Config) -> DuplexService:
        built.append(build_service(config, embed=fake_embed))
        return built[0]

    serving = asyncio.create_task(cli.serve(_config(tmp_path), build=build))
    await _wait_until(lambda: bool(built) and _listening(built[0]))
    url = _web_url(built[0], "/")
    async with aiohttp.ClientSession() as http:
        async with http.get(url) as page:
            assert page.status == 200

        started = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, STOP_TIMEOUT_S)
        assert time.monotonic() - started < STOP_TIMEOUT_S

        with pytest.raises(aiohttp.ClientConnectionError):
            await http.get(url)


def _listening(duplex: DuplexService) -> bool:
    try:
        _web_url(duplex, "/")
    except RuntimeError:  # the web transport has not bound its port yet
        return False
    return True


def test_serve_command_reports_a_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.yaml"

    assert cli.run(_parse("serve", "--config", str(missing))) == 1
    assert f"{missing}: config file not found" in capsys.readouterr().err


def test_no_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(_parse()) == 2
    assert capsys.readouterr().err.startswith("hermes-duplex: ")


def test_prompt_context_comes_from_the_assistant_section_and_the_profiles(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        assistant={"name": "Iris", "persona": "Keep it brisk.", "backchannel": "none"},
        profiles={"general": {"description": "Anything"}, "notes": {}},
    )

    assert service.prompt_context(config) == PromptContext(
        assistant_name="Iris",
        persona="Keep it brisk.",
        backchannel="none",
        profiles=(("general", "Anything"), ("notes", "")),
    )


def _context_config(tmp_path: Path) -> Config:
    """rob, the Hermes owner, and alex, with every Hermes context text on, and each file on disk."""
    for relative, text in (
        ("profiles/general/SOUL.md", "The general agent handles anything."),
        ("profiles/general/memories/USER.md", "Rob lives in Lisbon."),
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    return _config(
        tmp_path,
        hermes={"api_base": "http://127.0.0.1:9", "voice_context_files": ["soul", "user", "briefing"]},
        people={
            "rob": {"web_token_env": "TOKEN_ROB", "profiles": ["*"], "hermes_owner": True},
            "alex": {"profiles": ["*"], "voice_context": "Alex is Rob's partner."},
        },
    )


async def _with_briefing(config: Config) -> HermesContextFiles:
    """The config's context files, with a briefing for general cached."""

    async def write(profile: str, kind: ContextKind, source: str | None, max_chars: int) -> str:
        return "Rob is moving house."

    files = HermesContextFiles(
        config.hermes_home, config.hermes.voice_context_files, config.hermes.voice_context_max_chars, write
    )
    assert await files.refresh("general", "briefing") == "rewritten"
    return files


def _task(number: int, person: str, text: str) -> Task:
    moment = datetime(2026, 11, 6, tzinfo=UTC)
    return Task(
        number=number,
        uuid=f"u{number}",
        person=person,
        profile="general",
        effort="medium",
        model=None,
        text=text,
        state="running",
        run_id=None,
        created_at=moment,
        updated_at=moment,
    )


async def test_prompt_context_carries_the_owners_texts_only_while_they_are_alone(tmp_path: Path) -> None:
    config = _context_config(tmp_path)
    files = await _with_briefing(config)

    alone = service.prompt_context(config, "general", "rob", True, context_files=files)
    together = service.prompt_context(config, "general", "rob", False, context_files=files)

    assert alone.hermes_soul == together.hermes_soul == "The general agent handles anything."
    assert (alone.person, alone.hermes_user, alone.hermes_briefing) == (
        "rob",
        "Rob lives in Lisbon.",
        "Rob is moving house.",
    )
    assert (together.person, together.hermes_user, together.hermes_briefing) == (None, None, None)


async def test_prompt_context_never_gives_the_owners_texts_to_anyone_else(tmp_path: Path) -> None:
    config = _context_config(tmp_path)
    files = await _with_briefing(config)

    alex = service.prompt_context(config, "general", "alex", True, context_files=files)

    assert (alex.person, alex.person_voice_context) == ("alex", "Alex is Rob's partner.")
    assert (alex.hermes_user, alex.hermes_briefing) == (None, None)


def test_prompt_context_lists_the_newest_open_tasks_by_topic_and_profile(tmp_path: Path) -> None:
    config = _context_config(tmp_path)
    tasks = [_task(n, "rob", f"please check item {n}") for n in range(1, 13)]

    context = service.prompt_context(config, "general", "rob", False, open_tasks=tasks)

    assert context.person == "rob"
    assert context.open_tasks == tuple((f"check item {n}", "general") for n in range(3, 13))
    assert service.prompt_context(config, "general", None, False, open_tasks=tasks).person is None


class _HangingCloseProvider(FakeProvider):
    """A fake provider whose ``close`` never returns on its own."""

    def __init__(self) -> None:
        super().__init__()
        self.closing = asyncio.Event()

    async def close(self) -> None:
        self.closing.set()
        await asyncio.Event().wait()


@pytest.fixture
def hanging_providers(monkeypatch: pytest.MonkeyPatch) -> list[_HangingCloseProvider]:
    """Make ``build_service`` give every conversation a provider whose ``close`` hangs."""
    providers: list[_HangingCloseProvider] = []

    def factory(config: ProviderConfig, secrets: Secrets, prompt_context: PromptContext) -> RealtimeProvider:
        providers.append(_HangingCloseProvider())
        return providers[-1]

    monkeypatch.setattr(service, "provider_factory", lambda name: factory)
    return providers


@pytest.fixture
async def started(tmp_path: Path) -> AsyncIterator[DuplexService]:
    duplex = build_service(_config(tmp_path), embed=fake_embed)
    await duplex.start()
    yield duplex
    await duplex.stop()


async def test_stop_finishes_within_5_s_when_a_provider_close_hangs(
    hanging_providers: list[_HangingCloseProvider], started: DuplexService, caplog: pytest.LogCaptureFixture
) -> None:
    async with aiohttp.ClientSession() as http:
        socket = await http.ws_connect(_web_url(started, "/ws"), protocols=("bearer", ENV["TOKEN_ROB"]))
        await _wait_until(lambda: bool(started.status().conversations))
        url = _web_url(started, "/")

        begun = time.monotonic()
        await started.stop()
        elapsed = time.monotonic() - begun

        assert elapsed < STOP_TIMEOUT_S + 0.5
        [provider] = hanging_providers
        assert provider.closing.is_set()
        assert "did not stop within" in caplog.text
        assert started.status().conversations == ()
        while (message := await socket.receive(timeout=1)).type == aiohttp.WSMsgType.TEXT:
            pass  # the status the page got before the stop
        assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        with pytest.raises(aiohttp.ClientConnectionError):
            await http.get(url)


async def test_status_lists_active_conversations(started: DuplexService) -> None:
    assert started.status().conversations == ()

    url = _web_url(started, "/ws")
    async with aiohttp.ClientSession() as http, http.ws_connect(url, protocols=("bearer", ENV["TOKEN_ROB"])):
        await _wait_until(lambda: bool(started.status().conversations))
        status = started.status()

    assert status.provider == "fake"
    assert [conversation_id.startswith("web-rob-") for conversation_id in status.conversations] == [True]
    assert status.tasks == ()


async def test_status_shows_each_persons_active_profile_and_the_transports_switch_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path,
        profiles={"general": {}, "notes": {"aliases": ["my notes"]}},
        people={
            "rob": {"web_token_env": "TOKEN_ROB", "discord_id": "123", "profiles": ["*"]},
            "alex": {"profiles": ["general"]},
        },
        transports={
            "discord": {"enabled": True, "bot_token_env": "BOT_TOKEN"},
            "web": {"enabled": True, "port": 0},
        },
    )
    duplex = build_service(config, embed=fake_embed)
    bot, web_transport = duplex.transports
    assert isinstance(bot, DiscordTransport) and isinstance(web_transport, WebTransport)
    for name in ("login", "connect", "close"):
        monkeypatch.setattr(bot.client, name, AsyncMock())
    monkeypatch.setattr(bot.commands, "sync", AsyncMock())
    await duplex.start()
    try:
        assert duplex.status().active_profiles == {"rob": "general", "alex": "general"}

        # The bot and the page share the service's store: an alias from Discord moves rob's page.
        url = _web_url(duplex, "/ws")
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(url, protocols=("bearer", ENV["TOKEN_ROB"])) as page,
        ):
            assert json.loads((await page.receive(timeout=1)).data)["active_profile"] == "general"
            rob = cast(discord.User, discord.Object(123))
            assert "now talking to notes" in await bot.switch_profile(rob, "my notes")
            assert json.loads((await page.receive(timeout=1)).data)["active_profile"] == "notes"

        assert duplex.status().active_profiles == {"rob": "notes", "alex": "general"}
    finally:
        await duplex.stop()


async def test_service_runs_the_discord_bot_and_disconnects_it_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path, transports={"discord": {"enabled": True, "bot_token_env": "BOT_TOKEN"}, "web": {}}
    )
    duplex = build_service(config, embed=fake_embed)
    [bot] = duplex.transports
    assert isinstance(bot, DiscordTransport)
    monkeypatch.setattr(bot.client, "login", AsyncMock())
    monkeypatch.setattr(bot.client, "connect", AsyncMock())
    monkeypatch.setattr(bot.client, "close", AsyncMock())
    monkeypatch.setattr(bot.commands, "sync", AsyncMock())

    await duplex.start()
    begun = time.monotonic()
    await duplex.stop()

    assert time.monotonic() - begun < STOP_TIMEOUT_S
    cast(AsyncMock, bot.client.login).assert_awaited_once_with(ENV["BOT_TOKEN"])
    cast(AsyncMock, bot.client.close).assert_awaited_once()


def _decisions_reply(request_body: dict[str, Any]) -> dict[str, Any]:
    """A Decisions API reply that picks the first choice of every choice question."""
    answers: list[dict[str, Any]] = []
    for question in request_body["questions"]:
        if question["type"] == "choice":
            values = [choice["value"] for choice in question["choices"]]
            probabilities = [
                {"value": value, "probability": 1.0 if value == values[0] else 0.0} for value in values
            ]
            answers.append({"type": "choice", "name": question["name"], "probabilities": probabilities})
        else:
            answers.append({"type": "predicate", "name": question["name"], "probability": 0.0})
    return {"answers": answers}


@pytest.fixture
async def decisions_api_base() -> AsyncIterator[str]:
    """A local fake of the Decisions API's ``POST /v1/decisions``; yields its base URL."""

    async def decide(request: web.Request) -> web.Response:
        return web.json_response(_decisions_reply(await request.json()))

    app = web.Application()
    app.add_routes([web.post("/v1/decisions", decide)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
    await runner.cleanup()


async def test_service_with_the_decisions_backend_starts_and_closes_it_on_stop(
    tmp_path: Path, decisions_api_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[RouterBackend] = []

    def recording_build_backends(config: Config, *, embed: Embedder | None = None) -> list[RouterBackend]:
        built.extend(build_backends(config, embed=embed))
        return built

    monkeypatch.setattr(service, "build_backends", recording_build_backends)
    router = {
        "backend": "decisions",
        "decisions": {"api_key_env": "DECISIONS_KEY", "base_url": decisions_api_base},
    }
    duplex = build_service(_config(tmp_path, router=router))
    [decisions] = built
    assert isinstance(decisions, DecisionsBackend)
    closed = AsyncMock(wraps=decisions.close)
    monkeypatch.setattr(decisions, "close", closed)

    await duplex.start()
    result = await decisions.classify(
        RouteRequest("what's the weather", "rob", history=(), active_profile="general"), 2000
    )
    assert result.profile_probs["general"] == 1.0
    await duplex.stop()

    closed.assert_awaited_once()


async def test_start_restores_the_tasks_a_previous_run_left_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = TaskTracker(config.hermes_home)
    task = await previous.create(person="rob", profile="general", effort="medium", model=None, text="hi")
    await previous.transition(task.number, "running", run_id="run_1")
    await previous.close()

    duplex = build_service(config, embed=fake_embed)
    await duplex.start()
    try:
        assert [(task.number, task.run_id, task.state) for task in duplex.status().tasks] == [
            (1, "run_1", "running")
        ]
    finally:
        await duplex.stop()


async def test_start_leaves_out_open_tasks_older_than_the_configured_age(tmp_path: Path) -> None:
    config = _config(tmp_path, tasks={"restore_max_age_h": 0.0001})  # 0.36 s
    previous = TaskTracker(config.hermes_home)
    task = await previous.create(person="rob", profile="general", effort="medium", model=None, text="hi")
    await previous.transition(task.number, "running", run_id="run_1")
    await previous.close()
    await asyncio.sleep(0.5)

    duplex = build_service(config, embed=fake_embed)
    await duplex.start()
    try:
        assert duplex.status().tasks == ()
    finally:
        await duplex.stop()


async def test_start_goes_on_when_the_task_history_is_torn(tmp_path: Path) -> None:
    config = _config(tmp_path)
    history = config.hermes_home / "duplex" / "tasks.jsonl"
    history.parent.mkdir(parents=True)
    history.write_bytes(b'{"task": 1, "text": "caf\xc3')

    duplex = build_service(config, embed=fake_embed)
    await duplex.start()
    try:
        assert duplex.status().tasks == ()
    finally:
        await duplex.stop()


async def test_start_loads_each_persons_saved_active_profile(tmp_path: Path) -> None:
    profiles = {"general": {"description": "Anything"}, "stock-picker": {"description": "Portfolio"}}
    config = _config(tmp_path, profiles=profiles)
    saved = config.hermes_home / "duplex" / "active_profiles.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"rob": "stock-picker"}), encoding="utf-8")

    duplex = build_service(config, embed=fake_embed)
    assert duplex._active_profiles.get("rob") == "general", "nothing is read before start"
    await duplex.start()
    try:
        assert duplex._active_profiles.get("rob") == "stock-picker"
    finally:
        await duplex.stop()
