"""``doctor``: config, env var, Hermes profile, provider key, and Discord login checks, the table, and
the exit status, against local fakes only."""

import argparse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import discord
import pytest
import yaml
from aiohttp import web

from hermes_duplex import cli, doctor
from hermes_duplex.doctor import CheckResult, diagnose, format_table
from tests.fakes.fake_hermes_api import FakeHermesApi

ENV = {
    "HERMES_KEY_GENERAL": "general-key-0123456789",
    "HERMES_KEY_STOCKS": "stocks-key-0123456789",
    "OPENAI_API_KEY": "sk-provider-secret-value",
    "DUPLEX_TOKEN_ROB": "rob-web-token-value",
    "DUPLEX_DISCORD_TOKEN": "discord-bot-secret-value",
}
SECRETS = tuple(ENV.values())
UNREACHABLE = "http://127.0.0.1:9"


@pytest.fixture
async def hermes() -> AsyncIterator[FakeHermesApi]:
    keys = {"general": ENV["HERMES_KEY_GENERAL"], "stock-picker": ENV["HERMES_KEY_STOCKS"]}
    async with FakeHermesApi(keys) as fake:
        yield fake


@pytest.fixture
async def provider_auth_url() -> AsyncIterator[str]:
    """A fake of the provider's ``GET /v1/models``: 200 for the provider key, else 401."""

    async def models(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {ENV['OPENAI_API_KEY']}":
            return web.json_response({"error": {"code": "invalid_api_key"}}, status=401)
        return web.json_response({"object": "list", "data": []})

    app = web.Application()
    app.add_routes([web.get("/v1/models", models)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}/v1/models"
    await runner.cleanup()


class FakeDiscord:
    """Stands in for the Discord login: accepts only the configured bot token."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def login(self, token: str) -> None:
        self.tokens.append(token)
        if token != ENV["DUPLEX_DISCORD_TOKEN"]:
            raise discord.LoginFailure("Improper token has been passed.")


def _write_config(
    tmp_path: Path, api_base: str, *, provider: str = "gpt-live", discord_on: bool = True
) -> Path:
    raw: dict[str, Any] = {
        "hermes": {
            "api_base": api_base,
            "profile_keys_env": {"general": "HERMES_KEY_GENERAL", "stock-picker": "HERMES_KEY_STOCKS"},
        },
        "provider": {"name": provider, **({"api_key_env": "OPENAI_API_KEY"} if provider != "fake" else {})},
        "router": {"backend": "knn"},
        "profiles": {"general": {"description": "Anything"}, "stock-picker": {"description": "Stocks"}},
        "people": {"rob": {"web_token_env": "DUPLEX_TOKEN_ROB", "profiles": ["*"]}},
        "transports": {"discord": {"enabled": discord_on, "bot_token_env": "DUPLEX_DISCORD_TOKEN"}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


async def _diagnose(
    path: Path, provider_auth_url: str, env: dict[str, str] = ENV, fake_discord: FakeDiscord | None = None
) -> dict[str, CheckResult]:
    login = (fake_discord or FakeDiscord()).login
    results = await diagnose(path, env=env, provider_auth_urls={"gpt-live": provider_auth_url}, login=login)
    return {result.check: result for result in results}


def _failed(results: dict[str, CheckResult]) -> list[str]:
    return [check for check, result in results.items() if not result.passed]


async def test_every_check_passes_with_a_working_setup(
    tmp_path: Path, hermes: FakeHermesApi, provider_auth_url: str
) -> None:
    fake_discord = FakeDiscord()

    results = await _diagnose(
        _write_config(tmp_path, hermes.api_base), provider_auth_url, fake_discord=fake_discord
    )

    assert list(results) == [
        "config",
        "env HERMES_KEY_GENERAL",
        "env HERMES_KEY_STOCKS",
        "env OPENAI_API_KEY",
        "env DUPLEX_TOKEN_ROB",
        "env DUPLEX_DISCORD_TOKEN",
        "hermes profile general",
        "hermes profile stock-picker",
        "provider gpt-live",
        "discord login",
    ]
    assert _failed(results) == []
    assert [r.path for r in hermes.requests_to("GET", "/v1/models")] == [
        "/p/general/v1/models",
        "/p/stock-picker/v1/models",
    ]
    assert fake_discord.tokens == [ENV["DUPLEX_DISCORD_TOKEN"]]


async def test_a_missing_env_var_is_reported_and_its_check_is_not_run(
    tmp_path: Path, hermes: FakeHermesApi, provider_auth_url: str
) -> None:
    env = {name: value for name, value in ENV.items() if name != "HERMES_KEY_STOCKS"}

    results = await _diagnose(_write_config(tmp_path, hermes.api_base), provider_auth_url, env)

    assert _failed(results) == ["env HERMES_KEY_STOCKS", "hermes profile stock-picker"]
    assert (
        results["env HERMES_KEY_STOCKS"].detail == "not set or empty (hermes.profile_keys_env.stock-picker)"
    )
    assert "HERMES_KEY_STOCKS is not set" in results["hermes profile stock-picker"].detail
    assert hermes.requests_to("GET", "/p/stock-picker/v1/models") == []


async def test_a_wrong_profile_key_is_reported_as_rejected(
    tmp_path: Path, hermes: FakeHermesApi, provider_auth_url: str
) -> None:
    env = {**ENV, "HERMES_KEY_STOCKS": "not-the-stocks-key-0123"}

    results = await _diagnose(_write_config(tmp_path, hermes.api_base), provider_auth_url, env)

    assert _failed(results) == ["hermes profile stock-picker"]
    assert "rejected the key in HERMES_KEY_STOCKS (HTTP 401)" in results["hermes profile stock-picker"].detail


async def test_unreachable_hermes_fails_every_profile(tmp_path: Path, provider_auth_url: str) -> None:
    results = await _diagnose(_write_config(tmp_path, UNREACHABLE), provider_auth_url)

    assert _failed(results) == ["hermes profile general", "hermes profile stock-picker"]
    assert results["hermes profile general"].detail.startswith(
        f"cannot reach {UNREACHABLE}/p/general/v1/models"
    )


async def test_rejected_provider_key_and_discord_token_fail(
    tmp_path: Path, hermes: FakeHermesApi, provider_auth_url: str
) -> None:
    env = {**ENV, "OPENAI_API_KEY": "sk-wrong", "DUPLEX_DISCORD_TOKEN": "wrong-token"}

    results = await _diagnose(_write_config(tmp_path, hermes.api_base), provider_auth_url, env)

    assert _failed(results) == ["provider gpt-live", "discord login"]
    assert "rejected the key in OPENAI_API_KEY (HTTP 401)" in results["provider gpt-live"].detail
    assert results["discord login"].detail == "Discord rejected the token in DUPLEX_DISCORD_TOKEN"


async def test_fake_provider_and_disabled_discord_are_not_checked(
    tmp_path: Path, hermes: FakeHermesApi
) -> None:
    path = _write_config(tmp_path, hermes.api_base, provider="fake", discord_on=False)

    results = await diagnose(path, env=ENV)

    assert not {"provider fake", "discord login"} & {result.check for result in results}
    assert all(result.passed for result in results)


async def test_an_invalid_config_yields_only_the_failed_config_row(tmp_path: Path) -> None:
    [result] = await diagnose(_write_config(tmp_path, "ftp://nope"), env=ENV)

    assert result == CheckResult("config", False, "hermes.api_base: must be an http:// or https:// URL")


async def test_secrets_never_appear_in_the_table(
    tmp_path: Path, hermes: FakeHermesApi, provider_auth_url: str
) -> None:
    path = _write_config(tmp_path, hermes.api_base)
    passing = await diagnose(
        path, env=ENV, provider_auth_urls={"gpt-live": provider_auth_url}, login=FakeDiscord().login
    )
    wrong = {name: f"{value}-wrong" for name, value in ENV.items()}
    failing = await diagnose(
        path, env=wrong, provider_auth_urls={"gpt-live": provider_auth_url}, login=FakeDiscord().login
    )

    for table in (format_table(passing), format_table(failing)):
        for secret in (*SECRETS, *wrong.values()):
            assert secret not in table


def test_format_table_aligns_columns() -> None:
    table = format_table(
        [CheckResult("config", True, "duplex.yaml"), CheckResult("env A_LONGER_NAME", False, "x")]
    )

    assert table.splitlines() == [
        "CHECK              RESULT  DETAIL",
        "config             pass    duplex.yaml",
        "env A_LONGER_NAME  FAIL    x",
    ]


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    cli.add_commands(parser)
    return parser.parse_args(argv)


def test_doctor_command_exits_1_and_prints_failed_rows_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DUPLEX_TOKEN_ROB", ENV["DUPLEX_TOKEN_ROB"])
    # With both profile keys unset, no check makes a request.
    path = _write_config(tmp_path, UNREACHABLE, provider="fake", discord_on=False)

    assert cli.run(_parse("doctor", "--config", str(path))) == 1
    out = capsys.readouterr().out
    assert out.startswith("CHECK ")
    assert "env HERMES_KEY_GENERAL" in out
    assert "FAIL" in out
    assert ENV["DUPLEX_TOKEN_ROB"] not in out


def test_doctor_command_exits_0_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def all_pass(config_path: str | None) -> list[CheckResult]:
        return [CheckResult("config", True, str(config_path))]

    monkeypatch.setattr(doctor, "diagnose", all_pass)

    assert cli.run(_parse("doctor", "--config", "duplex.yaml")) == 0
    assert "config  pass    duplex.yaml" in capsys.readouterr().out
