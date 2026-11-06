"""Switch requests on the real embedding model: the held-out phrasings in
``tests/fixtures/router_eval/switch_heldout.yaml``, read as a voice session reads them.

Skips unless ``minishlab/potion-base-8M`` is already in the local Hugging Face cache (``eval-router``
or the README's e2e recipe puts it there); it never downloads it."""

from pathlib import Path

import pytest

from hermes_duplex.router.eval import SwitchScores, load_phrases, switch_scores
from hermes_duplex.router.knn import DEFAULT_MODEL, ProfileMatcher, load_model

HELDOUT = Path(__file__).resolve().parents[1] / "fixtures" / "router_eval" / "switch_heldout.yaml"


@pytest.fixture
async def scores(monkeypatch: pytest.MonkeyPatch) -> SwitchScores:
    huggingface_hub = pytest.importorskip("huggingface_hub")
    if not isinstance(huggingface_hub.try_to_load_from_cache(DEFAULT_MODEL, "config.json"), str):
        pytest.skip(f"{DEFAULT_MODEL} is not in the local Hugging Face cache")
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_OFFLINE", True)  # read from the cache only
    phrase_file = load_phrases(HELDOUT)
    assert phrase_file.profiles is not None
    matcher = ProfileMatcher(phrase_file.profiles, load_model(DEFAULT_MODEL))
    return await switch_scores(phrase_file.phrases, phrase_file.profiles, matcher)


@pytest.mark.parametrize(
    ("text", "profile"),
    [
        ("use my portfolio agent", "stock-picker"),
        ("connect me with the stock research assistant", "stock-picker"),
        ("go to the stock trader profile", "stock-picker"),
        ("put me on the trading agent", "stock-picker"),
        ("move me to stocks", "stock-picker"),
        ("connect me with the investing agent, what's Apple at", "stock-picker"),
        ("put me on the coding agent", "coder"),
        ("take me to the headlines bot", "news"),
    ],
)
def test_a_switch_phrasing_switches_to_the_profile_it_means(
    scores: SwitchScores, text: str, profile: str
) -> None:
    assert scores.taken.get(text) == profile


@pytest.mark.parametrize(
    "text",
    [
        "talk to Sarah about the stock picker",
        "can I talk to Sarah about the stock picker",
        "connect me with my accountant",
        "find me a travel agent",
        "change my profile picture",
        "use a different approach",
        "call my agent",
        "go to the gym",
        "help me pick a different agent for my house sale",
        "go to the insurance agent",
        "talk to my real estate agent",
    ],
)
def test_a_task_that_only_sounds_like_a_switch_stays_a_task(scores: SwitchScores, text: str) -> None:
    assert text not in scores.taken


def test_the_held_out_scores_hold(scores: SwitchScores) -> None:
    # The README's "Score the router" table gives the current values.
    assert scores.precision is not None and scores.precision >= 0.85
    assert scores.task_as_switch_rate is not None and scores.task_as_switch_rate <= 0.06
    assert scores.target_accuracy is not None and scores.target_accuracy >= 0.77
