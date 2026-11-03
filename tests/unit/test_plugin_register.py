"""register(ctx): what the plugin registers with Hermes, and the gateway adapter that runs the service."""

import argparse
import asyncio
import functools
import logging
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml

from hermes_duplex import service
from hermes_duplex.plugin import GatewayAdapter, register
from tests.fakes.fake_embedder import fake_embed

DOCUMENTED_CTX_METHODS = frozenset(
    {
        "register_command",
        "register_cli_command",
        "register_hook",
        "register_platform",
        "spawn_task",
        "on_unload",
        "get_config",
    }
)


class RecordingContext:
    """A plugin ``ctx`` offering only the documented methods; any other attribute raises
    ``AttributeError``, as Hermes's validation probe does for names its context lacks."""

    def __init__(self, refuse_platform: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._refuse_platform = refuse_platform

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name not in DOCUMENTED_CTX_METHODS:
            raise AttributeError(name)

        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))
            if name == "register_platform" and self._refuse_platform:
                raise RuntimeError("ctx.register_platform() cannot run in the plugin host")

        return record

    def methods(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def call(self, method: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        [(args, kwargs)] = [(args, kwargs) for name, args, kwargs in self.calls if name == method]
        return args, kwargs


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DUPLEX_CONFIG", raising=False)
    return tmp_path


def _write_config(hermes_home: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    monkeypatch.setenv("DUPLEX_TEST_TOKEN_ROB", "rob-web-token")
    raw: dict[str, Any] = {
        "hermes": {"api_base": "http://127.0.0.1:9"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {"description": "Anything", "examples": ["what's the weather"]}},
        "people": {"rob": {"web_token_env": "DUPLEX_TEST_TOKEN_ROB", "profiles": ["*"]}},
        "transports": {"web": {"enabled": True, "port": 0}},
        **overrides,
    }
    (hermes_home / "duplex.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")


@pytest.fixture
def fake_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the plugin's service use the deterministic embedder instead of loading a model."""
    monkeypatch.setattr(service, "build_service", functools.partial(service.build_service, embed=fake_embed))


def test_without_config_registers_the_commands_warns_once_and_starts_nothing(
    hermes_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = RecordingContext()

    register(ctx)

    assert ctx.methods() == ["register_command", "register_cli_command"]
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.getMessage() for record in warnings] == [
        f"hermes-duplex: no config at {hermes_home / 'duplex.yaml'}; the voice service will not start"
    ]
    (_, handler), _ = ctx.call("register_command")
    assert "not running" in handler("")


def test_with_config_registers_the_gateway_platform_and_unload(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(hermes_home, monkeypatch)
    ctx = RecordingContext()

    register(ctx)

    assert ctx.methods() == ["register_command", "register_cli_command", "register_platform", "on_unload"]
    assert set(ctx.methods()) <= DOCUMENTED_CTX_METHODS
    (name, _), _ = ctx.call("register_command")
    assert name == "duplex"
    _, platform = ctx.call("register_platform")
    assert platform["name"] == "duplex"
    assert platform["check_fn"]() is True
    assert platform["is_connected"](None) is True


def test_a_refused_platform_does_not_raise(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_config(hermes_home, monkeypatch)
    ctx = RecordingContext(refuse_platform=True)

    register(ctx)

    assert "on_unload" not in ctx.methods()
    assert "use `hermes duplex serve`" in caplog.text


def test_cli_command_parses_serve_and_runs_the_shared_handler(hermes_home: Path) -> None:
    ctx = RecordingContext()
    register(ctx)
    (name,), command = ctx.call("register_cli_command")
    parser = argparse.ArgumentParser(prog="hermes duplex")
    command["setup_fn"](parser)

    assert name == "duplex"
    assert parser.parse_args(["serve", "--config", "x.yaml"]).config == "x.yaml"
    assert command["handler_fn"](parser.parse_args(["doctor"])) == 1  # no config in this HERMES_HOME


def _adapter(ctx: RecordingContext) -> GatewayAdapter:
    _, platform = ctx.call("register_platform")
    return platform["adapter_factory"](object())


def _status_text(ctx: RecordingContext) -> str:
    (_, handler), _ = ctx.call("register_command")
    return handler("")


async def test_adapter_connect_starts_the_service_and_disconnect_stops_it(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch, fake_embedding: None
) -> None:
    _write_config(hermes_home, monkeypatch)
    ctx = RecordingContext()
    register(ctx)
    adapter = _adapter(ctx)

    assert await adapter.connect() is True
    try:
        assert "running with provider fake" in _status_text(ctx)
        assert "No active conversations." in _status_text(ctx)
        assert adapter.platform.value == "duplex"
        assert not adapter.has_fatal_error
        assert not (await adapter.send("chat", "hello")).success
    finally:
        await adapter.disconnect()

    assert "not running" in _status_text(ctx)


async def test_unload_from_another_thread_stops_the_service(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch, fake_embedding: None
) -> None:
    port = _free_port()
    _write_config(hermes_home, monkeypatch, transports={"web": {"enabled": True, "port": port}})
    ctx = RecordingContext()
    register(ctx)
    assert await _adapter(ctx).connect() is True
    (unload,), _ = ctx.call("on_unload")

    await asyncio.to_thread(unload)

    assert "not running" in _status_text(ctx)
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.ClientConnectionError):
            await http.get(f"http://127.0.0.1:{port}/")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_adapter_reports_an_invalid_config_as_a_fatal_error(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(hermes_home, monkeypatch, provider={"name": "no-such-provider"})
    ctx = RecordingContext()
    register(ctx)
    adapter = _adapter(ctx)

    assert await adapter.connect() is False
    assert adapter.has_fatal_error
    assert adapter.fatal_error_code == "config_error"
    assert adapter.fatal_error_message is not None and "provider.name" in adapter.fatal_error_message
    assert adapter.fatal_error_retryable is False
    assert "not running" in _status_text(ctx)
