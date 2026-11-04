"""JevBackend against an aiohttp fake of ``POST /v1/systemone`` serving the documented System One shape:
request shape, answer mapping, a non-default base URL, malformed replies, HTTP errors, and the deadline."""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Self

import pytest
from aiohttp import web

from hermes_duplex.config import ControlExamples, JevConfig, Secrets
from hermes_duplex.router.base import (
    CONTROL_DESCRIPTION,
    ProfileSpec,
    RouterBackend,
    RouteRequest,
    control_spec,
)
from hermes_duplex.router.jev import DEFAULT_MODEL, JevBackend, JevError

API_KEY = "ts-jev-test-key"

PROFILES = (
    ProfileSpec("stock-picker", "Portfolio, holdings, stock research, trades", ("how's my portfolio",)),
    ProfileSpec("general", "Anything else", ("what's the weather",)),
    control_spec(ControlExamples()),
)


def _choice_answer(probs: dict[str, float]) -> dict[str, Any]:
    top = max(probs, key=lambda value: probs[value])
    return {"type": "choice", "choice": top, "confidence": probs[top], "probabilities": probs}


def _reply(answers: dict[str, Any]) -> dict[str, Any]:
    return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 120, "output_tokens": 12}}


GOOD_ANSWERS: dict[str, Any] = {
    "profile": _choice_answer({"stock-picker": 0.7, "general": 0.2, "_control": 0.1}),
    "effort": _choice_answer({"low": 0.1, "medium": 0.3, "high": 0.6}),
    "followup": {"type": "noul", "noul": 0.25},
}


@dataclass
class FakeSystemOneApi:
    """Serves ``POST {prefix}/v1/systemone``: records each request, waits ``delay_s``, then sends
    ``status`` with ``body`` (JSON) or ``raw`` (text) when set."""

    prefix: str = ""
    status: int = 200
    body: Any = field(default_factory=lambda: _reply(GOOD_ANSWERS))
    raw: str | None = None
    delay_s: float = 0.0
    requests: list[web.Request] = field(default_factory=list)
    bodies: list[Any] = field(default_factory=list)
    base_url: str = ""
    _runner: web.AppRunner | None = None

    async def __aenter__(self) -> Self:
        app = web.Application()
        app.add_routes([web.post(f"{self.prefix}/v1/systemone", self._answer)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.base_url = f"http://127.0.0.1:{self._runner.addresses[0][1]}{self.prefix}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request)
        self.bodies.append(await request.json())
        await asyncio.sleep(self.delay_s)
        if self.raw is not None:
            return web.Response(status=self.status, text=self.raw, content_type="application/json")
        return web.json_response(self.body, status=self.status)


@pytest.fixture
async def fake() -> AsyncIterator[FakeSystemOneApi]:
    async with FakeSystemOneApi() as api:
        yield api


@pytest.fixture
async def backend(fake: FakeSystemOneApi) -> AsyncIterator[JevBackend]:
    async with JevBackend(API_KEY, base_url=fake.base_url) as jev:
        await jev.configure(PROFILES)
        yield jev


def _request(text: str = "should I buy NVDA", history: tuple[str, ...] = ()) -> RouteRequest:
    return RouteRequest(text=text, speaker="rob", history=history, active_profile="general")


# --- request shape ---


async def test_request_sends_model_key_state_and_three_named_questions(
    backend: JevBackend, fake: FakeSystemOneApi
) -> None:
    history = ("one", "two", "three", "four", "five")
    await backend.classify(_request("and TSLA?", history), deadline_ms=1000)

    [request] = fake.requests
    assert request.path == "/v1/systemone"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    [body] = fake.bodies
    assert body["model"] == "jev-latest"
    assert body["state"] == {"request": "and TSLA?", "earlier_messages": ["two", "three", "four", "five"]}
    assert set(body["questions"]) == {"profile", "effort", "followup"}
    profile, effort, followup = (body["questions"][name] for name in ("profile", "effort", "followup"))
    assert profile["type"] == "choice"
    assert profile["instructions"]
    assert profile["criteria"] == {
        "stock-picker": "Portfolio, holdings, stock research, trades",
        "general": "Anything else",
        "_control": CONTROL_DESCRIPTION,
    }
    assert effort["type"] == "choice"
    assert effort["instructions"]
    assert list(effort["criteria"]) == ["low", "medium", "high"]
    assert all(effort["criteria"].values())
    assert followup == {"type": "noul", "instructions": "The request continues the previous task's topic."}


async def test_request_without_history_sends_no_earlier_messages(
    backend: JevBackend, fake: FakeSystemOneApi
) -> None:
    await backend.classify(_request("what's the weather"), deadline_ms=1000)

    assert fake.bodies[0]["state"] == {"request": "what's the weather", "earlier_messages": []}


async def test_profile_without_description_is_sent_with_a_null_description(fake: FakeSystemOneApi) -> None:
    async with JevBackend(API_KEY, base_url=fake.base_url) as backend:
        await backend.configure((ProfileSpec("general", "", ()), PROFILES[2]))
        fake.body = _reply({**GOOD_ANSWERS, "profile": _choice_answer({"general": 1.0})})
        await backend.classify(_request(), deadline_ms=1000)

    assert fake.bodies[0]["questions"]["profile"]["criteria"] == {
        "general": None,
        "_control": CONTROL_DESCRIPTION,
    }


async def test_non_default_base_url_with_a_path_prefix_is_used() -> None:
    async with FakeSystemOneApi(prefix="/compat/jev") as api:
        async with JevBackend(API_KEY, base_url=f"{api.base_url}/") as backend:
            await backend.configure(PROFILES)
            await backend.classify(_request(), deadline_ms=1000)

        assert api.requests[0].path == "/compat/jev/v1/systemone"


async def test_from_config_uses_the_key_from_the_named_env_var_and_configured_endpoint() -> None:
    async with FakeSystemOneApi(prefix="/clef") as api:
        config = JevConfig(api_key_env="CLEF_API_KEY", model="clef-flash", base_url=api.base_url)
        async with JevBackend.from_config(config, Secrets({"CLEF_API_KEY": "clef-from-env"})) as backend:
            await backend.configure(PROFILES)
            await backend.classify(_request(), deadline_ms=1000)

        assert api.requests[0].path == "/clef/v1/systemone"
        assert api.requests[0].headers["Authorization"] == "Bearer clef-from-env"
        assert api.bodies[0]["model"] == "clef-flash"


# --- response mapping ---


async def test_answers_map_to_backend_result(backend: JevBackend) -> None:
    result = await backend.classify(_request(), deadline_ms=1000)

    assert result.backend == "jev"
    assert result.profile_probs == pytest.approx({"stock-picker": 0.7, "general": 0.2, "_control": 0.1})
    assert result.effort_probs == pytest.approx({"low": 0.1, "medium": 0.3, "high": 0.6})
    assert result.followup_prob == 0.25
    assert result.control_intent is None
    assert result.latency_ms >= 0


async def test_missing_choices_get_zero_and_probabilities_are_normalized(
    backend: JevBackend, fake: FakeSystemOneApi
) -> None:
    fake.body = _reply(
        {
            "followup": {"type": "noul", "noul": 0.9},
            "effort": _choice_answer({"low": 0.5, "medium": 0.5}),
            "profile": _choice_answer({"_control": 0.6, "general": 0.2}),
        }
    )

    result = await backend.classify(_request("any updates"), deadline_ms=1000)

    assert result.profile_probs == pytest.approx({"stock-picker": 0.0, "general": 0.25, "_control": 0.75})
    assert result.effort_probs == pytest.approx({"low": 0.5, "medium": 0.5, "high": 0.0})
    assert result.followup_prob == 0.9


# --- malformed replies and failures ---


def _without(name: str) -> dict[str, Any]:
    return _reply({key: answer for key, answer in GOOD_ANSWERS.items() if key != name})


def _with(name: str, answer: Any) -> dict[str, Any]:
    return _reply({**GOOD_ANSWERS, name: answer})


def _with_profile_probabilities(probabilities: Any) -> dict[str, Any]:
    return _with("profile", {**GOOD_ANSWERS["profile"], "probabilities": probabilities})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"model": DEFAULT_MODEL}, id="no-answers"),
        pytest.param({"answers": [GOOD_ANSWERS["profile"]]}, id="answers-not-an-object"),
        pytest.param(_with("profile", "stock-picker"), id="answer-not-an-object"),
        pytest.param(_without("profile"), id="missing-profile"),
        pytest.param(_without("effort"), id="missing-effort"),
        pytest.param(_without("followup"), id="missing-followup"),
        pytest.param(_with_profile_probabilities(None), id="no-probabilities"),
        pytest.param(_with_profile_probabilities([{"general": 0.9}]), id="probabilities-not-an-object"),
        pytest.param(_with_profile_probabilities({"home": 0.9}), id="unknown-choice"),
        pytest.param(_with_profile_probabilities({"general": 1.5}), id="probability-above-1"),
        pytest.param(_with_profile_probabilities({"general": "high"}), id="probability-not-a-number"),
        pytest.param(_with_profile_probabilities({"general": True}), id="probability-a-boolean"),
        pytest.param(_with_profile_probabilities({"general": 0.0}), id="all-zero"),
        pytest.param(_with("followup", {"type": "choice", "probabilities": {}}), id="followup-wrong-type"),
        pytest.param(_with("followup", {"type": "noul"}), id="followup-no-noul"),
        pytest.param(_with("followup", {"type": "noul", "noul": -0.1}), id="followup-below-0"),
    ],
)
async def test_malformed_reply_raises(backend: JevBackend, fake: FakeSystemOneApi, body: Any) -> None:
    fake.body = body

    with pytest.raises(JevError):
        await backend.classify(_request(), deadline_ms=1000)


async def test_invalid_json_raises(backend: JevBackend, fake: FakeSystemOneApi) -> None:
    fake.raw = "{not json"

    with pytest.raises(JevError, match="invalid JSON"):
        await backend.classify(_request(), deadline_ms=1000)


@pytest.mark.parametrize("status", [500, 503, 529, 429, 422, 401])
async def test_http_error_raises_with_status_and_without_the_key(
    backend: JevBackend, fake: FakeSystemOneApi, status: int
) -> None:
    fake.status = status
    fake.body = {"detail": "upstream failed"}

    with pytest.raises(JevError, match=f"HTTP {status}") as caught:
        await backend.classify(_request(), deadline_ms=1000)
    assert API_KEY not in str(caught.value)


async def test_slow_reply_beyond_the_deadline_raises_at_the_deadline(
    backend: JevBackend, fake: FakeSystemOneApi
) -> None:
    fake.delay_s = 1.0

    started = time.perf_counter()
    with pytest.raises(JevError, match="within 100 ms"):
        await backend.classify(_request(), deadline_ms=100)
    assert time.perf_counter() - started < 0.5


async def test_unreachable_api_raises() -> None:
    async with FakeSystemOneApi() as api:
        base_url = api.base_url
    async with JevBackend(API_KEY, base_url=base_url) as backend:
        await backend.configure(PROFILES)
        with pytest.raises(JevError, match="request failed"):
            await backend.classify(_request(), deadline_ms=1000)


# --- configuration ---


@pytest.mark.parametrize(
    "profiles",
    [
        pytest.param((), id="no-choices"),
        pytest.param((PROFILES[0], PROFILES[0]), id="duplicate-names"),
        pytest.param(tuple(ProfileSpec(f"p{n}", "", ()) for n in range(256)), id="too-many"),
    ],
)
async def test_configure_rejects_choice_counts_the_api_does_not_accept(
    profiles: tuple[ProfileSpec, ...],
) -> None:
    with pytest.raises(ValueError, match="unique choices"):
        await JevBackend(API_KEY).configure(profiles)


def test_backend_implements_the_router_backend_interface() -> None:
    backend: RouterBackend = JevBackend(API_KEY)

    assert backend.name == "jev"


async def test_classify_before_configure_raises() -> None:
    with pytest.raises(RuntimeError, match="before configure"):
        await JevBackend(API_KEY).classify(_request(), deadline_ms=1000)
