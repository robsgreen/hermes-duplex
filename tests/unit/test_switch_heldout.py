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
        # Round 10b: the added phrasings, and targets without an agent noun on a profile's name.
        ("switch to stocks", "stock-picker"),
        ("talk to the stock picker", "stock-picker"),
        ("switch me to the stock picker please", "stock-picker"),
        ("can you switch me to the trip planning agent", "travel"),
        ("put me through to the finance agent", "stock-picker"),
        ("switch me over to the news one?", "news"),
        ("switch over to stocks", "stock-picker"),
        ("get me the stocks agent", "stock-picker"),
        ("can I have the coding assistant", "coder"),
        ("hand me over to the news agent", "news"),
        ("bring up the travel agent", "travel"),
        ("I'd like the coder", "coder"),
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
        # Round 10b: targets without an agent noun that are no profile's near-exact name.
        "talk to my stock broker",
        "talk to my stock trader",
        "switch to stock photos",
        "talk to my travel buddy",
        "talk to a programmer",
        "change to Celsius",
        "talk to my dad",
        "can I speak to a human",
        "put me through to my accountant",
        "get me a coffee",
        "I'd like the salmon",
        "get me the news",
    ],
)
def test_a_task_that_only_sounds_like_a_switch_stays_a_task(scores: SwitchScores, text: str) -> None:
    assert text not in scores.taken


def test_the_held_out_scores_hold(scores: SwitchScores) -> None:
    # The README's "Score the router" table gives the current values; these floors sit a little below
    # them, so a small change of model or phrasing does not fail the test, and a real loss does.
    assert scores.precision is not None and scores.precision >= 0.95
    assert scores.recall is not None and scores.recall >= 0.87
    assert scores.task_as_switch_rate is not None and scores.task_as_switch_rate <= 0.02
    assert scores.target_accuracy is not None and scores.target_accuracy >= 0.87
