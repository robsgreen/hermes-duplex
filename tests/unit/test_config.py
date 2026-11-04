"""Config loading: a full round trip, defaults, path resolution order, and every fail-closed case."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.config import (
    AssistantConfig,
    AudioConfig,
    ConfigError,
    ControlExamples,
    DecisionsConfig,
    DiscordConfig,
    HermesConfig,
    JevConfig,
    MissingEnvError,
    PersonConfig,
    ProfileConfig,
    ProviderConfig,
    RouterConfig,
    TasksConfig,
    TransportsConfig,
    WebConfig,
    load_config,
    resolve_config_path,
    resolve_hermes_home,
)
from hermes_duplex.providers.base import ModelChoice

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
REPO_ROOT = Path(__file__).resolve().parents[2]

ENV = {
    "HERMES_HOME": "/srv/hermes",
    "HERMES_KEY_GENERAL": "general-key-0123456789",
    "HERMES_KEY_STOCKS": "stocks-key-0123456789",
    "OPENAI_API_KEY": "sk-provider-secret",
    "DUPLEX_TOKEN_ROB": "rob-web-token",
    "DUPLEX_TOKEN_ALEX": "alex-web-token",
    "DUPLEX_DISCORD_TOKEN": "discord-bot-secret",
}

Mutation = Callable[[dict[str, Any]], None]


def _valid_raw() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "valid.yaml").read_text(encoding="utf-8"))


def _load_mutated(tmp_path: Path, mutate: Mutation, env: dict[str, str] = ENV) -> None:
    raw = _valid_raw()
    mutate(raw)
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    load_config(path, env=env)


def _assert_error(tmp_path: Path, mutate: Mutation, key: str, env: dict[str, str] = ENV) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        _load_mutated(tmp_path, mutate, env)
    assert caught.value.key == key
    assert str(caught.value).startswith(f"{key}: ")
    return caught.value


def test_valid_config_round_trips_into_dataclasses() -> None:
    config = load_config(FIXTURES / "valid.yaml", env=ENV)

    assert config.path == FIXTURES / "valid.yaml"
    assert config.hermes_home == Path("/srv/hermes")
    assert config.hermes == HermesConfig(
        api_base="http://127.0.0.1:8642",
        profile_keys_env={"general": "HERMES_KEY_GENERAL", "stock-picker": "HERMES_KEY_STOCKS"},
    )
    assert config.provider == ProviderConfig(
        name="openai-realtime",
        model="gpt-realtime",
        voice="marin",
        api_key_env="OPENAI_API_KEY",
        turn_detection={"type": "semantic_vad", "eagerness": "auto"},
    )
    assert config.router == RouterConfig(
        backend="knn",
        fast_path="knn",
        deadline_ms=250,
        default_profile="general",
        sticky_idle_s=300,
        suggest_switch=False,
        suggest_threshold=0.9,
        control_examples=ControlExamples(status=("where are we",), cancel=("forget it",)),
        decisions=DecisionsConfig(
            api_key_env="OPENAI_API_KEY", model="gpt-6-luna-preview", base_url="https://decisions.example/v1"
        ),
        jev=JevConfig(api_key_env="OPENAI_API_KEY", model="jev-preview", base_url="https://jev.example"),
    )
    assert config.profiles == {
        "stock-picker": ProfileConfig(
            description="Portfolio, holdings, stock research, trades",
            examples=("how's my portfolio", "should I buy NVDA"),
            models={
                "medium": ModelChoice(provider="openai", model="gpt-x-mini"),
                "high": ModelChoice(provider="anthropic", model="claude-x"),
            },
            default_model=ModelChoice(provider="openai", model="gpt-x"),
            aliases=("stocks", "The  Stock Picker"),
            voice_context="Always hand off prices.\nSay tickers letter by letter.\n",
        ),
        "general": ProfileConfig(description="Anything else", examples=("what's the weather",)),
    }
    assert config.people == {
        "rob": PersonConfig(
            discord_id=123, web_token_env="DUPLEX_TOKEN_ROB", profiles=("*",), default_profile="stock-picker"
        ),
        "alex": PersonConfig(discord_id=456, web_token_env="DUPLEX_TOKEN_ALEX", profiles=("general",)),
    }
    assert config.transports == TransportsConfig(
        discord=DiscordConfig(enabled=True, bot_token_env="DUPLEX_DISCORD_TOKEN"),
        web=WebConfig(enabled=True, host="0.0.0.0", port=9000),
    )
    assert config.audio == AudioConfig(pacer_lead_ms=120, gate_threshold_dbfs=-40.0, playback_queue_s=2.5)
    assert config.tasks == TasksConfig(restore_max_age_h=6.5)
    assert config.assistant == AssistantConfig(
        name="Iris",
        persona="Be dry-witted, like a concierge for the household.\nKeep it brisk.\n",
        backchannel="frequent",
    )
    assert config.secrets.get("OPENAI_API_KEY") == "sk-provider-secret"
    assert config.secrets.get("DUPLEX_TOKEN_ALEX") == "alex-web-token"


def test_minimal_config_takes_defaults() -> None:
    config = load_config(FIXTURES / "minimal.yaml", env={})

    assert config.hermes.profile_keys_env == {}
    assert config.provider.api_key_env is None
    assert config.provider.turn_detection is None
    assert config.provider.script == FIXTURES / "scripts" / "demo.jsonl"
    assert config.provider.record_path is None
    assert config.provider.capabilities is None
    assert config.router == RouterConfig(backend="knn")
    assert config.router.deadline_ms == 300
    assert config.router.default_profile == "general"
    assert config.router.sticky_idle_s == 600
    assert config.router.suggest_switch is True
    assert config.router.suggest_threshold == 0.85
    assert config.profiles == {"general": ProfileConfig()}
    assert config.people == {}
    assert config.transports == TransportsConfig()
    assert config.transports.web == WebConfig(enabled=False, host="127.0.0.1", port=8765)
    assert config.audio == AudioConfig(pacer_lead_ms=200, gate_threshold_dbfs=-35.0, playback_queue_s=5.0)
    assert config.tasks == TasksConfig(restore_max_age_h=24.0)
    assert config.assistant == AssistantConfig(name="Hermes", persona=None, backchannel="moderate")


def test_example_config_loads_when_its_env_vars_are_set() -> None:
    config = load_config(REPO_ROOT / "duplex.example.yaml", env=ENV)

    assert config.provider.name == "gpt-live"
    assert config.router.default_profile == "general"
    assert config.profiles["stock-picker"].models["high"].provider == "anthropic"
    assert config.router.decisions == DecisionsConfig(api_key_env="OPENAI_API_KEY")
    assert config.router.jev is None
    assert config.router.suggest_switch is True
    assert config.router.suggest_threshold == 0.85
    assert config.profiles["stock-picker"].aliases == ("stocks", "the stock picker", "portfolio")
    assert config.profiles["stock-picker"].voice_context is not None
    assert config.people["rob"].default_profile == "general"


def test_example_config_names_first_missing_env_var() -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(REPO_ROOT / "duplex.example.yaml", env={})

    assert caught.value.key == "hermes.profile_keys_env.general"
    assert "HERMES_KEY_GENERAL" in str(caught.value)


def test_missing_env_vars_are_all_listed_with_the_rest_of_the_config() -> None:
    env = {name: value for name, value in ENV.items() if name not in ("OPENAI_API_KEY", "DUPLEX_TOKEN_ALEX")}

    with pytest.raises(MissingEnvError) as caught:
        load_config(REPO_ROOT / "duplex.example.yaml", env=env)

    assert caught.value.key == "provider.api_key_env"
    assert caught.value.missing == (
        ("provider.api_key_env", "OPENAI_API_KEY"),
        ("router.decisions.api_key_env", "OPENAI_API_KEY"),
        ("people.alex.web_token_env", "DUPLEX_TOKEN_ALEX"),
    )
    config = caught.value.config
    assert config.provider.name == "gpt-live"
    assert config.secrets.names() == (
        "HERMES_KEY_GENERAL",
        "HERMES_KEY_STOCKS",
        "DUPLEX_TOKEN_ROB",
        "DUPLEX_DISCORD_TOKEN",
    )


def test_secrets_never_appear_in_repr() -> None:
    config = load_config(FIXTURES / "valid.yaml", env=ENV)

    for text in (repr(config), str(config), repr(config.secrets), str(config.secrets)):
        for name, value in ENV.items():
            if name != "HERMES_HOME":
                assert value not in text


def test_secrets_lookup_of_unnamed_env_var_raises() -> None:
    config = load_config(FIXTURES / "valid.yaml", env=ENV)

    with pytest.raises(KeyError):
        config.secrets.get("HOME")


# --- path resolution ---


def test_config_path_prefers_explicit_argument(tmp_path: Path) -> None:
    env = {"HERMES_DUPLEX_CONFIG": "/etc/duplex.yaml", "HERMES_HOME": "/srv/hermes"}

    assert resolve_config_path(tmp_path / "mine.yaml", env) == tmp_path / "mine.yaml"


def test_config_path_falls_back_to_env_var() -> None:
    env = {"HERMES_DUPLEX_CONFIG": "/etc/duplex.yaml", "HERMES_HOME": "/srv/hermes"}

    assert resolve_config_path(None, env) == Path("/etc/duplex.yaml")


def test_config_path_falls_back_to_hermes_home() -> None:
    assert resolve_config_path(None, {"HERMES_HOME": "/srv/hermes"}) == Path("/srv/hermes/duplex.yaml")


def test_config_path_defaults_to_dot_hermes_in_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_hermes_home({}) == tmp_path / ".hermes"
    assert resolve_config_path(None, {}) == tmp_path / ".hermes" / "duplex.yaml"


def test_load_config_uses_resolved_path(tmp_path: Path) -> None:
    (tmp_path / "duplex.yaml").write_text((FIXTURES / "minimal.yaml").read_text(encoding="utf-8"))

    config = load_config(env={"HERMES_HOME": str(tmp_path)})

    assert config.path == tmp_path / "duplex.yaml"
    assert config.hermes_home == tmp_path


def test_missing_config_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path / "absent.yaml", env={})

    assert caught.value.key == str(tmp_path / "absent.yaml")


def test_invalid_yaml_reports_position_without_quoting_the_file(tmp_path: Path) -> None:
    path = tmp_path / "duplex.yaml"
    path.write_text("provider:\n  name: fake\n  voice: [sk-literal-secret\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_config(path, env={})

    assert caught.value.key == str(path)
    assert "line" in str(caught.value)
    assert "sk-literal-secret" not in str(caught.value)


# --- fail-closed validation ---


def test_unknown_provider_is_rejected(tmp_path: Path) -> None:
    error = _assert_error(tmp_path, lambda raw: raw["provider"].update(name="gpt-4o"), "provider.name")
    assert "gpt-4o" in str(error)


def test_missing_provider_name_is_rejected_not_inferred(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["provider"].pop("name"), "provider.name")


def test_unknown_router_backend_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"].update(backend="llm"), "router.backend")


def test_unknown_fast_path_backend_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"].update(fast_path="decisions"), "router.fast_path")


def test_decisions_backend_without_its_settings_is_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["router"].update(backend="decisions")
        raw["router"].pop("decisions")

    _assert_error(tmp_path, mutate, "router.decisions.api_key_env")


def test_decisions_settings_without_api_key_env_are_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["router"]["decisions"].pop("api_key_env"), "router.decisions.api_key_env"
    )


def test_decisions_settings_take_defaults(tmp_path: Path) -> None:
    raw = _valid_raw()
    raw["router"].update(backend="decisions", decisions={"api_key_env": "OPENAI_API_KEY"})
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(path, env=ENV)

    assert config.router.backend == "decisions"
    assert config.router.decisions == DecisionsConfig(api_key_env="OPENAI_API_KEY", model=None, base_url=None)


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"base_url": "decisions.example/v1"}, "router.decisions.base_url"),
        ({"model": ""}, "router.decisions.model"),
        ({"timeout_ms": 100}, "router.decisions.timeout_ms"),
        ({"api_key": "sk-literal"}, "router.decisions.api_key"),
    ],
)
def test_invalid_decisions_settings_are_rejected(tmp_path: Path, settings: dict[str, Any], key: str) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"]["decisions"].update(settings), key)


def test_decisions_api_key_env_must_be_set(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["router"]["decisions"].update(api_key_env="OPENAI_DECISIONS_KEY")

    error = _assert_error(tmp_path, mutate, "router.decisions.api_key_env")
    assert "OPENAI_DECISIONS_KEY" in str(error)


def test_jev_backend_without_its_settings_is_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["router"].update(backend="jev")
        raw["router"].pop("jev")

    _assert_error(tmp_path, mutate, "router.jev.api_key_env")


def test_jev_settings_without_api_key_env_are_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"]["jev"].pop("api_key_env"), "router.jev.api_key_env")


def test_jev_settings_take_defaults(tmp_path: Path) -> None:
    raw = _valid_raw()
    raw["router"].update(backend="jev", jev={"api_key_env": "OPENAI_API_KEY"})
    raw["router"].pop("decisions")
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(path, env=ENV)

    assert config.router.backend == "jev"
    assert config.router.jev == JevConfig(api_key_env="OPENAI_API_KEY", model=None, base_url=None)
    assert config.router.decisions is None


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"base_url": "api.typesafe.ai"}, "router.jev.base_url"),
        ({"model": ""}, "router.jev.model"),
        ({"timeout_ms": 100}, "router.jev.timeout_ms"),
        ({"api_key": "ts-literal"}, "router.jev.api_key"),
    ],
)
def test_invalid_jev_settings_are_rejected(tmp_path: Path, settings: dict[str, Any], key: str) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"]["jev"].update(settings), key)


def test_jev_api_key_env_must_be_set(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["router"]["jev"].update(api_key_env="TYPESAFE_API_KEY")

    error = _assert_error(tmp_path, mutate, "router.jev.api_key_env")
    assert "TYPESAFE_API_KEY" in str(error)


def test_person_referencing_undefined_profile_is_rejected(tmp_path: Path) -> None:
    error = _assert_error(
        tmp_path, lambda raw: raw["people"]["alex"].update(profiles=["home"]), "people.alex.profiles"
    )
    assert "'home'" in str(error)


def test_undefined_default_profile_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["router"].update(default_profile="home"), "router.default_profile"
    )


def test_profile_key_for_undefined_profile_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["hermes"]["profile_keys_env"].update(home="HERMES_KEY_HOME"),
        "hermes.profile_keys_env.home",
    )


def test_new_profile_and_person_keys_default_to_none_or_empty(tmp_path: Path) -> None:
    raw = _valid_raw()
    del raw["profiles"]["stock-picker"]["aliases"], raw["profiles"]["stock-picker"]["voice_context"]
    del raw["people"]["rob"]["default_profile"]
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(path, env=ENV)

    assert config.profiles["stock-picker"].aliases == ()
    assert config.profiles["stock-picker"].voice_context is None
    assert config.people["rob"].default_profile is None


def test_voice_context_of_2000_characters_is_accepted(tmp_path: Path) -> None:
    _load_mutated(tmp_path, lambda raw: raw["profiles"]["general"].update(voice_context="x" * 2000))


def test_voice_context_over_2000_characters_is_rejected(tmp_path: Path) -> None:
    error = _assert_error(
        tmp_path,
        lambda raw: raw["profiles"]["general"].update(voice_context="x" * 2001),
        "profiles.general.voice_context",
    )
    assert "2001" in str(error)


@pytest.mark.parametrize("value", ["  \n", ["one"], 7])
def test_voice_context_that_is_not_text_is_rejected(tmp_path: Path, value: object) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["profiles"]["general"].update(voice_context=value),
        "profiles.general.voice_context",
    )


@pytest.mark.parametrize("alias", ["stocks", "STOCKS", " the   stock picker "])
def test_alias_used_by_two_profiles_is_rejected(tmp_path: Path, alias: str) -> None:
    # The fixture is written with sorted keys, so "weather" is read after "stock-picker".
    error = _assert_error(
        tmp_path, lambda raw: raw["profiles"].update(weather={"aliases": [alias]}), "profiles.weather.aliases"
    )
    assert f"'{alias}'" in str(error)
    assert "'stock-picker'" in str(error) and "'weather'" in str(error)


def test_alias_repeated_within_one_profile_is_accepted(tmp_path: Path) -> None:
    _load_mutated(tmp_path, lambda raw: raw["profiles"]["general"].update(aliases=["chat", "Chat"]))


def test_alias_may_equal_another_profiles_name(tmp_path: Path) -> None:
    _load_mutated(tmp_path, lambda raw: raw["profiles"]["general"].update(aliases=["stock-picker"]))


@pytest.mark.parametrize("aliases", [["   "], [""], "stocks", [7]])
def test_invalid_aliases_are_rejected(tmp_path: Path, aliases: object) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["profiles"]["general"].update(aliases=aliases), "profiles.general.aliases"
    )


def test_undefined_person_default_profile_is_rejected(tmp_path: Path) -> None:
    error = _assert_error(
        tmp_path,
        lambda raw: raw["people"]["rob"].update(default_profile="home"),
        "people.rob.default_profile",
    )
    assert "'home'" in str(error)


def test_person_default_profile_they_may_not_use_is_rejected(tmp_path: Path) -> None:
    error = _assert_error(
        tmp_path,
        lambda raw: raw["people"]["alex"].update(default_profile="stock-picker"),
        "people.alex.default_profile",
    )
    assert "'stock-picker'" in str(error)


def test_person_default_profile_from_their_list_is_accepted(tmp_path: Path) -> None:
    _load_mutated(tmp_path, lambda raw: raw["people"]["alex"].update(default_profile="general"))


@pytest.mark.parametrize("profiles", [[], None])  # null counts as a missing key
def test_person_without_profiles_is_rejected(tmp_path: Path, profiles: list[str] | None) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["people"]["alex"].update(profiles=profiles), "people.alex.profiles"
    )


@pytest.mark.parametrize("value", [0, 1.5, -0.1, "0.9", True])
def test_suggest_threshold_outside_zero_to_one_is_rejected(tmp_path: Path, value: object) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["router"].update(suggest_threshold=value), "router.suggest_threshold"
    )


def test_suggest_threshold_of_one_is_accepted(tmp_path: Path) -> None:
    _load_mutated(tmp_path, lambda raw: raw["router"].update(suggest_threshold=1.0))


def test_suggest_switch_that_is_not_a_boolean_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"].update(suggest_switch="yes"), "router.suggest_switch")


@pytest.mark.parametrize(
    ("path", "key"),
    [
        (("profiles", "stock-picker"), "alias"),
        (("profiles", "stock-picker"), "voice"),
        (("people", "rob"), "default"),
        (("router",), "suggest"),
    ],
)
def test_unknown_key_next_to_the_new_keys_is_rejected(
    tmp_path: Path, path: tuple[str, ...], key: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        section = raw
        for name in path:
            section = section[name]
        section[key] = "x"

    _assert_error(tmp_path, mutate, ".".join((*path, key)))


@pytest.mark.parametrize("name", ["_control", "ambiguous"])
def test_reserved_profile_name_is_rejected(tmp_path: Path, name: str) -> None:
    error = _assert_error(tmp_path, lambda raw: raw["profiles"].update({name: {}}), f"profiles.{name}")
    assert "reserved" in str(error)


@pytest.mark.parametrize(
    ("env_name", "key"),
    [
        ("HERMES_KEY_STOCKS", "hermes.profile_keys_env.stock-picker"),
        ("OPENAI_API_KEY", "provider.api_key_env"),
        ("DUPLEX_TOKEN_ALEX", "people.alex.web_token_env"),
        ("DUPLEX_DISCORD_TOKEN", "transports.discord.bot_token_env"),
    ],
)
def test_missing_secret_env_var_is_rejected(tmp_path: Path, env_name: str, key: str) -> None:
    env = {name: value for name, value in ENV.items() if name != env_name}

    error = _assert_error(tmp_path, lambda raw: None, key, env)
    assert env_name in str(error)


def test_empty_secret_env_var_counts_as_missing(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: None, "people.rob.web_token_env", ENV | {"DUPLEX_TOKEN_ROB": ""})


def test_real_provider_without_api_key_env_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["provider"].pop("api_key_env"), "provider.api_key_env")


def test_enabled_discord_without_bot_token_env_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["transports"]["discord"].pop("bot_token_env"),
        "transports.discord.bot_token_env",
    )


@pytest.mark.parametrize("turn_detection", [{"type": "semantic_vad"}, {"type": "none"}, {}])
def test_xai_turn_detection_other_than_server_vad_is_rejected(
    tmp_path: Path, turn_detection: dict[str, str]
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["provider"].update(name="xai-realtime", turn_detection=turn_detection)

    _assert_error(tmp_path, mutate, "provider.turn_detection.type")


def test_xai_with_server_vad_is_accepted(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["provider"].update(name="xai-realtime", turn_detection={"type": "server_vad"})

    _load_mutated(tmp_path, mutate)


@pytest.mark.parametrize("missing", ["provider", "model"])
def test_model_choice_missing_a_field_is_rejected(tmp_path: Path, missing: str) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["profiles"]["stock-picker"]["models"]["high"].pop(missing),
        f"profiles.stock-picker.models.high.{missing}",
    )


def test_default_model_missing_a_field_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["profiles"]["stock-picker"]["default_model"].pop("model"),
        "profiles.stock-picker.default_model.model",
    )


def test_unknown_effort_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["profiles"]["stock-picker"]["models"].update(max={"provider": "x", "model": "y"}),
        "profiles.stock-picker.models.max",
    )


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["router"].update(deadline=300), "router.deadline")


@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"backchannel": "very-frequent"}, "assistant.backchannel"),
        ({"backchannel": ""}, "assistant.backchannel"),
        ({"name": ""}, "assistant.name"),
        ({"name": 7}, "assistant.name"),
        ({"name": "   "}, "assistant.name"),
        ({"persona": " \n\t"}, "assistant.persona"),
        ({"persona": ["one", "two"]}, "assistant.persona"),
        ({"language": "en"}, "assistant.language"),
    ],
)
def test_invalid_assistant_settings_are_rejected(tmp_path: Path, settings: dict[str, Any], key: str) -> None:
    _assert_error(tmp_path, lambda raw: raw["assistant"].update(settings), key)


def test_assistant_must_be_a_mapping(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw.update(assistant="Iris"), "assistant")


def test_fake_only_key_on_real_provider_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["provider"].update(script="s.jsonl"), "provider.script")


def test_fake_capabilities_on_real_provider_are_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["provider"].update(capabilities={"delegation_style": "live"}),
        "provider.capabilities",
    )


def _fake_provider(raw: dict[str, Any], capabilities: dict[str, Any]) -> None:
    raw["provider"] = {"name": "fake", "capabilities": capabilities}


def test_fake_capabilities_hold_only_the_keys_set(tmp_path: Path) -> None:
    raw = _valid_raw()
    _fake_provider(
        raw,
        {
            "delegation_style": "live",
            "emits_speech_events": False,
            "replaces_instructions": False,
            "input_rate": 16000,
            "output_rate": None,
        },
    )
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(path, env=ENV)

    assert config.provider.capabilities == {
        "delegation_style": "live",
        "emits_speech_events": False,
        "replaces_instructions": False,
        "input_rate": 16000,
    }


@pytest.mark.parametrize(
    ("capabilities", "key"),
    [
        ({"delegation_style": "native"}, "provider.capabilities.delegation_style"),
        ({"supports_truncate": "yes"}, "provider.capabilities.supports_truncate"),
        ({"replaces_instructions": "no"}, "provider.capabilities.replaces_instructions"),
        ({"input_rate": 0}, "provider.capabilities.input_rate"),
        ({"max_session_s": "600"}, "provider.capabilities.max_session_s"),
        ({"barge_in": True}, "provider.capabilities.barge_in"),
    ],
)
def test_invalid_fake_capability_is_rejected(tmp_path: Path, capabilities: dict[str, Any], key: str) -> None:
    _assert_error(tmp_path, lambda raw: _fake_provider(raw, capabilities), key)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("router", "deadline_ms"),
        ("router", "sticky_idle_s"),
        ("audio", "pacer_lead_ms"),
    ],
)
@pytest.mark.parametrize("value", [0, "300", True])
def test_non_positive_or_non_integer_numbers_are_rejected(
    tmp_path: Path, section: str, key: str, value: object
) -> None:
    _assert_error(tmp_path, lambda raw: raw[section].update({key: value}), f"{section}.{key}")


@pytest.mark.parametrize("value", [0, -1, "24", True])
def test_restore_max_age_that_is_not_a_positive_number_is_rejected(tmp_path: Path, value: object) -> None:
    _assert_error(
        tmp_path, lambda raw: raw["tasks"].update(restore_max_age_h=value), "tasks.restore_max_age_h"
    )


def test_unknown_tasks_key_is_rejected(tmp_path: Path) -> None:
    _assert_error(tmp_path, lambda raw: raw["tasks"].update(max_age_h=1), "tasks.max_age_h")


@pytest.mark.parametrize(
    ("path", "key"),
    [
        (("provider",), "api_key"),
        (("provider",), "key"),
        (("provider", "turn_detection"), "secret"),
        (("hermes",), "token"),
        (("transports", "discord"), "bot_token"),
        (("people", "rob"), "web_token"),
    ],
)
def test_literal_secret_key_is_rejected_without_echoing_it(
    tmp_path: Path, path: tuple[str, ...], key: str
) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        section = raw
        for name in path:
            section = section[name]
        section[key] = "literal-secret-value"

    error = _assert_error(tmp_path, mutate, ".".join((*path, key)))
    assert "literal-secret-value" not in str(error)


@pytest.mark.parametrize("value", ["sk-proj-abc123", "openai_api_key", "1KEY", ""])
def test_env_value_that_is_not_an_env_var_name_is_rejected_without_echoing_it(
    tmp_path: Path, value: str
) -> None:
    error = _assert_error(
        tmp_path, lambda raw: raw["provider"].update(api_key_env=value), "provider.api_key_env"
    )
    if value:
        assert value not in str(error)


def test_profile_key_value_that_is_not_an_env_var_name_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        tmp_path,
        lambda raw: raw["hermes"]["profile_keys_env"].update(general="hermes-key-0123456789"),
        "hermes.profile_keys_env.general",
    )
