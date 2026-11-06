"""KnnBackend: phrase matching over injected embeddings, ``_control`` intents, and off-loop embedding;
ProfileMatcher: switch targets by name and by meaning."""

import threading
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
import pytest

from hermes_duplex.config import CONTROL_PROFILE, ControlExamples, ProfileConfig
from hermes_duplex.router import knn
from hermes_duplex.router.base import ProfileSpec, RouteRequest, control_spec
from hermes_duplex.router.knn import Embedder, KnnBackend, ProfileMatcher
from tests.fakes.fake_embedder import fake_embed

PROFILES = (
    ProfileSpec(
        name="stock-picker",
        description="Portfolio, holdings, stock research, trades",
        examples=("how's my portfolio", "should I buy NVDA", "what did TSLA do today"),
    ),
    ProfileSpec(
        name="general", description="Anything else", examples=("what's the weather", "set a reminder")
    ),
)


def _request(text: str) -> RouteRequest:
    return RouteRequest(text=text, speaker="rob", history=(), active_profile="general")


async def _configured(extra: ControlExamples | None = None) -> KnnBackend:
    backend = KnnBackend(fake_embed)
    await backend.configure([*PROFILES, control_spec(extra or ControlExamples())])
    return backend


async def test_routes_a_portfolio_phrase_to_stock_picker() -> None:
    backend = await _configured()

    result = await backend.classify(_request("how is my portfolio doing"), deadline_ms=300)

    assert max(result.profile_probs, key=lambda name: result.profile_probs[name]) == "stock-picker"
    assert result.profile_probs["stock-picker"] > 0.9
    assert set(result.profile_probs) == {"stock-picker", "general", CONTROL_PROFILE}
    assert sum(result.profile_probs.values()) == pytest.approx(1.0)
    assert result.effort_probs is None
    assert result.backend == "knn"
    assert result.latency_ms >= 0


async def test_matches_the_description_as_well_as_the_examples() -> None:
    backend = await _configured()

    result = await backend.classify(_request("research on my holdings"), deadline_ms=300)

    assert result.profile_probs["stock-picker"] > 0.9


@pytest.mark.parametrize(
    ("text", "intent"),
    [("any updates on that", "status"), ("what are you working on", "status"), ("cancel that", "cancel")],
)
async def test_control_phrases_route_to_control_with_their_intent(text: str, intent: str) -> None:
    backend = await _configured()

    result = await backend.classify(_request(text), deadline_ms=300)

    assert result.profile_probs[CONTROL_PROFILE] > 0.9
    assert result.control_intent == intent


@pytest.mark.parametrize(
    ("text", "similarity"),
    [("cancel that", pytest.approx(1.0)), ("cancel", pytest.approx(0.5**0.5)), ("zxq", 0.0)],
)
async def test_reports_the_similarity_of_the_nearest_control_phrase(text: str, similarity: float) -> None:
    backend = await _configured()

    result = await backend.classify(_request(text), deadline_ms=300)

    assert result.control_similarity == similarity


async def test_config_control_examples_extend_the_builtin_ones() -> None:
    backend = await _configured(ControlExamples(cancel=("abort the job",)))

    result = await backend.classify(_request("abort the job"), deadline_ms=300)

    assert result.profile_probs[CONTROL_PROFILE] > 0.9
    assert result.control_intent == "cancel"


async def test_a_request_with_no_known_words_is_not_confident() -> None:
    backend = await _configured()

    result = await backend.classify(_request("zxq"), deadline_ms=300)

    assert max(result.profile_probs.values()) == pytest.approx(1 / 3)


async def test_classify_before_configure_raises() -> None:
    with pytest.raises(RuntimeError, match="before configure"):
        await KnnBackend(fake_embed).classify(_request("hi"), deadline_ms=300)


async def test_embeds_off_the_event_loop_thread_and_only_once_per_phrase_set() -> None:
    loop_thread = threading.get_ident()
    calls: list[tuple[int, int]] = []

    def embed(texts: Sequence[str]) -> npt.NDArray[np.float64]:
        calls.append((threading.get_ident(), len(texts)))
        return fake_embed(texts)

    backend = KnnBackend(embed)
    await backend.configure(PROFILES)
    await backend.classify(_request("set a reminder"), deadline_ms=300)
    await backend.classify(_request("what's the weather"), deadline_ms=300)

    # One call embeds every description and example; each classify embeds just the request.
    assert [count for _, count in calls] == [7, 1, 1]
    assert all(thread != loop_thread for thread, _ in calls)


# --- ProfileMatcher -----------------------------------------------------------------------------

MATCHED_PROFILES = {
    "stock-picker": ProfileConfig(
        description="Portfolio, holdings, stock research, trades", aliases=("stocks", "the stock picker")
    ),
    "general": ProfileConfig(description="Weather, reminders, and anything else"),
}


class _CountingEmbedder:
    """``fake_embed``, counting the texts it is asked to embed."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, texts: Sequence[str]) -> npt.NDArray[np.floating]:
        self.texts.extend(texts)
        return fake_embed(texts)


@pytest.mark.parametrize(
    ("words", "profile"),
    [("stock-picker", "stock-picker"), ("the stock picker please", "stock-picker"), ("general", "general")],
)
async def test_a_name_or_alias_resolves_without_embedding_anything(words: str, profile: str) -> None:
    embed = _CountingEmbedder()

    assert await ProfileMatcher(MATCHED_PROFILES, embed).resolve(words) == profile
    assert embed.texts == []


@pytest.mark.parametrize(
    ("words", "profile"),
    [
        ("go to the stock trader profile", "stock-picker"),  # "stock" is in its name and description
        ("use my holdings agent", "stock-picker"),
        ("can I talk to the reminders assistant", "general"),
    ],
)
async def test_other_words_resolve_by_meaning_to_a_clear_winner(words: str, profile: str) -> None:
    assert await ProfileMatcher(MATCHED_PROFILES, fake_embed).resolve(words) == profile


@pytest.mark.parametrize(
    "words",
    [
        "switch to Sarah",  # like nothing a profile is
        "use my agent",  # nothing left once the asking is dropped
        "go to the stocks general agent",  # as near to one as to the other
    ],
)
async def test_words_without_a_clear_winner_name_no_profile(words: str) -> None:
    assert await ProfileMatcher(MATCHED_PROFILES, fake_embed).resolve(words) is None


async def test_profile_phrases_are_embedded_once() -> None:
    embed = _CountingEmbedder()
    matcher = ProfileMatcher(MATCHED_PROFILES, embed)

    await matcher.resolve("the trades agent")
    await matcher.resolve("the holdings agent")

    phrases = ["stock picker", "stocks", "the stock picker", MATCHED_PROFILES["stock-picker"].description]
    phrases += ["general", MATCHED_PROFILES["general"].description]
    assert embed.texts == [*phrases, "trades", "holdings"]


@pytest.mark.parametrize(
    ("words", "profile"),
    [
        ("my holdings", None),  # "holdings" is only in stock-picker's description
        ("my holdings agent", "stock-picker"),
        ("the reminders", None),
        ("the reminders bot", "general"),
        ("the stock trader", "stock-picker"),  # near its name, so no agent noun is needed
    ],
)
async def test_a_description_counts_only_when_the_words_call_it_an_agent(
    words: str, profile: str | None
) -> None:
    assert await ProfileMatcher(MATCHED_PROFILES, fake_embed).resolve(words) == profile


async def test_a_further_request_in_the_words_is_not_matched() -> None:
    embed = _CountingEmbedder()

    await ProfileMatcher(MATCHED_PROFILES, embed).resolve(
        "connect me with the investing agent, what's Apple at"
    )

    assert embed.texts[-1] == "investing"


class _FailingLoad:
    """Stands in for ``load_model``: fails until ``works`` is set, counting its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.works = False

    def __call__(self, model: str) -> Embedder:
        self.calls += 1
        if not self.works:
            raise OSError("no network")
        return fake_embed


async def test_a_failed_model_load_is_not_retried_for_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    load = _FailingLoad()
    monkeypatch.setattr(knn, "load_model", load)
    now = [0.0]
    matcher = ProfileMatcher(MATCHED_PROFILES, clock=lambda: now[0])

    with pytest.raises(OSError):
        await matcher.resolve("the holdings agent")
    now[0] = knn.MODEL_RETRY_S - 1
    load.works = True
    with pytest.raises(RuntimeError):
        await matcher.resolve("the holdings agent")
    assert load.calls == 1, "remembered: not tried again"
    assert await matcher.resolve("stocks") == "stock-picker", "names and aliases need no model"

    now[0] = knn.MODEL_RETRY_S
    assert await matcher.resolve("the holdings agent") == "stock-picker"
    assert load.calls == 2


async def test_prewarm_loads_the_model_and_logs_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    load = _FailingLoad()
    monkeypatch.setattr(knn, "load_model", load)
    matcher = ProfileMatcher(MATCHED_PROFILES)

    await matcher.prewarm()  # does not raise
    assert "could not load the switch-target model" in caplog.text

    load.works = True
    warm = ProfileMatcher(MATCHED_PROFILES)
    await warm.prewarm()
    assert load.calls == 2
    assert await warm.resolve("the holdings agent") == "stock-picker"
    assert load.calls == 2, "loaded once, by the prewarm"
