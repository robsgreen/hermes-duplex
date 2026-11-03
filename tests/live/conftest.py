"""Fixtures for the live tests.

``live_hermes`` (one per module) is the end-to-end tier's real Hermes gateway (see
``tests/e2e/real_hermes.py``) reconfigured to use real services: the plugin talks to GPT-Live and
routes with the OpenAI Decisions API (knn as its fast path), and the ``general`` and
``stock-picker`` profiles run on a real OpenAI-compatible model. The environment variables that opt
in and configure it are listed in ``test_live_gpt_live.py``'s docstring.

Tests that use the fixture skip when an opt-in variable is missing, when the spoken WAV fixtures
have not been generated (``tests/live/make_audio_fixtures.py``), or when Hermes is not installed.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.real_hermes import RealHermes, hermes_command, run_real_hermes
from tests.live.make_audio_fixtures import AUDIO_DIR, SILENCE, SPEECH

PROFILE_PROVIDER = "live"
"""The name the profiles' ``config.yaml`` gives the real model's provider."""


@pytest.fixture(scope="module")
def live_hermes() -> Iterator[RealHermes]:
    if os.environ.get("HERMES_DUPLEX_LIVE") != "1" or not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("live test (calls paid APIs): set OPENAI_API_KEY and HERMES_DUPLEX_LIVE=1 to run it")
    missing = [name for name in (*SPEECH, SILENCE) if not (AUDIO_DIR / name).is_file()]
    if missing:
        pytest.skip(
            f"live test: tests/fixtures/audio/ lacks {', '.join(missing)}; generate them with "
            "`OPENAI_API_KEY=... uv run python -m tests.live.make_audio_fixtures`"
        )
    model = os.environ.get("HERMES_DUPLEX_LIVE_MODEL", "gpt-5-mini")
    base_url = os.environ.get("HERMES_DUPLEX_LIVE_BASE_URL", "https://api.openai.com/v1")
    key_env = os.environ.get("HERMES_DUPLEX_LIVE_KEY_ENV", "OPENAI_API_KEY")
    if not os.environ.get(key_env) or key_env.startswith("HERMES_"):
        pytest.skip(
            f"live test: HERMES_DUPLEX_LIVE_KEY_ENV names {key_env}, which is unset or starts with HERMES_"
        )

    def use_real_services(home: Path) -> None:
        for profile in ("general", "stock-picker"):
            profile_config = {
                "model": {"provider": PROFILE_PROVIDER, "default": model},
                "providers": {
                    PROFILE_PROVIDER: {"api": base_url, "key_env": key_env, "transport": "chat_completions"}
                },
            }
            _write_yaml(home / "profiles" / profile / "config.yaml", profile_config)

        duplex_path = home / "duplex.yaml"
        duplex = yaml.safe_load(duplex_path.read_text(encoding="utf-8"))
        duplex["provider"] = {"name": "gpt-live", "api_key_env": "OPENAI_API_KEY"}
        duplex["router"] = {
            "backend": "decisions",
            "fast_path": "knn",
            # A real Decisions call can take longer than the default 300 ms; give it time, so the
            # test exercises Decisions rather than the knn fallback.
            "deadline_ms": 3000,
            "decisions": {"api_key_env": "OPENAI_API_KEY"},
        }
        cell = {"provider": PROFILE_PROVIDER, "model": model}
        duplex["profiles"]["stock-picker"]["models"] = {"low": cell, "medium": cell, "high": cell}
        _write_yaml(duplex_path, duplex)

    with run_real_hermes(hermes_command(), configure=use_real_services) as hermes:
        yield hermes


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
