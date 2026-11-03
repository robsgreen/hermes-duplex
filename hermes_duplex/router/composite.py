"""The routing policy: runs both paths, applies the sticky rule and fallbacks, and looks up the model.

``CompositeRouter`` is what the rest of the service routes through. It owns each speaker's active
(sticky) profile, the ``_control`` choice, the main path's deadline and circuit breaker, the
profile-by-effort model table, and the decision log.
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
EARLY_ACCEPT_PROB = 0.85
"""A fast-path top probability at or above this (with ``EARLY_ACCEPT_MARGIN``) settles the request
without waiting for the main path."""
EARLY_ACCEPT_MARGIN = 0.3
"""How much the fast path's top choice must beat its runner-up by to be accepted early."""
FOLLOWUP_PROB = 0.5
"""Below this follow-up probability, a request may leave the speaker's active profile."""
LEAVE_MARGIN = 0.2
"""How much the winner must beat the active profile by to leave it, when the backend judged follow-up."""
LEAVE_MARGIN_WITHOUT_FOLLOWUP = 0.3
"""The same margin when the backend did not judge follow-up (``followup_prob`` is None)."""
BREAKER_FAILURES = 3
"""Consecutive main-path failures or deadline misses that open the circuit breaker."""
BREAKER_OPEN_S = 30.0
"""How long an open breaker skips the main path, in seconds."""


@dataclass
class _Active:
    profile: str
    last_routed_s: float
    """Clock reading of the speaker's last decision for this profile."""


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
    """Routes a speaker's request to a profile (or ``_control``), an effort, and a model.

    Every configured profile is a choice, whatever the speaker may use: permission is checked after
    routing. ``_control`` is always a choice too. A speaker's active profile is the last profile they
    were routed to and may use; it expires after ``sticky_idle_s`` of no routing, and ``_control``
    decisions, like decisions for a profile the speaker may not use, neither change nor refresh it.

    Each request goes to the main path (``config.backend``) and, when configured, the fast path
    (``config.fast_path``) at once. The router waits up to ``config.deadline_ms`` for the main path,
    and stops early when the fast path is sure (``EARLY_ACCEPT_PROB``, ``EARLY_ACCEPT_MARGIN``) and
    agrees with the active profile. The first confident answer of main, then fast path decides;
    without one the request goes to the hint, else the active profile, else ``default_profile``.
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
        ``clock`` returns seconds and times the sticky expiry, the circuit breaker, and the decision
        latency; the deadline itself is real time.

        Raises ``ValueError`` when ``config.default_profile`` is not in ``profiles`` or a backend
        ``config`` names is not in ``backends``.
        """
        if config.default_profile not in profiles:
            raise ValueError(f"default profile '{config.default_profile}' is not a configured profile")
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
        self._active: dict[str, _Active] = {}
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
        history: Sequence[str] = (),
        hint: str | None = None,
        permitted: Callable[[str], bool] | None = None,
    ) -> RouteDecision:
        """Decide where ``speaker``'s request ``text`` goes, update their active profile, and log it.

        ``history`` is the conversation's recent lines, oldest first; ``hint`` is a profile the voice
        model suggested, used only when neither path answers confidently (a name that is not a
        configured profile is ignored). ``permitted(profile)`` says whether the speaker may use a
        profile (None: every profile); the decision may still name one they may not, which the caller
        refuses, but only a permitted profile becomes their active profile. Never raises for a backend
        failure: with no answer, the request goes to the hint, else the active profile, else the
        default profile.
        """
        started = self._clock()
        active = self._active_profile(speaker, started)
        req = RouteRequest(
            text=text, speaker=speaker, history=tuple(history), active_profile=active, hint=hint
        )
        answers = await self._ask(req, ask_main=self._breaker.allows(started))
        decision = self._decide(answers, hint, active, started)
        if decision.profile != CONTROL_PROFILE and (permitted is None or permitted(decision.profile)):
            self._active[speaker] = _Active(decision.profile, started)
        _log_decision(speaker, decision)
        return decision

    def _active_profile(self, speaker: str, now: float) -> str | None:
        active = self._active.get(speaker)
        if active is None:
            return None
        if now - active.last_routed_s >= self._config.sticky_idle_s:
            del self._active[speaker]
            return None
        return active.profile

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
                call.add_done_callback(self._abandoned.discard)

        if not settled:
            for call in pending:
                logger.warning("router backend %s missed the %d ms deadline", calls[call].name, deadline_ms)
                if call is main_call:
                    self._breaker.record(answered=False, now_s=self._clock())
        return _Answers(main=main, fast=fast)

    def _accepts_early(self, fast: BackendResult, active: str | None) -> bool:
        """Whether the fast path is sure enough to stop waiting for the main path: a clear winner that
        keeps the speaker on their active profile (or is ``_control``, which leaves it alone)."""
        probs = fast.profile_probs
        top, runner_up = sorted((probs.get(choice, 0.0) for choice in self._choices), reverse=True)[:2]
        sure = top >= EARLY_ACCEPT_PROB and top - runner_up >= EARLY_ACCEPT_MARGIN
        return sure and (active is None or self._winner(probs) in (active, CONTROL_PROFILE))

    def _decide(
        self, answers: _Answers, hint: str | None, active: str | None, started: float
    ) -> RouteDecision:
        # The main path's effort and follow-up judgements count whenever it answered, even when it was
        # not confident about the profile.
        judge = answers.main or answers.fast
        decider = next(
            (
                result
                for result in (answers.main, answers.fast)
                if result is not None and self._is_confident(result)
            ),
            None,
        )
        followup_prob = judge.followup_prob if judge is not None else None

        source: RouteSource
        if decider is not None:
            probs = decider.profile_probs
            winner = self._winner(probs)
            # Status and cancel concern the speaker's tasks, not a new topic, so ``_control`` bypasses
            # the sticky rule and leaves the active profile alone.
            if (
                winner == CONTROL_PROFILE
                or active is None
                or winner == active
                or self._leaves(probs, winner, active, followup_prob)
            ):
                profile, source = winner, _backend_source(decider)
            else:
                profile, source = active, "sticky"
        else:
            probs = judge.profile_probs if judge is not None else {}
            if hint is not None and hint in self._profiles:
                profile, source = hint, "hint"
            else:
                profile, source = active or self._config.default_profile, "fallback"

        effort = _top_effort(judge)
        is_control = profile == CONTROL_PROFILE
        control_intent = None
        if is_control:
            # A backend that cannot tell the intents apart gets ``status``, which changes nothing.
            control_intent = (decider.control_intent if decider is not None else None) or "status"
        return RouteDecision(
            profile=profile,
            effort=effort,
            model=None if is_control else self._model(profile, effort),
            confidence=probs.get(profile, 0.0),
            probabilities=probs,
            followup_prob=followup_prob,
            sticky=profile == active,
            source=source,
            latency_ms=round((self._clock() - started) * 1000),
            control_intent=control_intent,
        )

    def _winner(self, probs: Mapping[str, float]) -> str:
        return max(self._choices, key=lambda choice: probs.get(choice, 0.0))

    def _is_confident(self, result: BackendResult) -> bool:
        return result.profile_probs.get(self._winner(result.profile_probs), 0.0) >= CONFIDENT_PROB

    @staticmethod
    def _leaves(probs: Mapping[str, float], winner: str, active: str, followup_prob: float | None) -> bool:
        """The sticky rule: whether a confident ``winner`` takes the request off the ``active`` profile."""
        margin = probs.get(winner, 0.0) - probs.get(active, 0.0)
        if followup_prob is None:
            return margin >= LEAVE_MARGIN_WITHOUT_FOLLOWUP
        return followup_prob < FOLLOWUP_PROB and margin >= LEAVE_MARGIN

    def _model(self, profile: str, effort: Effort) -> ModelChoice | None:
        config = self._profiles[profile]
        return config.models.get(effort) or config.default_model


def _result(call: asyncio.Task[BackendResult], backend: RouterBackend) -> BackendResult | None:
    """The finished ``call``'s answer, or None (logged) when ``backend`` raised."""
    try:
        return call.result()
    except Exception:
        logger.exception("router backend %s failed", backend.name)
        return None


def _backend_source(result: BackendResult) -> RouteSource:
    # Backend names (knn, decisions, jev) are the backend members of RouteSource.
    return cast(RouteSource, result.backend)


def _top_effort(result: BackendResult | None) -> Effort:
    if result is None or not result.effort_probs:
        return DEFAULT_EFFORT
    probs = result.effort_probs
    return max(probs, key=lambda effort: probs[effort])


def _log_decision(speaker: str, decision: RouteDecision) -> None:
    """Log one line per decision; the fields are also attached to the record as ``route_decision``."""
    top3 = sorted(decision.probabilities.items(), key=lambda item: item[1], reverse=True)[:3]
    model = decision.model
    fields = {
        "speaker": speaker,
        "profile": decision.profile,
        "effort": decision.effort,
        "model": f"{model.provider}/{model.model}" if model is not None else None,
        "confidence": round(decision.confidence, 3),
        "top3": {choice: round(prob, 3) for choice, prob in top3},
        "source": decision.source,
        "latency_ms": decision.latency_ms,
        "sticky": decision.sticky,
    }
    line = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("route %s", line, extra={"route_decision": fields})
