"""The ``jev`` router backend: TypeSafe's System One API (model Jev), called over HTTP with ``aiohttp``.

One ``POST {base_url}/v1/systemone`` asks two questions about a request: which choice (profile or
``_control``) should handle it, and how much effort it needs. The answers' probabilities become a ``BackendResult``. ``base_url`` is configurable so a
service that speaks the same API can stand in for ``https://api.typesafe.ai``.

Wire shape, from the API reference https://docs.typesafe.ai/api/system-one.md (OpenAPI 0.2.0),
https://docs.typesafe.ai/concepts/state.md, and https://docs.typesafe.ai/api/errors.md:

- Request: header ``Authorization: Bearer <key>``; body ``{"model": "jev-latest", "state": <string,
  object, or array>, "questions": {<name>: <question>}}``. A choice question is ``{"type": "choice",
  "instructions": str, "criteria": {<choice>: <description or null>}}`` with 1 to 255 choices; a
  yes/no question is ``{"type": "noul", "instructions": str}``.
- Response (200): ``{"model": str, "answers": {<name>: <answer>}, "usage": {...}}``, answers keyed by
  question name. A choice answer is ``{"type": "choice", "choice": str, "confidence": float,
  "probabilities": {<choice>: float}}``; a noul answer is ``{"type": "noul", "noul": float}``, the
  probability of yes.
- Errors: a non-200 status (400, 401, 413, 415, 422, 429, 529) with a JSON body.

``_request_body`` and ``_parse_answers`` hold the whole mapping to and from that shape.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self, get_args

import aiohttp

from ..config import JevConfig, Secrets
from .base import BackendResult, Effort, ProfileSpec, RouteRequest

DEFAULT_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
HISTORY_LINES = 4
"""How many of the request's most recent history lines are sent along with it."""

MAX_CHOICES = 255
"""The API's limit on the number of choices in one choice question."""

_EFFORTS: tuple[Effort, ...] = get_args(Effort)

_PROFILE_INSTRUCTIONS = (
    "Pick the profile that should handle the request. "
    "The earlier messages are the recent conversation, for context only."
)
_EFFORT_INSTRUCTIONS = "Pick how much reasoning the request needs."
_EFFORT_CRITERIA: Mapping[Effort, str] = {
    "low": "A quick lookup or a simple action",
    "medium": "An ordinary task",
    "high": "Multi-step research or analysis",
}


class JevError(Exception):
    """The System One API did not answer in time, failed, or sent an answer this backend cannot
    read. The message names which, without the API key."""


@dataclass(frozen=True)
class _Answers:
    """The two answers of one reply, as probabilities."""

    profile_probs: dict[str, float]
    effort_probs: dict[Effort, float]


class JevBackend:
    """Scores requests with one System One call each: profile and effort.

    It cannot tell ``_control`` intents apart, so ``control_intent`` is always None. The backend owns
    one ``aiohttp.ClientSession``, opened on first ``classify``; call ``close()`` (or use the backend
    as an async context manager) when done.
    """

    name = "jev"

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL) -> None:
        """``api_key`` is sent as the bearer token; ``base_url`` is the API root without ``/v1``,
        e.g. ``DEFAULT_BASE_URL``."""
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._url = f"{base_url.rstrip('/')}/v1/systemone"
        self._profiles: tuple[ProfileSpec, ...] = ()
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_config(cls, config: JevConfig, secrets: Secrets) -> Self:
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
        """Route to ``profiles``, one choice each with its description; replaces any earlier
        configuration.

        Raises ``ValueError`` unless there are 1 to 255 choices with unique names.
        """
        names = {spec.name for spec in profiles}
        if len(names) != len(profiles) or not 1 <= len(names) <= MAX_CHOICES:
            raise ValueError(f"the System One API needs 1 to {MAX_CHOICES} unique choices")
        self._profiles = tuple(profiles)

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        """Ask the System One API about ``req.text``, with its last few history lines as context.

        Gives up after ``deadline_ms``. Raises ``JevError`` when the call times out, cannot connect,
        gets a non-200 reply, or the reply lacks either of the two answers with valid probabilities;
        raises ``RuntimeError`` before ``configure``.
        """
        if not self._profiles:
            raise RuntimeError("JevBackend.classify called before configure")
        started = time.perf_counter()
        reply = await self._post(_request_body(self._model, self._profiles, req), deadline_ms)
        answers = _parse_answers(reply, tuple(spec.name for spec in self._profiles))
        return BackendResult(
            profile_probs=answers.profile_probs,
            effort_probs=answers.effort_probs,
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
                    raise JevError(f"System One API replied HTTP {resp.status}")
                return await resp.json(content_type=None)
        except TimeoutError as exc:
            raise JevError(f"System One API did not answer within {deadline_ms} ms") from exc
        except aiohttp.ClientError as exc:
            raise JevError(f"System One API request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise JevError("System One API replied with invalid JSON") from exc

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session


def _request_body(model: str, profiles: Sequence[ProfileSpec], req: RouteRequest) -> dict[str, Any]:
    """The System One request body for ``req``: the request and its recent history as the state,
    and the profile and effort questions. A profile without a description is sent with a
    null description, which the API reads as the choice's name alone."""
    return {
        "model": model,
        "state": {"request": req.text, "earlier_messages": list(req.history[-HISTORY_LINES:])},
        "questions": {
            "profile": {
                "type": "choice",
                "instructions": _PROFILE_INSTRUCTIONS,
                "criteria": {spec.name: spec.description or None for spec in profiles},
            },
            "effort": {
                "type": "choice",
                "instructions": _EFFORT_INSTRUCTIONS,
                "criteria": dict(_EFFORT_CRITERIA),
            },
        },
    }


def _parse_answers(reply: Any, choices: Sequence[str]) -> _Answers:
    """The profile and effort answers of ``reply``. Raises ``JevError`` when the reply
    has no answers object or any answer is missing, of the wrong type, or malformed."""
    answers = reply.get("answers") if isinstance(reply, dict) else None
    if not isinstance(answers, dict):
        raise JevError("System One API reply has no answers object")
    return _Answers(
        profile_probs=_choice_probabilities(answers, "profile", choices),
        effort_probs=_choice_probabilities(answers, "effort", _EFFORTS),
    )


def _choice_probabilities[V: str](
    answers: Mapping[str, Any], name: str, values: Sequence[V]
) -> dict[V, float]:
    """The choice answer ``name``'s probabilities over ``values``, normalized to sum to 1; a value
    the answer leaves out gets 0. Raises ``JevError`` when the answer is missing, names a value
    outside ``values``, or holds no positive probability."""
    probabilities = _answer(answers, name, "choice").get("probabilities")
    if not isinstance(probabilities, dict):
        raise JevError(f"System One API answer '{name}' has no probabilities")
    probs = dict.fromkeys(values, 0.0)
    known = {str(value): value for value in values}
    for choice, probability in probabilities.items():
        value = known.get(choice)
        if value is None:
            raise JevError(f"System One API answer '{name}' has an unknown choice")
        probs[value] = _probability(probability, name)
    total = sum(probs.values())
    if total <= 0:
        raise JevError(f"System One API answer '{name}' has no positive probability")
    return {value: prob / total for value, prob in probs.items()}


def _answer(answers: Mapping[str, Any], name: str, kind: str) -> Mapping[str, Any]:
    answer = answers.get(name)
    if not isinstance(answer, dict) or answer.get("type") != kind:
        raise JevError(f"System One API reply has no {kind} answer '{name}'")
    return answer


def _probability(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise JevError(f"System One API answer '{name}' has a probability outside 0..1")
    return float(value)
