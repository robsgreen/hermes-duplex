"""``doctor``: checks that a config can run, and prints one pass/fail row per check.

The checks: the config loads; every env var it names is set; each profile's Hermes API server
answers ``GET {api_base}/p/<profile>/v1/models`` with that profile's key; the provider accepts its
API key on a cheap call; and, when Discord is enabled, Discord accepts the bot token. A check that
needs a secret which is not set fails without calling anything. No row ever holds a secret value:
details are built from env var names, config keys, HTTP status codes, and exception type names.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import Config, ConfigError, MissingEnvError, load_config

PROVIDER_AUTH_URLS: Mapping[str, str] = {
    "gpt-live": "https://api.openai.com/v1/models",
    "openai-realtime": "https://api.openai.com/v1/models",
    "xai-realtime": "https://api.x.ai/v1/models",
}
"""Provider name → the URL whose ``GET`` with the provider key as bearer token checks the key. The
``fake`` provider has no key and is not checked."""

REQUEST_TIMEOUT_S = 10.0
"""How long each network check waits before it fails."""

type DiscordLogin = Callable[[str], Awaitable[None]]
"""Logs in to Discord with a bot token and disconnects; raises ``discord.LoginFailure`` when Discord
rejects the token."""


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check."""

    check: str
    """What was checked, e.g. ``hermes profile general``."""

    passed: bool

    detail: str
    """Why it passed or failed; never a secret value."""


async def discord_login(token: str) -> None:
    """Log in to Discord as the bot ``token`` names, then close the connection."""
    # Imported here: discord.py is slow to import and only this check needs it.
    import discord

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
    finally:
        await client.close()


async def diagnose(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    provider_auth_urls: Mapping[str, str] = PROVIDER_AUTH_URLS,
    login: DiscordLogin = discord_login,
) -> list[CheckResult]:
    """Run every check against the config at ``config_path`` (resolved like ``load_config``) and
    return the results in a fixed order: config, env vars, Hermes profiles, provider, Discord.

    ``env`` (default ``os.environ``) supplies the secrets. ``provider_auth_urls`` and ``login``
    replace the provider key check URLs and the Discord login (tests pass fakes). An invalid config
    yields only the failed config row. The network checks run at once, each bounded by
    ``REQUEST_TIMEOUT_S``. Never raises for a failed check.
    """
    try:
        config = load_config(config_path, env=env)
        missing: tuple[tuple[str, str], ...] = ()
    except MissingEnvError as error:
        config, missing = error.config, error.missing
    except ConfigError as error:
        return [CheckResult("config", False, str(error))]

    results = [CheckResult("config", True, str(config.path)), *_env_results(config, missing)]
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        checks = [_check_profile(http, config, profile) for profile in config.profiles]
        if config.provider.name in provider_auth_urls:
            checks.append(_check_provider(http, config, provider_auth_urls[config.provider.name]))
        if config.transports.discord.enabled:
            checks.append(_check_discord(config, login))
        results.extend(await asyncio.gather(*checks))
    return results


def format_table(results: list[CheckResult]) -> str:
    """The results as a plain-text table with a CHECK, RESULT, and DETAIL column."""
    rows = [("CHECK", "RESULT", "DETAIL")] + [
        (result.check, "pass" if result.passed else "FAIL", result.detail) for result in results
    ]
    width = max(len(check) for check, _, _ in rows)
    return "\n".join(f"{check:<{width}}  {outcome:<6}  {detail}".rstrip() for check, outcome, detail in rows)


def _env_results(config: Config, missing: tuple[tuple[str, str], ...]) -> list[CheckResult]:
    """One row per env var config names: the set ones, then the unset ones with the key naming each."""
    results = [CheckResult(f"env {name}", True, "set") for name in config.secrets.names()]
    unset: dict[str, str] = {}
    for key, name in missing:
        unset.setdefault(name, key)
    results.extend(
        CheckResult(f"env {name}", False, f"not set or empty ({key})") for name, key in unset.items()
    )
    return results


async def _check_profile(http: aiohttp.ClientSession, config: Config, profile: str) -> CheckResult:
    check = f"hermes profile {profile}"
    env_name = config.hermes.profile_keys_env.get(profile)
    if env_name is None:
        return CheckResult(check, False, f"no key configured (hermes.profile_keys_env.{profile})")
    url = f"{config.hermes.api_base}/p/{quote(profile, safe='')}/v1/models"
    return await _check_bearer(http, check, url, _secret(config, env_name), env_name)


async def _check_provider(http: aiohttp.ClientSession, config: Config, url: str) -> CheckResult:
    check = f"provider {config.provider.name}"
    env_name = config.provider.api_key_env
    assert env_name is not None, "config loading requires api_key_env for every provider but fake"
    return await _check_bearer(http, check, url, _secret(config, env_name), env_name)


async def _check_bearer(
    http: aiohttp.ClientSession, check: str, url: str, key: str | None, env_name: str
) -> CheckResult:
    """``GET url`` with ``key`` as bearer token: passes on HTTP 200."""
    if key is None:
        return CheckResult(check, False, f"not checked: {env_name} is not set")
    try:
        async with http.get(url, headers={"Authorization": f"Bearer {key}"}) as resp:
            status = resp.status
    except TimeoutError:
        return CheckResult(check, False, f"no answer from {url} within {REQUEST_TIMEOUT_S:g} s")
    except aiohttp.ClientError as error:
        return CheckResult(check, False, f"cannot reach {url} ({type(error).__name__})")
    if status == 200:
        return CheckResult(check, True, f"{url} answered HTTP 200")
    if status in (401, 403):
        return CheckResult(check, False, f"{url} rejected the key in {env_name} (HTTP {status})")
    return CheckResult(check, False, f"{url} answered HTTP {status}")


async def _check_discord(config: Config, login: DiscordLogin) -> CheckResult:
    check = "discord login"
    env_name = config.transports.discord.bot_token_env
    assert env_name is not None, "config loading requires bot_token_env when Discord is enabled"
    token = _secret(config, env_name)
    if token is None:
        return CheckResult(check, False, f"not checked: {env_name} is not set")
    # Imported here like in ``discord_login``.
    import discord

    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            await login(token)
    except discord.LoginFailure:
        return CheckResult(check, False, f"Discord rejected the token in {env_name}")
    except TimeoutError:
        return CheckResult(check, False, f"no answer from Discord within {REQUEST_TIMEOUT_S:g} s")
    except (discord.DiscordException, aiohttp.ClientError, OSError) as error:
        return CheckResult(check, False, f"cannot log in to Discord ({type(error).__name__})")
    return CheckResult(check, True, "logged in")


def _secret(config: Config, env_name: str) -> str | None:
    """The value of ``env_name``, or None when it is not set."""
    try:
        return config.secrets.get(env_name)
    except KeyError:
        return None
