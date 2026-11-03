"""PermissionChecker: lookup by Discord id and web token, the "*" wildcard, and one owner per identity."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.config import Config, ConfigError, load_config
from hermes_duplex.people import PermissionChecker, Person

ENV = {
    "TOKEN_ROB": "rob-web-token",
    "TOKEN_ALEX": "alex-web-token",
    "TOKEN_SAM": "sam-web-token",
}

PEOPLE: dict[str, dict[str, Any]] = {
    "rob": {"discord_id": "123", "web_token_env": "TOKEN_ROB", "profiles": ["*"]},
    "alex": {"discord_id": "456", "web_token_env": "TOKEN_ALEX", "profiles": ["general"]},
    "sam": {"web_token_env": "TOKEN_SAM", "profiles": []},
}


def _config(tmp_path: Path, people: dict[str, dict[str, Any]] = PEOPLE, env: dict[str, str] = ENV) -> Config:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {}, "stock-picker": {}, "home": {}},
        "people": people,
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env=env)


@pytest.fixture
def checker(tmp_path: Path) -> PermissionChecker:
    return PermissionChecker(_config(tmp_path))


def test_lookup_by_discord_id(checker: PermissionChecker) -> None:
    alex = checker.by_discord_id(456)

    assert alex == Person(name="alex", discord_id=456, profiles=frozenset({"general"}))
    assert checker.by_discord_id(789) is None


def test_allowlisted_discord_ids_are_people_with_an_id(checker: PermissionChecker) -> None:
    assert checker.allowlisted_discord_ids == frozenset({123, 456})


def test_web_token_maps_to_exactly_its_person(checker: PermissionChecker) -> None:
    rob = checker.by_web_token("rob-web-token")
    sam = checker.by_web_token("sam-web-token")

    assert rob is not None and rob.name == "rob"
    assert sam is not None and sam.name == "sam"
    assert sam.discord_id is None


@pytest.mark.parametrize("token", ["", "rob-web-toke", "rob-web-token ", "ROB-WEB-TOKEN", "röb"])
def test_unknown_web_token_maps_to_nobody(checker: PermissionChecker, token: str) -> None:
    assert checker.by_web_token(token) is None


def test_wildcard_grants_every_defined_profile(checker: PermissionChecker) -> None:
    rob = checker.by_discord_id(123)

    assert rob is not None
    assert rob.profiles == frozenset({"general", "stock-picker", "home"})
    assert all(checker.allowed(rob, profile) for profile in ("general", "stock-picker", "home"))
    assert not checker.allowed(rob, "_control")


def test_listed_profiles_only(checker: PermissionChecker) -> None:
    alex = checker.by_discord_id(456)
    sam = checker.by_web_token("sam-web-token")

    assert alex is not None and sam is not None
    assert checker.allowed(alex, "general")
    assert not checker.allowed(alex, "stock-picker")
    assert not checker.allowed(sam, "general")


def test_duplicate_web_token_is_a_config_error(tmp_path: Path) -> None:
    config = _config(tmp_path, env=ENV | {"TOKEN_SAM": "rob-web-token"})

    with pytest.raises(ConfigError) as caught:
        PermissionChecker(config)

    assert caught.value.key == "people.sam.web_token_env"
    assert "rob" in str(caught.value)
    assert "rob-web-token" not in str(caught.value)


def test_shared_web_token_env_var_is_a_config_error(tmp_path: Path) -> None:
    people = PEOPLE | {"sam": {"web_token_env": "TOKEN_ALEX", "profiles": []}}

    with pytest.raises(ConfigError) as caught:
        PermissionChecker(_config(tmp_path, people=people))

    assert caught.value.key == "people.sam.web_token_env"


def test_duplicate_discord_id_is_a_config_error(tmp_path: Path) -> None:
    people = PEOPLE | {"sam": {"discord_id": 456, "profiles": []}}

    with pytest.raises(ConfigError) as caught:
        PermissionChecker(_config(tmp_path, people=people))

    assert caught.value.key == "people.sam.discord_id"
