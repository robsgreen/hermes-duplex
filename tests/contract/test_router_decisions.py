"""DecisionsBackend against an aiohttp fake of ``POST /v1/decisions``: request shape, answer mapping,
refusals, malformed replies, HTTP errors, and the deadline."""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Self

import pytest
from aiohttp import web

from hermes_duplex.config import ControlExamples, DecisionsConfig, Secrets
from hermes_duplex.router.base import (
    CONTROL_DESCRIPTION,
    ProfileSpec,
    RouterBackend,
    RouteRequest,
    control_spec,
)
from hermes_duplex.router.decisions import DEFAULT_MODEL, DecisionsBackend, DecisionsError

API_KEY = "sk-decisions-test-key"

PROFILES = (
    ProfileSpec("stock-picker", "Portfolio, holdings, stock research, trades", ("how's my portfolio",)),
    ProfileSpec("general", "Anything else", ("what's the weather",)),
    control_spec(ControlExamples()),
)


def _choice_answer(name: str, probs: dict[str, float]) -> dict[str, Any]:
    top = max(probs, key=lambda value: probs[value])
    return {
        "type": "choice",
        "name": name,
        "choice": top,
        "confidence": probs[top],
        "probabilities": [{"value": value, "probability": prob} for value, prob in probs.items()],
    }


def _reply(*answers: dict[str, Any]) -> dict[str, Any]:
    return {"answers": list(answers), "model": DEFAULT_MODEL, "usage": {"input_tokens": 42}}


GOOD_ANSWERS = (
    _choice_answer("profile", {"stock-picker": 0.7, "general": 0.2, "_control": 0.1}),
    _choice_answer("effort", {"low": 0.1, "medium": 0.3, "high": 0.6}),
    {"type": "predicate", "name": "followup", "probability": 0.25},
)


@dataclass
class FakeDecisionsApi:
    """Serves ``POST /v1/decisions``: records each request, waits ``delay_s``, then sends ``status``
    with ``body`` (JSON) or ``raw`` (text) when set."""

    status: int = 200
    body: Any = field(default_factory=lambda: _reply(*GOOD_ANSWERS))
    raw: str | None = None
    delay_s: float = 0.0
    requests: list[web.Request] = field(default_factory=list)
    bodies: list[Any] = field(default_factory=list)
    base_url: str = ""
    _runner: web.AppRunner | None = None

    async def __aenter__(self) -> Self:
        app = web.Application()
        app.add_routes([web.post("/v1/decisions", self._decide)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.base_url = f"http://127.0.0.1:{self._runner.addresses[0][1]}/v1"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _decide(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request)
        self.bodies.append(await request.json())
        await asyncio.sleep(self.delay_s)
        if self.raw is not None:
            return web.Response(status=self.status, text=self.raw, content_type="application/json")
        return web.json_response(self.body, status=self.status)


@pytest.fixture
async def fake() -> AsyncIterator[FakeDecisionsApi]:
    async with FakeDecisionsApi() as api:
        yield api


@pytest.fixture
async def backend(fake: FakeDecisionsApi) -> AsyncIterator[DecisionsBackend]:
    async with DecisionsBackend(API_KEY, base_url=fake.base_url) as decisions:
        await decisions.configure(PROFILES)
        yield decisions


def _request(text: str = "should I buy NVDA", history: tuple[str, ...] = ()) -> RouteRequest:
    return RouteRequest(text=text, speaker="rob", history=history, active_profile=None, hint=None)


# --- request shape ---


async def test_request_sends_model_key_input_and_three_questions(
    backend: DecisionsBackend, fake: FakeDecisionsApi
) -> None:
    history = ("one", "two", "three", "four", "five")
    await backend.classify(_request("and TSLA?", history), deadline_ms=1000)

    [request] = fake.requests
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    [body] = fake.bodies
    assert body["model"] == "gpt-6-luna"
    assert body["input"] == [
        {"role": "user", "content": line} for line in ("two", "three", "four", "five", "and TSLA?")
    ]
    profile, effort, followup = body["questions"]
    assert profile["type"] == "choice"
    assert profile["name"] == "profile"
    assert profile["instructions"]
    assert profile["choices"] == [
        {"value": "stock-picker", "description": "Portfolio, holdings, stock research, trades"},
        {"value": "general", "description": "Anything else"},
        {"value": "_control", "description": CONTROL_DESCRIPTION},
    ]
    assert effort["type"] == "choice"
    assert effort["name"] == "effort"
    assert effort["instructions"]
    assert effort["choices"] == [{"value": "low"}, {"value": "medium"}, {"value": "high"}]
    assert followup == {
        "type": "predicate",
        "name": "followup",
        "instructions": "The request continues the previous task's topic.",
    }


async def test_request_without_history_sends_only_the_text(
    backend: DecisionsBackend, fake: FakeDecisionsApi
) -> None:
    await backend.classify(_request("what's the weather"), deadline_ms=1000)

    assert fake.bodies[0]["input"] == [{"role": "user", "content": "what's the weather"}]


async def test_profile_without_description_is_sent_as_a_bare_choice(fake: FakeDecisionsApi) -> None:
    async with DecisionsBackend(API_KEY, base_url=fake.base_url) as backend:
        await backend.configure((ProfileSpec("general", "", ()), PROFILES[2]))
        fake.body = _reply(_choice_answer("profile", {"general": 1.0}), *GOOD_ANSWERS[1:])
        await backend.classify(_request(), deadline_ms=1000)

    assert fake.bodies[0]["questions"][0]["choices"][0] == {"value": "general"}


async def test_from_config_uses_the_key_from_the_named_env_var_and_configured_endpoint(
    fake: FakeDecisionsApi,
) -> None:
    config = DecisionsConfig(
        api_key_env="OPENAI_DECISIONS_KEY", model="gpt-6-luna-preview", base_url=fake.base_url
    )
    async with DecisionsBackend.from_config(
        config, Secrets({"OPENAI_DECISIONS_KEY": "sk-from-env"})
    ) as backend:
        await backend.configure(PROFILES)
        await backend.classify(_request(), deadline_ms=1000)

    assert fake.requests[0].headers["Authorization"] == "Bearer sk-from-env"
    assert fake.bodies[0]["model"] == "gpt-6-luna-preview"


# --- response mapping ---


async def test_answers_map_to_backend_result(backend: DecisionsBackend) -> None:
    result = await backend.classify(_request(), deadline_ms=1000)

    assert result.backend == "decisions"
    assert result.profile_probs == pytest.approx({"stock-picker": 0.7, "general": 0.2, "_control": 0.1})
    assert result.effort_probs == pytest.approx({"low": 0.1, "medium": 0.3, "high": 0.6})
    assert result.followup_prob == 0.25
    assert result.control_intent is None
    assert result.latency_ms >= 0


async def test_answers_are_matched_by_name_and_missing_choices_get_zero(
    backend: DecisionsBackend, fake: FakeDecisionsApi
) -> None:
    fake.body = _reply(
        {"type": "predicate", "name": "followup", "probability": 0.9},
        _choice_answer("effort", {"low": 0.5, "medium": 0.5}),
        _choice_answer("profile", {"_control": 0.6, "general": 0.2}),
    )

    result = await backend.classify(_request("any updates"), deadline_ms=1000)

    assert result.profile_probs == pytest.approx({"stock-picker": 0.0, "general": 0.25, "_control": 0.75})
    assert result.effort_probs == pytest.approx({"low": 0.5, "medium": 0.5, "high": 0.0})
    assert result.followup_prob == 0.9


# --- refusals and failures ---


REFUSAL = {"type": "refusal", "refusal": "I can't help with that."}


async def test_refused_profile_raises(backend: DecisionsBackend, fake: FakeDecisionsApi) -> None:
    fake.body = _reply(REFUSAL, *GOOD_ANSWERS[1:])

    with pytest.raises(DecisionsError, match="refused"):
        await backend.classify(_request(), deadline_ms=1000)


async def test_refused_effort_and_followup_leave_those_scores_unjudged(
    backend: DecisionsBackend, fake: FakeDecisionsApi
) -> None:
    fake.body = _reply(GOOD_ANSWERS[0], REFUSAL, {**REFUSAL, "name": "followup"})

    result = await backend.classify(_request(), deadline_ms=1000)

    assert result.profile_probs == pytest.approx({"stock-picker": 0.7, "general": 0.2, "_control": 0.1})
    assert result.effort_probs is None
    assert result.followup_prob is None


def _without(name: str) -> dict[str, Any]:
    return _reply(*(answer for answer in GOOD_ANSWERS if answer["name"] != name))


def _with_profile(probabilities: Any) -> dict[str, Any]:
    profile = {**GOOD_ANSWERS[0], "probabilities": probabilities}
    return _reply(profile, *GOOD_ANSWERS[1:])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"model": DEFAULT_MODEL}, id="no-answers"),
        pytest.param({"answers": "profile"}, id="answers-not-a-list"),
        pytest.param({"answers": ["profile"]}, id="answer-not-an-object"),
        pytest.param(_without("profile"), id="missing-profile"),
        pytest.param(_without("effort"), id="missing-effort"),
        pytest.param(_without("followup"), id="missing-followup"),
        pytest.param(_with_profile(None), id="no-probabilities"),
        pytest.param(_with_profile([{"value": "home", "probability": 0.9}]), id="unknown-choice"),
        pytest.param(_with_profile(["general"]), id="probability-entry-not-an-object"),
        pytest.param(_with_profile([{"value": "general", "probability": 1.5}]), id="probability-above-1"),
        pytest.param(
            _with_profile([{"value": "general", "probability": "high"}]), id="probability-not-a-number"
        ),
        pytest.param(_with_profile([{"value": "general", "probability": 0.0}]), id="all-zero"),
        pytest.param(
            _reply(*GOOD_ANSWERS[:2], {"type": "choice", "name": "followup", "probabilities": []}),
            id="followup-wrong-type",
        ),
        pytest.param(
            _reply(*GOOD_ANSWERS[:2], {"type": "predicate", "name": "followup"}), id="followup-no-probability"
        ),
    ],
)
async def test_malformed_reply_raises(backend: DecisionsBackend, fake: FakeDecisionsApi, body: Any) -> None:
    fake.body = body

    with pytest.raises(DecisionsError):
        await backend.classify(_request(), deadline_ms=1000)


async def test_invalid_json_raises(backend: DecisionsBackend, fake: FakeDecisionsApi) -> None:
    fake.raw = "{not json"

    with pytest.raises(DecisionsError, match="invalid JSON"):
        await backend.classify(_request(), deadline_ms=1000)


@pytest.mark.parametrize("status", [500, 503, 429, 401])
async def test_http_error_raises_with_status_and_without_the_key(
    backend: DecisionsBackend, fake: FakeDecisionsApi, status: int
) -> None:
    fake.status = status
    fake.body = {"error": {"message": "upstream failed", "type": "server_error"}}

    with pytest.raises(DecisionsError, match=f"HTTP {status}") as caught:
        await backend.classify(_request(), deadline_ms=1000)
    assert API_KEY not in str(caught.value)


async def test_slow_reply_beyond_the_deadline_raises_at_the_deadline(
    backend: DecisionsBackend, fake: FakeDecisionsApi
) -> None:
    fake.delay_s = 1.0

    started = time.perf_counter()
    with pytest.raises(DecisionsError, match="within 100 ms"):
        await backend.classify(_request(), deadline_ms=100)
    assert time.perf_counter() - started < 0.5


async def test_unreachable_api_raises() -> None:
    async with FakeDecisionsApi() as api:
        base_url = api.base_url
    async with DecisionsBackend(API_KEY, base_url=base_url) as backend:
        await backend.configure(PROFILES)
        with pytest.raises(DecisionsError, match="request failed"):
            await backend.classify(_request(), deadline_ms=1000)


# --- configuration ---


@pytest.mark.parametrize(
    "profiles",
    [
        pytest.param(PROFILES[:1], id="one-choice"),
        pytest.param((PROFILES[0], PROFILES[0]), id="duplicate-names"),
        pytest.param(tuple(ProfileSpec(f"p{n}", "", ()) for n in range(256)), id="too-many"),
    ],
)
async def test_configure_rejects_choice_counts_the_api_does_not_accept(
    profiles: tuple[ProfileSpec, ...],
) -> None:
    with pytest.raises(ValueError, match="unique choices"):
        await DecisionsBackend(API_KEY).configure(profiles)


def test_backend_implements_the_router_backend_interface() -> None:
    backend: RouterBackend = DecisionsBackend(API_KEY)

    assert backend.name == "decisions"


async def test_classify_before_configure_raises() -> None:
    with pytest.raises(RuntimeError, match="before configure"):
        await DecisionsBackend(API_KEY).classify(_request(), deadline_ms=1000)
