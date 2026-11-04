"""ActiveProfiles: default resolution, persistence, fallback after config changes, switching, listeners."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.config import Config, load_config
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles, SwitchResult

PROFILES: dict[str, dict[str, Any]] = {"home": {}, "general": {}, "stock-picker": {}}

PEOPLE: dict[str, dict[str, Any]] = {
    # people.<p>.default_profile wins.
    "rob": {"discord_id": "1", "profiles": ["*"], "default_profile": "stock-picker"},
    # No default_profile, may use router.default_profile ("general").
    "alex": {"discord_id": "2", "profiles": ["stock-picker", "general"]},
    # Neither: the first profile in their list.
    "sam": {"discord_id": "3", "profiles": ["stock-picker", "home"]},
    # Neither, with a single profile.
    "kim": {"discord_id": "4", "profiles": ["home"]},
}


def _config(
    tmp_path: Path,
    profiles: dict[str, dict[str, Any]] = PROFILES,
    people: dict[str, dict[str, Any]] = PEOPLE,
) -> Config:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn", "default_profile": "general"},
        "profiles": profiles,
        "people": people,
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={})


def _store(config: Config, path: Path) -> ActiveProfiles:
    return ActiveProfiles(path, PermissionChecker(config), config)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "duplex" / "active_profiles.json"


@pytest.fixture
def store(tmp_path: Path, path: Path) -> ActiveProfiles:
    return _store(_config(tmp_path), path)


class _Recorder:
    """A switch listener that records the profiles it is told about and can fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.profiles: list[str] = []
        self._error = error

    async def __call__(self, profile: str) -> None:
        self.profiles.append(profile)
        if self._error is not None:
            raise self._error


# Default resolution


def test_default_is_the_persons_own_default_profile(store: ActiveProfiles) -> None:
    assert store.get("rob") == "stock-picker"


def test_default_is_the_router_default_when_the_person_may_use_it(store: ActiveProfiles) -> None:
    assert store.get("alex") == "general"


def test_default_is_the_first_profile_in_the_persons_list(store: ActiveProfiles) -> None:
    assert store.get("sam") == "stock-picker"
    assert store.get("kim") == "home"


# Persistence


async def test_a_switch_survives_a_new_store_on_the_same_file(tmp_path: Path, path: Path) -> None:
    config = _config(tmp_path)
    await _store(config, path).switch("alex", "stock-picker")

    reloaded = _store(config, path)
    reloaded.load()

    assert reloaded.get("alex") == "stock-picker"
    assert reloaded.get("sam") == "stock-picker"
    assert reloaded.fallback_pending("alex") is None


async def test_the_file_is_written_by_replacing_it_whole(store: ActiveProfiles, path: Path) -> None:
    await store.switch("alex", "stock-picker")

    assert json.loads(path.read_text(encoding="utf-8")) == {"alex": "stock-picker"}
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize("contents", [b"{not json", b"\xff\xfe", b'["alex"]', b'{"alex": 3}'])
def test_a_corrupt_file_gives_defaults_and_logs_an_error(
    store: ActiveProfiles, path: Path, caplog: pytest.LogCaptureFixture, contents: bytes
) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)

    with caplog.at_level(logging.ERROR, logger="hermes_duplex.profiles"):
        store.load()

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert str(path) in caplog.records[0].getMessage()
    assert store.get("alex") == "general"


def test_a_missing_file_gives_defaults_without_an_error(
    store: ActiveProfiles, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="hermes_duplex.profiles"):
        store.load()

    assert [record.levelno for record in caplog.records] == [logging.INFO]
    assert store.get("rob") == "stock-picker"


async def test_a_corrupt_file_is_rewritten_on_the_next_switch(store: ActiveProfiles, path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    store.load()

    await store.switch("alex", "stock-picker")

    assert json.loads(path.read_text(encoding="utf-8")) == {"alex": "stock-picker"}


async def test_a_file_that_cannot_be_written_logs_an_error_and_the_switch_holds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    store = _store(_config(tmp_path), blocker / "active_profiles.json")

    with caplog.at_level(logging.ERROR, logger="hermes_duplex.profiles"):
        result = await store.switch("alex", "stock-picker")

    assert result.outcome == "switched"
    assert store.get("alex") == "stock-picker"
    assert "could not save" in caplog.text


# Fallback after config changes


@pytest.mark.parametrize("saved", ["retired", "home"], ids=["removed from config", "no longer permitted"])
def test_an_unusable_saved_profile_falls_back_to_the_default_once(
    store: ActiveProfiles, path: Path, saved: str
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"alex": saved}), encoding="utf-8")

    store.load()

    assert store.get("alex") == "general"
    assert store.fallback_pending("alex") == "general"
    assert store.fallback_pending("alex") is None
    assert store.fallback_pending("rob") is None


def test_saved_entries_for_people_no_longer_in_config_are_ignored(store: ActiveProfiles, path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"gone": "home", "sam": "home"}), encoding="utf-8")

    store.load()

    assert store.get("sam") == "home"
    assert store.fallback_pending("gone") is None


# Switching


async def test_switch_outcomes(store: ActiveProfiles) -> None:
    assert await store.switch("alex", "nonexistent") == SwitchResult("unknown", notified_live=False)
    assert await store.switch("alex", "home") == SwitchResult("refused", notified_live=False)
    assert await store.switch("stranger", "home") == SwitchResult("refused", notified_live=False)
    assert await store.switch("alex", "general") == SwitchResult("already_active", notified_live=False)
    assert await store.switch("alex", "stock-picker") == SwitchResult("switched", notified_live=False)
    assert store.get("alex") == "stock-picker"
    assert await store.switch("alex", "stock-picker") == SwitchResult("already_active", notified_live=False)


async def test_switch_tells_that_persons_listeners_only(store: ActiveProfiles) -> None:
    first, second, other = _Recorder(), _Recorder(), _Recorder()
    store.subscribe("alex", first)
    store.subscribe("alex", second)
    store.subscribe("sam", other)

    result = await store.switch("alex", "stock-picker")

    assert result == SwitchResult("switched", notified_live=True)
    assert first.profiles == ["stock-picker"]
    assert second.profiles == ["stock-picker"]
    assert other.profiles == []


async def test_unsubscribed_listeners_are_not_told(store: ActiveProfiles) -> None:
    listener = _Recorder()
    unsubscribe = store.subscribe("alex", listener)
    unsubscribe()
    unsubscribe()

    result = await store.switch("alex", "stock-picker")

    assert listener.profiles == []
    assert result.notified_live is False


async def test_a_failing_listener_does_not_block_the_switch_or_the_others(
    store: ActiveProfiles, path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    failing, working = _Recorder(RuntimeError("boom")), _Recorder()
    store.subscribe("alex", failing)
    store.subscribe("alex", working)

    with caplog.at_level(logging.ERROR, logger="hermes_duplex.profiles"):
        result = await store.switch("alex", "stock-picker")

    assert result == SwitchResult("switched", notified_live=True)
    assert working.profiles == ["stock-picker"]
    assert store.get("alex") == "stock-picker"
    assert json.loads(path.read_text(encoding="utf-8")) == {"alex": "stock-picker"}
    assert "boom" in caplog.text


async def test_a_switch_clears_a_pending_fallback(store: ActiveProfiles, path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"alex": "home"}), encoding="utf-8")
    store.load()

    await store.switch("alex", "stock-picker")

    assert store.fallback_pending("alex") is None
