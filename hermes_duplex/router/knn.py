"""The local phrase matcher: nearest neighbour over static embeddings of each choice's phrases.

It answers in a few milliseconds and judges only the profile, never effort.
"""

import asyncio
import time
from collections.abc import Callable, Sequence

import numpy as np
import numpy.typing as npt

from .base import BackendResult, ControlIntent, ControlSpec, ProfileSpec, RouteRequest

Embedder = Callable[[Sequence[str]], npt.NDArray[np.floating]]
"""Turns n texts into an (n, d) array of embeddings. It runs in a worker thread."""

DEFAULT_MODEL = "minishlab/potion-base-8M"
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
            self._embed = await asyncio.to_thread(self._load_model)
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

    def _load_model(self) -> Embedder:
        # Imported here so that importing this module, and tests with an injected embedder, never
        # load the model library or touch the network.
        from model2vec import StaticModel

        return StaticModel.from_pretrained(self._model, force_download=False).encode


def _unit_rows(vectors: npt.NDArray[np.floating]) -> npt.NDArray[np.float64]:
    """Scale each row to unit length; an all-zero row (no known words) stays zero, matching nothing."""
    rows = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)
