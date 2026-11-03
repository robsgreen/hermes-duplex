"""The ``decisions`` router backend: the OpenAI Decisions API, called over HTTP with ``aiohttp``.

One ``POST {base_url}/decisions`` asks three questions about a request: which choice (profile or
``_control``) should handle it, how much effort it needs, and whether it follows up on the previous
task. The answers' probability lists become a ``BackendResult``.
"""

import time
from collections.abc import Mapping, Sequence
from typing import Any, Self, get_args

import aiohttp

from ..config import DecisionsConfig, Secrets
from .base import BackendResult, Effort, ProfileSpec, RouteRequest

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
HISTORY_LINES = 4
"""How many of the request's most recent history lines are sent along with it."""

MIN_CHOICES = 2
MAX_CHOICES = 255
"""The API's bounds on the number of choices in one choice question."""

_EFFORTS: tuple[Effort, ...] = get_args(Effort)

_PROFILE_INSTRUCTIONS = (
    "Pick the profile that should handle the last message, which is the user's request. "
    "Earlier messages are the recent conversation, for context only."
)
_EFFORT_INSTRUCTIONS = (
    "Pick how much reasoning the last message's request needs: low for a quick lookup or a simple "
    "action, medium for an ordinary task, high for multi-step research or analysis."
)
_FOLLOWUP_INSTRUCTIONS = "The request continues the previous task's topic."


class DecisionsError(Exception):
    """The Decisions API did not answer in time, failed, refused, or sent an answer this backend
    cannot read. The message names which, without the API key."""


class DecisionsBackend:
    """Scores requests with one OpenAI Decisions call each: profile, effort, and follow-up.

    It cannot tell ``_control`` intents apart, so ``control_intent`` is always None. The backend owns
    one ``aiohttp.ClientSession``, opened on first ``classify``; call ``close()`` (or use the backend
    as an async context manager) when done.
    """

    name = "decisions"

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL) -> None:
        """``api_key`` is sent as the bearer token; ``base_url`` is the API root, e.g. ``DEFAULT_BASE_URL``."""
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._url = f"{base_url.rstrip('/')}/decisions"
        self._questions: list[dict[str, Any]] = []
        self._choices: tuple[str, ...] = ()
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_config(cls, config: DecisionsConfig, secrets: Secrets) -> Self:
        """A backend with the key from the env var ``config.api_key_env`` names, and the configured
        model and base URL where set."""
        return cls(
            secrets.get(config.api_key_env),
            model=config.model or DEFAULT_MODEL,
            base_url=config.base_url or DEFAULT_BASE_URL,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session. The backend opens a new one if it is used again."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        """Build the three questions, one profile choice per spec with its description; replaces any
        earlier configuration.

        Raises ``ValueError`` unless there are 2 to 255 choices with unique names.
        """
        names = tuple(spec.name for spec in profiles)
        if len(set(names)) != len(names) or not MIN_CHOICES <= len(names) <= MAX_CHOICES:
            raise ValueError(f"the Decisions API needs {MIN_CHOICES} to {MAX_CHOICES} unique choices")
        self._choices = names
        self._questions = [
            {
                "type": "choice",
                "name": "profile",
                "instructions": _PROFILE_INSTRUCTIONS,
                "choices": [_choice(spec.name, spec.description) for spec in profiles],
            },
            {
                "type": "choice",
                "name": "effort",
                "instructions": _EFFORT_INSTRUCTIONS,
                "choices": [{"value": effort} for effort in _EFFORTS],
            },
            {"type": "predicate", "name": "followup", "instructions": _FOLLOWUP_INSTRUCTIONS},
        ]

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        """Ask the Decisions API about ``req.text``, with its last few history lines as context.

        Gives up after ``deadline_ms``. Raises ``DecisionsError`` when the call times out, cannot
        connect, gets a non-200 reply, refuses the profile question, or holds anything other than the
        three answers with probabilities; a refused effort or follow-up question leaves that score
        None. Raises ``RuntimeError`` before ``configure``.
        """
        if not self._questions:
            raise RuntimeError("DecisionsBackend.classify called before configure")
        lines = (*req.history[-HISTORY_LINES:], req.text)
        body = {
            "model": self._model,
            "input": [{"role": "user", "content": line} for line in lines],
            "questions": self._questions,
        }
        started = time.perf_counter()
        reply = await self._post(body, deadline_ms)
        answers = _answers_by_name(reply, [question["name"] for question in self._questions])
        refused = {name for name, answer in answers.items() if answer.get("type") == "refusal"}
        if "profile" in refused:
            raise DecisionsError("Decisions API refused to answer which profile")
        return BackendResult(
            profile_probs=_probabilities(answers, "profile", self._choices),
            effort_probs=None if "effort" in refused else _probabilities(answers, "effort", _EFFORTS),
            followup_prob=None if "followup" in refused else _predicate_probability(answers, "followup"),
            latency_ms=round((time.perf_counter() - started) * 1000),
            backend=self.name,
        )

    async def _post(self, body: Mapping[str, Any], deadline_ms: int) -> Any:
        timeout = aiohttp.ClientTimeout(total=deadline_ms / 1000)
        try:
            async with self._http().post(
                self._url, json=body, headers=self._headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    raise DecisionsError(f"Decisions API replied HTTP {resp.status}")
                return await resp.json(content_type=None)
        except TimeoutError as exc:
            raise DecisionsError(f"Decisions API did not answer within {deadline_ms} ms") from exc
        except aiohttp.ClientError as exc:
            raise DecisionsError(f"Decisions API request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise DecisionsError("Decisions API replied with invalid JSON") from exc

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session


def _choice(value: str, description: str) -> dict[str, str]:
    return {"value": value, "description": description} if description else {"value": value}


def _answers_by_name(reply: Any, question_names: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    """The reply's answers keyed by question name. An answer without a name, such as a refusal,
    answers the question at its position in ``question_names``. Raises ``DecisionsError`` for a reply
    without an answers list."""
    answers = reply.get("answers") if isinstance(reply, dict) else None
    if not isinstance(answers, list):
        raise DecisionsError("Decisions API reply has no answers list")
    by_name: dict[str, Mapping[str, Any]] = {}
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            raise DecisionsError("Decisions API answer is not an object")
        name = answer.get("name")
        if name is None and index < len(question_names):
            name = question_names[index]
        by_name[str(name)] = answer
    return by_name


def _probabilities[V: str](
    answers: Mapping[str, Mapping[str, Any]], name: str, values: Sequence[V]
) -> dict[V, float]:
    """The choice answer ``name``'s probabilities over ``values``, normalized to sum to 1; a value
    the answer leaves out gets 0. Raises ``DecisionsError`` when the answer is missing, names a value
    outside ``values``, or holds no positive probability."""
    answer = _answer(answers, name, "choice")
    entries = answer.get("probabilities")
    if not isinstance(entries, list):
        raise DecisionsError(f"Decisions API answer '{name}' has no probabilities")
    probs = dict.fromkeys(values, 0.0)
    known = {str(value): value for value in values}
    for entry in entries:
        value = known.get(str(entry.get("value"))) if isinstance(entry, dict) else None
        if value is None:
            raise DecisionsError(f"Decisions API answer '{name}' has an unknown or malformed choice")
        probs[value] = _probability(entry.get("probability"), name)
    total = sum(probs.values())
    if total <= 0:
        raise DecisionsError(f"Decisions API answer '{name}' has no positive probability")
    return {value: prob / total for value, prob in probs.items()}


def _predicate_probability(answers: Mapping[str, Mapping[str, Any]], name: str) -> float:
    return _probability(_answer(answers, name, "predicate").get("probability"), name)


def _answer(answers: Mapping[str, Mapping[str, Any]], name: str, kind: str) -> Mapping[str, Any]:
    answer = answers.get(name)
    if answer is None or answer.get("type") != kind:
        raise DecisionsError(f"Decisions API reply has no {kind} answer '{name}'")
    return answer


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise DecisionsError(f"Decisions API answer '{name}' has a probability outside 0..1")
    return float(value)
