"""The local phrase matchers: nearest neighbour over static embeddings.

``KnnBackend`` scores a request against each router choice's phrases; it answers in a few
milliseconds and judges only the choice, never effort. ``ProfileMatcher`` finds the profile a person's
words for an agent mean, when they name none exactly.
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

import numpy as np
import numpy.typing as npt

from ..config import ProfileConfig
from ..switching import resolve_profile, spoken_form, target_words
from .base import BackendResult, ControlIntent, ControlSpec, ProfileSpec, RouteRequest

logger = logging.getLogger(__name__)

Embedder = Callable[[Sequence[str]], npt.NDArray[np.floating]]
"""Turns n texts into an (n, d) array of embeddings. It runs in a worker thread."""

DEFAULT_MODEL = "minishlab/potion-base-8M"
TARGET_MIN_SIMILARITY = 0.4
"""``ProfileMatcher`` takes a profile for words that call it an agent only when their cosine
similarity to one of its phrases is at least this. Checked with ``DEFAULT_MODEL`` against the
held-out switch phrasings (``tests/fixtures/router_eval/switch_heldout.yaml``, five profiles, never
used to choose it): every such target that resolves to its profile scores 0.416 ("finance agent") to
0.797 ("headlines bot"), while the words that should name no profile ("regular", "real estate",
"leasing", "insurance") score 0.254 or less. On the router eval's own phrases resolving targets score
0.448 or more."""
NAME_MIN_SIMILARITY = 0.85
"""Words that do not call what they ask for an agent ("switch to stock pickers", "talk to my stock
broker") resolve to a profile only when their cosine similarity to one of its names or aliases is at
least this: a near-exact name, such as a plural ("stock pickers" 0.95, "travels" 0.89, "coders"
0.85). On the held-out switch phrasings the words of tasks that only sound like a switch score 0.723
or less ("stock trader"; "stock broker" 0.633, "stock photos" 0.618, "programmer" 0.468), and in
review probes 0.731 ("travel card")."""
TARGET_MARGIN = 0.15
"""How much the top profile's similarity must beat every other profile's by; a closer call names no
single profile. On the held-out switch phrasings every resolving target wins by 0.28 or more (the
narrowest is "finance agent", 0.416 against 0.132), so the margin turns away only near ties."""
MODEL_RETRY_S = 300.0
"""After the model fails to load, ``ProfileMatcher`` matches names and aliases only for this long
before it tries to load it again, so a missing download never slows every switch."""
DEFAULT_TEMPERATURE = 0.05
"""Softmax temperature over cosine scores; lower is sharper. Cosines between paraphrases and
unrelated phrases differ by a few tenths, which this turns into a clear winner."""


class KnnBackend:
    """Scores a request by its cosine similarity to each choice's description and examples.

    A choice's score is its best similarity to any of its phrases; ``profile_probs`` is the softmax
    of those scores. For ``_control`` (a ``ControlSpec``), the intent of its nearest phrase is
    reported as ``control_intent`` and that phrase's similarity as ``control_similarity``.
    """

    name = "knn"

    def __init__(
        self,
        embed: Embedder | None = None,
        *,
        model: str = DEFAULT_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        """``embed`` defaults to the model2vec static model ``model``, loaded on first ``configure``
        from the Hugging Face cache, or downloaded into it when missing."""
        self._embed = embed
        self._model = model
        self._temperature = temperature
        self._choices: list[str] = []
        self._phrase_choice = np.empty(0, dtype=np.intp)
        """Index into ``_choices`` of the choice each embedded phrase belongs to."""
        self._control_rows: list[tuple[int, ControlIntent]] = []
        """Row in ``_phrase_vectors`` and intent of each ``_control`` phrase."""
        self._phrase_vectors = np.empty((0, 0))
        """Unit-length embedding of each phrase, one row per phrase."""

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        """Embed every choice's description and examples; replaces any earlier configuration."""
        choices: list[str] = []
        phrases: list[str] = []
        phrase_choice: list[int] = []
        control_rows: list[tuple[int, ControlIntent]] = []
        for index, spec in enumerate(profiles):
            choices.append(spec.name)
            intents = spec.intents if isinstance(spec, ControlSpec) else {}
            for phrase in (spec.description, *spec.examples):
                if phrase:
                    if (intent := intents.get(phrase)) is not None:
                        control_rows.append((len(phrases), intent))
                    phrases.append(phrase)
                    phrase_choice.append(index)

        if self._embed is None:
            self._embed = await asyncio.to_thread(load_model, self._model)
        vectors = await asyncio.to_thread(self._embed, phrases)
        self._choices = choices
        self._phrase_choice = np.array(phrase_choice, dtype=np.intp)
        self._control_rows = control_rows
        self._phrase_vectors = _unit_rows(vectors)

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        """Score ``req.text``; ignores history and ``deadline_ms`` (it answers well within any deadline).

        Raises ``RuntimeError`` before ``configure``.
        """
        if self._embed is None or not self._choices:
            raise RuntimeError("KnnBackend.classify called before configure")
        started = time.perf_counter()
        query = _unit_rows(await asyncio.to_thread(self._embed, [req.text]))[0]
        similarity = self._phrase_vectors @ query

        best = np.full(len(self._choices), -1.0)
        np.maximum.at(best, self._phrase_choice, similarity)
        weights = np.exp((best - best.max()) / self._temperature)
        probs = weights / weights.sum()

        nearest_control = max(self._control_rows, key=lambda row: similarity[row[0]], default=None)
        control_intent: ControlIntent | None = None
        control_similarity: float | None = None
        if nearest_control is not None:
            phrase_row, control_intent = nearest_control
            control_similarity = float(similarity[phrase_row])

        return BackendResult(
            profile_probs={choice: float(p) for choice, p in zip(self._choices, probs, strict=True)},
            effort_probs=None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            backend=self.name,
            control_intent=control_intent,
            control_similarity=control_similarity,
        )

    async def close(self) -> None:
        """Does nothing: the backend holds nothing open."""


class ProfileMatcher:
    """Resolves a person's words for an agent ("the stock trader", "go to my portfolio agent") to a
    profile, by name first and then by meaning.

    Words that start with a profile's name or alias resolve as a spoken switch target does
    (``switching.resolve_profile``). Any others are reduced to the words that say which agent
    (``switching.target_words``) and compared, by cosine similarity of static embeddings, with each
    profile's name and aliases and, only when the person called it an agent, profile, assistant, or
    bot (``TargetWords.names_an_agent``: "the investing agent", not "my stock broker"), its
    description; a profile's score is its best similarity. The top profile is taken when its score is
    at least ``TARGET_MIN_SIMILARITY`` (``NAME_MIN_SIMILARITY``, a near-exact name, for words that do
    not call it an agent) and beats every other profile's by at least ``TARGET_MARGIN``; otherwise the
    words name no single profile. Whether the person may use the profile is not considered here.
    """

    def __init__(
        self,
        profiles: Mapping[str, ProfileConfig],
        embed: Embedder | None = None,
        *,
        model: str = DEFAULT_MODEL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``profiles`` are the configured profiles, in config order. ``embed`` defaults to the
        model2vec static model ``model``, shared with ``KnnBackend`` and loaded by ``prewarm`` or the
        first ``resolve`` that needs it. ``clock`` returns seconds and times ``MODEL_RETRY_S``."""
        self._names = {name: profile.aliases for name, profile in profiles.items()}
        self._phrases = [
            _ProfilePhrase(name, phrase, is_description)
            for name, profile in profiles.items()
            for phrase, is_description in (
                (" ".join(spoken_form(name)), False),
                *((alias, False) for alias in profile.aliases),
                (profile.description, True),
            )
            if phrase
        ]
        self._embed = embed
        self._model = model
        self._clock = clock
        self._vectors: npt.NDArray[np.float64] | None = None
        self._failed_s: float | None = None
        """``clock()`` when loading the model last failed, or None."""
        self._ready = asyncio.Lock()

    async def prewarm(self) -> None:
        """Load the model and embed every profile's phrases now, so the first switch by meaning does not
        wait for them; a failure is logged, not raised."""
        if not self._phrases:
            return
        try:
            await self._phrase_vectors()
        except Exception:
            logger.exception("could not load the switch-target model; switches match names only for now")

    async def resolve(self, words: str) -> str | None:
        """The profile ``words`` mean, by the rule in the class docstring; None when they name no
        single profile. Raises what the embedding model raises when it cannot be loaded, and, for
        ``MODEL_RETRY_S`` after that, ``RuntimeError`` at once without trying again."""
        if (exact := resolve_profile(words, self._names)) is not None:
            return exact
        wanted, names_an_agent = target_words(words)
        if not wanted or not self._phrases:
            return None
        vectors = await self._phrase_vectors()
        assert self._embed is not None
        query = _unit_rows(await asyncio.to_thread(self._embed, [wanted]))[0]
        best: dict[str, float] = {}
        for phrase, similarity in zip(self._phrases, vectors @ query, strict=True):
            if names_an_agent or not phrase.is_description:
                best[phrase.profile] = max(best.get(phrase.profile, -1.0), float(similarity))
        ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
        (top, score), runner_up = ranked[0], ranked[1][1] if len(ranked) > 1 else -1.0
        floor = TARGET_MIN_SIMILARITY if names_an_agent else NAME_MIN_SIMILARITY
        clear = score >= floor and score - runner_up >= TARGET_MARGIN
        logger.info(
            "switch target %r by meaning%s: %s (similarity %.3f, runner-up %.3f)",
            wanted,
            "" if names_an_agent else ", names and aliases only",
            top if clear else "no single profile",
            score,
            runner_up,
        )
        return top if clear else None

    async def _phrase_vectors(self) -> npt.NDArray[np.float64]:
        """The unit embedding of each of ``_phrases``, embedded on first use. Raises what loading the
        model raises, and ``RuntimeError`` within ``MODEL_RETRY_S`` of that."""
        async with self._ready:
            if self._vectors is None:
                if self._embed is None:
                    if self._failed_s is not None and self._clock() - self._failed_s < MODEL_RETRY_S:
                        raise RuntimeError(
                            f"the {self._model} model failed to load less than {MODEL_RETRY_S:g} s ago"
                        )
                    try:
                        self._embed = await asyncio.to_thread(load_model, self._model)
                    except Exception:
                        self._failed_s = self._clock()
                        raise
                texts = [phrase.text for phrase in self._phrases]
                self._vectors = _unit_rows(await asyncio.to_thread(self._embed, texts))
            return self._vectors


class _ProfilePhrase(NamedTuple):
    """One phrase a profile is matched by: its name, an alias, or its description."""

    profile: str
    text: str
    is_description: bool


@functools.cache
def load_model(model: str) -> Embedder:
    """The encoder of the model2vec static model ``model``, from the Hugging Face cache, or downloaded
    into it when missing; loaded once per process and shared. Blocks: call it in a worker thread."""
    # Imported here so that importing this module, and tests with an injected embedder, never load
    # the model library or touch the network.
    from model2vec import StaticModel

    return StaticModel.from_pretrained(model, force_download=False).encode


def _unit_rows(vectors: npt.NDArray[np.floating]) -> npt.NDArray[np.float64]:
    """Scale each row to unit length; an all-zero row (no known words) stays zero, matching nothing."""
    rows = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)
