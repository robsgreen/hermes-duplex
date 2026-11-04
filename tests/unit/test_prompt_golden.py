"""Every prompter's output for one fixed context, compared with committed golden files.

The files under ``tests/fixtures/prompts/<provider>/`` are the exact text each provider's model
reads, so a prompt change shows up in review as a diff of them. After changing a prompt on purpose,
regenerate them with ``HERMES_DUPLEX_UPDATE_PROMPTS=1 uv run pytest tests/unit/test_prompt_golden.py``
and review the diff. Without that variable a missing or different file fails the test.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_duplex.config import ProviderConfig, Secrets
from hermes_duplex.prompts.base import PromptContext, TaskReport, VoicePrompter
from hermes_duplex.providers.registry import provider_factory
from hermes_duplex.tasks import Task

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "prompts"
UPDATE = os.environ.get("HERMES_DUPLEX_UPDATE_PROMPTS") == "1"

CONTEXT = PromptContext(
    profiles=(
        ("stock-picker", "Portfolio, holdings, stock research, trades"),
        ("general", "Anything else"),
    ),
    active_profile="stock-picker",
    active_profile_description="Portfolio, holdings, stock research, trades",
    voice_context=(
        "Covers the user's portfolio, holdings, market data and trades.\n"
        "Always hand off prices, positions, and anything time-sensitive; never quote a number from memory.\n"
        "Say tickers letter by letter (NVDA as N-V-D-A).\n"
        "Confirm before handing off any request to buy or sell.\n"
    ),
)
"""The example config's deployment, with the default assistant, while stock-picker is active."""


def _bound_prompter(name: str) -> VoicePrompter:
    """The prompter of the provider the registry builds for ``provider.name`` ``name``."""
    config = ProviderConfig(name=name, model=None, voice=None, api_key_env="API_KEY", turn_detection=None)
    return provider_factory(name)(config, Secrets({"API_KEY": "test"}), CONTEXT).prompter


PROMPTERS: dict[str, VoicePrompter] = {
    name: _bound_prompter(name) for name in ("gpt-live", "openai-realtime", "xai-realtime")
}
"""Each real provider's prompter, as a provider built from config binds it."""

REPORT = TaskReport(
    task_number=3,
    topic="how is my portfolio doing",
    profile="stock-picker",
    status="completed",
    text="Your portfolio is up 2% today. NVDA led the gains; TSLA was flat.",
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)
LINES = [("user", "how is my portfolio doing"), ("assistant", "I'll ask the stock-picker agent.")]
TASKS = [
    Task(
        number=3,
        uuid="u",
        person="rob",
        profile="stock-picker",
        effort="medium",
        model=None,
        text="how is my portfolio doing",
        state="running",
        run_id="run_1",
        created_at=NOW,
        updated_at=NOW,
    )
]


def _render(prompter: VoicePrompter, kind: str) -> str:
    match kind:
        case "instructions":
            return prompter.instructions(CONTEXT)
        case "result":
            return prompter.result(REPORT, cap_tokens=500)
        case _:
            return prompter.reseed(LINES, TASKS)


@pytest.mark.parametrize("kind", ["instructions", "result", "reseed"])
@pytest.mark.parametrize("provider", sorted(PROMPTERS))
def test_prompter_output_matches_its_golden_file(provider: str, kind: str) -> None:
    rendered = _render(PROMPTERS[provider], kind) + "\n"
    path = GOLDEN / provider / f"{kind}.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.exists(), f"{path} is missing; regenerate it with HERMES_DUPLEX_UPDATE_PROMPTS=1"

    assert rendered == path.read_text(encoding="utf-8"), (
        f"{provider} {kind} changed; if on purpose, regenerate with HERMES_DUPLEX_UPDATE_PROMPTS=1"
    )
