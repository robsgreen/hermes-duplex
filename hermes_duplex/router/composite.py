"""The routing policy: runs both paths, picks the effort, suggests a better profile, and looks up the model.

``CompositeRouter`` is what the rest of the service routes through. It owns the ``_control`` choice,
the main path's deadline and circuit breaker, the switch-suggestion rule, and the decision log. It
never picks the profile a task runs on: that is the speaker's active profile.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ..config import CONTROL_PROFILE, ProfileConfig, RouterConfig
from ..providers.base import ModelChoice
from .base import (
    BackendResult,
    Effort,
    RouteDecision,
    RouterBackend,
    RouteRequest,
    RouteSource,
    choice_specs,
)

logger = logging.getLogger(__name__)

DEFAULT_EFFORT: Effort = "medium"
CONFIDENT_PROB = 0.5
"""A backend's top probability below this is not trusted; the router falls back instead."""
CONTROL_MIN_PROB = 0.995
"""The deciding answer's ``_control`` probability must be at least this for the request to be a
``_control`` request; below it the request is a task. A request taken as status fails silently while
a status question taken as a task is only a wasted run, so the bar is high. Probabilities sum to 1, so
this also means ``_control`` beats every profile by at least 0.99. On the router eval ``knn`` gives
tasks up to 0.989 for ``_control`` when profiles have only a description; 0.995 keeps every task a
task there and keeps the same status and cancel requests as 0.999 would."""
CONTROL_MATCH_SIMILARITY = 0.9
"""A fast-path ``_control`` answer is accepted without waiting for the main path only when the request
is at least this similar (``BackendResult.control_similarity``) to one of the ``_control`` phrases: a
near-exact match, such as "cancel it" for "cancel that"."""
EARLY_ACCEPT_PROB = 0.85
"""A fast-path top probability at or above this (with ``EARLY_ACCEPT_MARGIN``) for the active profile
settles the request without waiting for the main path."""
EARLY_ACCEPT_MARGIN = 0.3
"""How much the fast path's top choice must beat its runner-up by to be accepted early."""
SUGGEST_MARGIN = 0.2
"""How much a suggested profile's probability must beat the active profile's by."""
BREAKER_FAILURES = 3
"""Consecutive main-path failures or deadline misses that open the circuit breaker."""
BREAKER_OPEN_S = 30.0
"""How long an open breaker skips the main path, in seconds."""


@dataclass(frozen=True)
class _Answers:
    """What each path answered before the router stopped waiting; None for no answer."""

    main: BackendResult | None
    fast: BackendResult | None


class _Breaker:
    """Skips the main path for ``BREAKER_OPEN_S`` after ``BREAKER_FAILURES`` consecutive failures.

    Once the open period ends the main path is asked again, with the failure count reset.
    """

    def __init__(self, backend: str) -> None:
        self._backend = backend
        self._failures = 0
        self._open_until_s: float | None = None

    def allows(self, now_s: float) -> bool:
        """Whether to ask the main path now; closes the breaker (and logs it) once its time is up."""
        if self._open_until_s is None:
            return True
        if now_s < self._open_until_s:
            return False
        self._open_until_s = None
        self._failures = 0
        logger.info("router circuit breaker closed: asking %s again", self._backend)
        return True

    def record(self, *, answered: bool, now_s: float) -> None:
        """Count one main-path outcome: an answer resets the count; a failure or miss adds to it."""
        if answered:
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= BREAKER_FAILURES and self._open_until_s is None:
            self._open_until_s = now_s + BREAKER_OPEN_S
            logger.warning(
                "router circuit breaker open: skipping %s for %d s after %d consecutive failures",
                self._backend,
                BREAKER_OPEN_S,
                self._failures,
            )


class CompositeRouter:
    """Decides a request's effort and model, whether it is a ``_control`` request, and whether another
    profile would suit it better.

    Every configured profile is a choice for the backends, and ``_control`` is too. The request runs
    on the speaker's active profile, which the caller gives and the router never changes; the model
    comes from that profile's effort table. The router suggests another profile
    (``RouteDecision.suggestion``) only when ``config.suggest_switch`` is on, the speaker may use it,
    its probability is at least ``config.suggest_threshold``, and it beats the active profile's by at
    least ``SUGGEST_MARGIN``.

    A request is a task unless the deciding answer gives ``_control`` at least ``CONTROL_MIN_PROB``;
    a weaker ``_control`` answer, even the top one, leaves it a task on the active profile.

    Each request goes to the main path (``config.backend``) and, when configured, the fast path
    (``config.fast_path``) at once. The router waits up to ``config.deadline_ms`` for the main path.
    It stops early when the fast path is sure (``EARLY_ACCEPT_PROB``, ``EARLY_ACCEPT_MARGIN``) of the
    active profile, so a suggestion always waits for the main path, or when its ``_control`` answer
    passes ``CONTROL_MIN_PROB`` for a near-exact match of a ``_control`` phrase
    (``CONTROL_MATCH_SIMILARITY``). The first confident answer of main, then fast path decides.
    Without one (``source`` ``fallback``), the request is a task, nothing is suggested, and the effort
    is the main path's judgement when it answered, else the fast path's, else ``DEFAULT_EFFORT``.
    """

    def __init__(
        self,
        config: RouterConfig,
        profiles: Mapping[str, ProfileConfig],
        backends: Sequence[RouterBackend],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``backends`` must include the backends ``config`` names, matched by ``name``: the main
        path ``config.backend`` and, when set, the fast path ``config.fast_path``; others are unused.
        ``clock`` returns seconds and times the circuit breaker and the decision latency; the deadline
        itself is real time.

        Raises ``ValueError`` when a backend ``config`` names is not in ``backends``.
        """
        by_name = {backend.name: backend for backend in backends}
        wanted = [config.backend] if config.fast_path is None else [config.backend, config.fast_path]
        if missing := [name for name in wanted if name not in by_name]:
            raise ValueError(f"router backend '{missing[0]}' is configured but was not given")
        self._config = config
        self._profiles = profiles
        self._main = by_name[config.backend]
        # A fast path that is also the main path is just the main path.
        self._fast = by_name[config.fast_path] if config.fast_path not in (None, config.backend) else None
        self._breaker = _Breaker(self._main.name)
        self._clock = clock
        self._choices = (*profiles, CONTROL_PROFILE)
        self._abandoned: set[asyncio.Task[BackendResult]] = set()
        """Backend calls cancelled mid-flight, referenced until they finish unwinding."""

    async def configure(self) -> None:
        """Give each path's backend the choices: each profile, then ``_control`` with its examples."""
        specs = choice_specs(self._profiles, self._config.control_examples)
        for backend in (self._main, self._fast):
            if backend is not None:
                await backend.configure(specs)

    async def close(self) -> None:
        """Close each path's backend, releasing what it holds open; routing again reopens it."""
        for backend in (self._main, self._fast):
            if backend is not None:
                await backend.close()

    async def route(
        self,
        text: str,
        speaker: str,
        *,
        active_profile: str,
        permitted: Callable[[str], bool] | None = None,
        history: Sequence[str] = (),
    ) -> RouteDecision:
        """Decide the effort, model, and any switch suggestion for ``speaker``'s request ``text``, and
        log the decision.

        ``active_profile`` is the configured profile the request runs on; the model comes from its
        effort table. ``permitted(profile)`` says whether the speaker may use a profile (None: every
        profile); only a permitted profile is ever suggested. ``history`` is the conversation's recent
        lines, oldest first. Never raises for a backend failure: with no confident answer, the
        decision is the fallback the class describes.
        """
        started = self._clock()
        req = RouteRequest(text=text, speaker=speaker, history=tuple(history), active_profile=active_profile)
        answers = await self._ask(req, ask_main=self._breaker.allows(started))
        decision = self._decide(answers, active_profile, permitted, started)
        _log_decision(speaker, active_profile, decision)
        return decision

    async def _ask(self, req: RouteRequest, *, ask_main: bool) -> _Answers:
        """Ask the main path (when ``ask_main``) and the fast path at once, collecting answers until the
        main path answers confidently, the fast path is accepted early, both are done, or
        ``deadline_ms`` passes. Calls still running then are cancelled. Main-path outcomes feed the
        circuit breaker, except a call cancelled by an early accept.
        """
        deadline_ms = self._config.deadline_ms
        calls: dict[asyncio.Task[BackendResult], RouterBackend] = {}
        main_call: asyncio.Task[BackendResult] | None = None
        if ask_main:
            main_call = asyncio.create_task(self._main.classify(req, deadline_ms))
            calls[main_call] = self._main
        if self._fast is not None:
            calls[asyncio.create_task(self._fast.classify(req, deadline_ms))] = self._fast

        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + deadline_ms / 1000
        main: BackendResult | None = None
        fast: BackendResult | None = None
        settled = False
        pending = set(calls)
        try:
            while pending and not settled:
                done, pending = await asyncio.wait(
                    pending, timeout=max(0.0, give_up_at - loop.time()), return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                for call in done:
                    result = _result(call, calls[call])
                    if call is main_call:
                        main = result
                        self._breaker.record(answered=result is not None, now_s=self._clock())
                    else:
                        fast = result
                settled = (main is not None and self._is_confident(main)) or (
                    fast is not None and self._accepts_early(fast, req.active_profile)
                )
        finally:
            for call in pending:
                call.cancel()
                self._abandoned.add(call)
                call.add_done_callback(self._forget_abandoned)

        if not settled:
            for call in pending:
                logger.warning("router backend %s missed the %d ms deadline", calls[call].name, deadline_ms)
                if call is main_call:
                    self._breaker.record(answered=False, now_s=self._clock())
        return _Answers(main=main, fast=fast)

    def _forget_abandoned(self, call: asyncio.Task[BackendResult]) -> None:
        """Drop an abandoned call once it ends, and retrieve its outcome: a backend whose own timeout
        fires at the deadline ends with an error rather than being cancelled, and an error nobody
        retrieves is logged by asyncio as "Task exception was never retrieved"."""
        self._abandoned.discard(call)
        if not call.cancelled() and (exc := call.exception()) is not None:
            logger.debug("abandoned router call ended with %s: %s", type(exc).__name__, exc)

    def _accepts_early(self, fast: BackendResult, active: str) -> bool:
        """Whether the fast path is sure enough to stop waiting for the main path: a clear winner that
        is the speaker's active profile, so there is no switch to suggest, or a ``_control`` answer
        strong enough to decide that matches a ``_control`` phrase near-exactly."""
        probs = fast.profile_probs
        if self._winner(probs) == CONTROL_PROFILE:
            similarity = fast.control_similarity
            near_exact = similarity is not None and similarity >= CONTROL_MATCH_SIMILARITY
            return near_exact and _is_control(probs)
        top, runner_up = sorted((probs.get(choice, 0.0) for choice in self._choices), reverse=True)[:2]
        sure = top >= EARLY_ACCEPT_PROB and top - runner_up >= EARLY_ACCEPT_MARGIN
        return sure and self._winner(probs) == active

    def _decide(
        self,
        answers: _Answers,
        active: str,
        permitted: Callable[[str], bool] | None,
        started: float,
    ) -> RouteDecision:
        # The main path's effort judgement counts whenever it answered, even when it was not confident
        # about the choice.
        judge = answers.main or answers.fast
        decider = next(
            (
                result
                for result in (answers.main, answers.fast)
                if result is not None and self._is_confident(result)
            ),
            None,
        )
        effort = _top_effort(judge)
        probs: Mapping[str, float] = judge.profile_probs if judge is not None else {}
        source: RouteSource = "fallback"
        control_intent = None
        suggestion = None
        if decider is not None:
            probs, source = decider.profile_probs, _backend_source(decider)
            if _is_control(probs):
                # A backend that cannot tell the intents apart gets ``status``, which changes nothing.
                control_intent = decider.control_intent or "status"
            else:
                suggestion = self._suggestion(probs, active, permitted)
        return RouteDecision(
            effort=effort,
            model=None if control_intent is not None else self.model_for(active, effort),
            suggestion=suggestion,
            control_intent=control_intent,
            probabilities=probs,
            source=source,
            latency_ms=round((self._clock() - started) * 1000),
        )

    def model_for(self, profile: str, effort: Effort) -> ModelChoice | None:
        """The model for a run at ``effort`` on the configured ``profile``, from its effort table
        (``ProfileConfig.model_for``); None means the Hermes profile's own default."""
        return self._profiles[profile].model_for(effort)

    def _suggestion(
        self, probs: Mapping[str, float], active: str, permitted: Callable[[str], bool] | None
    ) -> str | None:
        """The profile to suggest switching to, by the rule in the class docstring, or None."""
        if not self._config.suggest_switch:
            return None
        others = [
            profile
            for profile in self._profiles
            if profile != active and (permitted is None or permitted(profile))
        ]
        if not others:
            return None
        best = max(others, key=lambda profile: probs.get(profile, 0.0))
        best_prob = probs.get(best, 0.0)
        beats_active = best_prob - probs.get(active, 0.0) >= SUGGEST_MARGIN
        return best if best_prob >= self._config.suggest_threshold and beats_active else None

    def _winner(self, probs: Mapping[str, float]) -> str:
        return max(self._choices, key=lambda choice: probs.get(choice, 0.0))

    def _is_confident(self, result: BackendResult) -> bool:
        return result.profile_probs.get(self._winner(result.profile_probs), 0.0) >= CONFIDENT_PROB


def _result(call: asyncio.Task[BackendResult], backend: RouterBackend) -> BackendResult | None:
    """The finished ``call``'s answer, or None (logged) when ``backend`` raised."""
    try:
        return call.result()
    except Exception:
        logger.exception("router backend %s failed", backend.name)
        return None


def _is_control(probs: Mapping[str, float]) -> bool:
    """Whether ``probs`` make the request a ``_control`` request (``CONTROL_MIN_PROB``)."""
    return probs.get(CONTROL_PROFILE, 0.0) >= CONTROL_MIN_PROB


def _backend_source(result: BackendResult) -> RouteSource:
    # Backend names (knn, decisions, jev) are the backend members of RouteSource.
    return cast(RouteSource, result.backend)


def _top_effort(result: BackendResult | None) -> Effort:
    if result is None or not result.effort_probs:
        return DEFAULT_EFFORT
    probs = result.effort_probs
    return max(probs, key=lambda effort: probs[effort])


def _log_decision(speaker: str, active_profile: str, decision: RouteDecision) -> None:
    """Log one line per decision; the fields are also attached to the record as ``route_decision``.

    ``profile`` is the speaker's active profile, which the request runs on unless ``control`` is set.
    """
    top3 = sorted(decision.probabilities.items(), key=lambda item: item[1], reverse=True)[:3]
    model = decision.model
    fields = {
        "speaker": speaker,
        "profile": active_profile,
        "control": decision.control_intent,
        "effort": decision.effort,
        "model": f"{model.provider}/{model.model}" if model is not None else None,
        "suggestion": decision.suggestion,
        "top3": {choice: round(prob, 3) for choice, prob in top3},
        "source": decision.source,
        "latency_ms": decision.latency_ms,
    }
    line = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("route %s", line, extra={"route_decision": fields})
