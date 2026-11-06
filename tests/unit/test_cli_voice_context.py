"""``hermes-duplex voice-context show`` and ``refresh``: what the voice model gets, and rewrites on demand."""

import argparse
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex import cli
from hermes_duplex.config import Config, load_config
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript

KEY = "general-key-0123456789"


def _write_config(tmp_path: Path, api_base: str = "http://127.0.0.1:9", **hermes: Any) -> Path:
    raw = {
        "hermes": {
            "api_base": api_base,
            "profile_keys_env": {"general": "HERMES_KEY_GENERAL"},
            "voice_context_files": ["soul", "user"],
            **hermes,
        },
        "provider": {"name": "gpt-live", "api_key_env": "OPENAI_API_KEY"},
        "router": {"backend": "knn"},
        "profiles": {"general": {"description": "Anything"}, "notes": {}},
        "people": {"rob": {"profiles": ["*"], "hermes_owner": True}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    soul = tmp_path / "profiles" / "general" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text("You are the general agent.", encoding="utf-8")
    user = tmp_path / "profiles" / "general" / "memories" / "USER.md"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("Rob lives in Lisbon.", encoding="utf-8")
    return path


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    cli.add_commands(parser)
    return parser.parse_args(argv)


def _load(path: Path, tmp_path: Path) -> Config:
    return load_config(
        path, env={"HERMES_HOME": str(tmp_path), "HERMES_KEY_GENERAL": KEY, "OPENAI_API_KEY": "sk"}
    )


def test_show_prints_the_instructions_with_the_files_and_notes_the_assumptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # the env vars the config names stay unset

    assert cli.run(_parse("voice-context", "show", "general", "--config", str(path))) == 0

    out, err = capsys.readouterr()
    assert out.startswith("You are Hermes, a voice assistant")
    assert "<agent_background>\nYou are the general agent.\n</agent_background>" in out
    assert "<owner_background>\nRob lives in Lisbon.\n</owner_background>" in out
    assert "# talking with rob, the Hermes owner, alone" in err
    assert "# SOUL.md: the file itself, no rewrite of it yet" in err
    assert "# briefing: off (hermes.voice_context_files)" in err
    assert not (tmp_path / "duplex").exists()


def test_show_of_an_unknown_profile_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert cli.run(_parse("voice-context", "show", "stocks", "--config", str(path))) == 1
    assert "'stocks' is not defined" in capsys.readouterr().err


def test_voice_context_without_a_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run(_parse("voice-context")) == 2


async def test_refresh_rewrites_each_file_once_then_finds_it_current_until_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with FakeHermesApi({"general": KEY}) as fake:
        fake.run_scripts = [
            RunScript(events=[{"event": "run.completed", "output": f"Rewrite {n}."}]) for n in range(1, 5)
        ]
        config = _load(_write_config(tmp_path, fake.api_base), tmp_path)

        first = await cli.refresh_voice_context(config, "general")
        again = await cli.refresh_voice_context(config, "general")
        forced = await cli.refresh_voice_context(config, "general", force=True)

        starts = fake.requests_to("POST", "/runs")

    lines = capsys.readouterr().out.splitlines()
    assert (first, again, forced) == (0, 0, 0)
    assert lines[0].startswith("general SOUL.md: written for voice, cached at ")
    assert lines[1].startswith("general USER.md: written for voice")
    assert lines[2].startswith("general SOUL.md: the cached rewrite is current")
    assert lines[4].startswith("general SOUL.md: written")
    assert [start.path for start in starts] == ["/p/general/v1/runs"] * 4
    cache = tmp_path / "duplex" / "voice_context"
    assert (cache / "general.soul.md").read_text(encoding="utf-8") == "Rewrite 3."


async def test_refresh_of_every_profile_reports_missing_files_and_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with FakeHermesApi({"general": KEY}) as fake:
        fake.run_scripts = [RunScript(events=[{"event": "run.failed", "error": "no model"}])]
        config = _load(_write_config(tmp_path, fake.api_base, voice_context_files=["soul"]), tmp_path)

        status = await cli.refresh_voice_context(config, None)

    assert status == 1
    assert capsys.readouterr().out.splitlines() == [
        "general SOUL.md: Hermes could not write it (see the log); the voice prompt keeps what it had",
        "notes SOUL.md: no file to rewrite",
    ]


async def test_refresh_with_no_files_enabled_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _load(_write_config(tmp_path, voice_context_files=[]), tmp_path)

    assert await cli.refresh_voice_context(config, None) == 1
    assert "lists nothing" in capsys.readouterr().err
