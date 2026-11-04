"""The router as config builds it: the OpenAI Decisions backend over HTTP as the main path and knn as
the fast path, with Decisions too slow, failing, or skipped by the circuit breaker.

Config loading, ``build_backends``, ``CompositeRouter``, ``DecisionsBackend`` (real HTTP), and
``KnnBackend`` are the real ones; the Decisions API is an aiohttp fake and the embedding model a
deterministic one.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self

import pytest
import yaml
from aiohttp import web

from hermes_duplex.config import load_config
from hermes_duplex.router.composite import BREAKER_OPEN_S, CompositeRouter
from hermes_duplex.router.factory import build_backends
from tests.fakes.fake_embedder import fake_embed

PHRASES = Path(__file__).resolve().parents[1] / "fixtures" / "router_eval" / "phrases.yaml"
DEADLINE_MS = 300
ROUTE_BUDGET_MS = 350

# Each switch of profile disagrees with the speaker's active one, so knn, though sure, is not
# accepted early and the router waits for Decisions until the deadline.
WEATHER = "what's the weather like tomorrow"
PORTFOLIO = "how is my portfolio doing"
GIBBERISH = "zzz qqq"  # shares no word with any example: knn cannot pick a profile


class FakeDecisions:
    """``POST /v1/decisions`` that answers stock-picker after ``delay_s``, or ``status`` when not 200."""

    def __init__(self, *, delay_s: float = 0.0, status: int = 200) -> None:
        self.delay_s = delay_s
        self.status = status
        self.calls = 0
        self.base_url = ""
        self._runner: web.AppRunner | None = None

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

    async def _decide(self, request: web.Request) -> web.Response:
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        if self.status != 200:
            return web.json_response({"error": {"message": "overloaded"}}, status=self.status)
        return web.json_response({"answers": _answers(), "model": "decisions-test"})


def _answers() -> list[dict[str, Any]]:
    def choice(name: str, probs: dict[str, float]) -> dict[str, Any]:
        top = max(probs, key=lambda value: probs[value])
        probabilities = [{"value": value, "probability": p} for value, p in probs.items()]
        return {
            "type": "choice",
            "name": name,
            "choice": top,
            "confidence": probs[top],
            "probabilities": probabilities,
        }

    return [
        choice("profile", {"stock-picker": 0.9, "general": 0.05, "_control": 0.05}),
        choice("effort", {"low": 0.1, "medium": 0.2, "high": 0.7}),
        {"type": "predicate", "name": "followup", "probability": 0.1},
    ]


class _SkewedClock:
    """Real monotonic time that a test can move forward, for the circuit breaker's 30 s."""

    def __init__(self) -> None:
        self.skew_s = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.skew_s


def _router(tmp_path: Path, decisions: FakeDecisions, clock: _SkewedClock | None = None) -> CompositeRouter:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {
            "backend": "decisions",
            "fast_path": "knn",
            "deadline_ms": DEADLINE_MS,
            "decisions": {"api_key_env": "DECISIONS_KEY", "base_url": decisions.base_url},
        },
        "profiles": yaml.safe_load(PHRASES.read_text(encoding="utf-8"))["profiles"],
        "people": {"rob": {"profiles": ["*"]}, "alex": {"profiles": ["general"]}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path, env={"DECISIONS_KEY": "sk-test-0123456789"})
    backends = build_backends(config, embed=fake_embed)
    if clock is None:
        return CompositeRouter(config.router, config.profiles, backends)
    return CompositeRouter(config.router, config.profiles, backends, clock=clock)


@pytest.fixture
async def slow_decisions() -> AsyncIterator[FakeDecisions]:
    async with FakeDecisions(delay_s=1.0) as fake:
        yield fake


async def test_slow_decisions_falls_to_knn_in_time_and_trips_the_breaker_for_30_s(
    tmp_path: Path, slow_decisions: FakeDecisions, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _SkewedClock()
    router = _router(tmp_path, slow_decisions, clock)
    await router.configure()
    try:
        await router.route(WEATHER, "rob", active_profile="general")  # knn agrees: accepted early
        caplog.clear()

        decisions = []
        for text, active in ((PORTFOLIO, "general"), (WEATHER, "stock-picker"), (PORTFOLIO, "general")):
            started = time.monotonic()
            decisions.append(await router.route(text, "rob", active_profile=active))
            assert (time.monotonic() - started) * 1000 < ROUTE_BUDGET_MS
        assert [d.source for d in decisions] == ["knn", "knn", "knn"]
        assert all(d.latency_ms < ROUTE_BUDGET_MS for d in decisions)
        missed = [r for r in caplog.records if "decisions missed the 300 ms deadline" in r.getMessage()]
        assert len(missed) == 3
        asked = slow_decisions.calls

        # Three misses in a row: Decisions is not asked again until 30 s have passed.
        skipped = await router.route(WEATHER, "rob", active_profile="stock-picker")
        assert skipped.source == "knn"
        assert slow_decisions.calls == asked

        clock.skew_s = BREAKER_OPEN_S + 1
        await router.route(PORTFOLIO, "rob", active_profile="general")
        assert slow_decisions.calls == asked + 1
    finally:
        await router.close()


async def test_every_backend_failing_gives_the_default_effort_on_the_active_profile(tmp_path: Path) -> None:
    async with FakeDecisions(status=500) as failing:
        router = _router(tmp_path, failing)
        await router.configure()
        try:
            decisions = [
                await router.route(GIBBERISH, speaker, active_profile=active)
                for speaker, active in (("alex", "general"), ("rob", "stock-picker"), ("rob", "general"))
            ]
            await router.route(GIBBERISH, "rob", active_profile="general")
        finally:
            await router.close()

    for decision in decisions:
        assert (decision.source, decision.effort, decision.model) == ("fallback", "medium", None)
        assert (decision.suggestion, decision.control_intent) == (None, None)
    # The third failure opened the breaker, so the last request did not ask Decisions.
    assert failing.calls == 3
