"""The router's data types, the interface every router backend implements, and the ``_control`` choice.

A backend scores one request against every choice it was configured with; the composite router
(``composite.py``) turns those scores into a ``RouteDecision``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from ..config import CONTROL_PROFILE, ControlExamples, ProfileConfig
from ..providers.base import ModelChoice

Effort = Literal["low", "medium", "high"]
"""How hard a task's model should think; Hermes receives it as ``model_options.reasoning.effort``."""

ControlIntent = Literal["status", "cancel"]
"""What a ``_control`` request asks for: a summary of running tasks, or to stop one."""

RouteSource = Literal["decisions", "jev", "knn", "hint", "sticky", "fallback"]
"""What settled a decision: a backend by name, the profile hint, the sticky rule, or the fallback."""

CONTROL_DESCRIPTION = "status or cancel of running voice tasks"
"""The fixed description of the ``_control`` choice."""

BUILTIN_CONTROL_EXAMPLES: Mapping[ControlIntent, tuple[str, ...]] = {
    "status": ("what are you working on", "any updates", "how's that task going"),
    "cancel": ("cancel that", "stop that task", "never mind"),
}


@dataclass(frozen=True)
class ProfileSpec:
    """One choice a backend can route to: a Hermes profile, or ``_control``."""

    name: str
    description: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class ControlSpec(ProfileSpec):
    """The ``_control`` choice. ``examples`` holds every phrase; ``intents`` says which intent each means.

    Backends that only need the choice treat it as any ``ProfileSpec``; a backend that can tell the
    intents apart reads ``intents``.
    """

    intents: Mapping[str, ControlIntent]
    """Example phrase → the intent it expresses; holds exactly the phrases in ``examples``."""


def control_spec(extra: ControlExamples) -> ControlSpec:
    """Return the ``_control`` choice: the built-in examples followed by ``extra`` from config.

    A phrase listed under both intents keeps the intent it was first listed with.
    """
    by_intent: tuple[tuple[ControlIntent, tuple[str, ...]], ...] = (
        ("status", BUILTIN_CONTROL_EXAMPLES["status"] + extra.status),
        ("cancel", BUILTIN_CONTROL_EXAMPLES["cancel"] + extra.cancel),
    )
    intents: dict[str, ControlIntent] = {}
    for intent, phrases in by_intent:
        for phrase in phrases:
            intents.setdefault(phrase, intent)
    return ControlSpec(
        name=CONTROL_PROFILE,
        description=CONTROL_DESCRIPTION,
        examples=tuple(intents),
        intents=MappingProxyType(intents),
    )


def choice_specs(
    profiles: Mapping[str, ProfileConfig], control_examples: ControlExamples
) -> list[ProfileSpec]:
    """Every choice a backend is configured with: each profile in order, then ``_control``."""
    specs: list[ProfileSpec] = [
        ProfileSpec(name=name, description=profile.description, examples=profile.examples)
        for name, profile in profiles.items()
    ]
    specs.append(control_spec(control_examples))
    return specs


@dataclass(frozen=True)
class RouteRequest:
    """One task to route.

    ``history`` holds the conversation's recent lines, oldest first. ``active_profile`` is the
    speaker's current sticky profile, or None. ``hint`` is a profile the voice model suggested, or None.
    """

    text: str
    speaker: str
    history: tuple[str, ...]
    active_profile: str | None
    hint: str | None


@dataclass(frozen=True)
class RouteDecision:
    """Where a task goes, and why.

    ``profile`` is a configured profile or ``_control``; a ``_control`` decision is answered locally
    and never dispatched to Hermes. ``model`` None means the Hermes profile's own default.
    ``confidence`` is the probability the deciding backend gave ``profile`` (0.0 when no backend
    answered). ``probabilities`` maps every choice to the deciding backend's probability (empty when
    none answered). ``sticky`` is true when the task stays on the speaker's active profile.
    """

    profile: str
    effort: Effort
    model: ModelChoice | None
    confidence: float
    probabilities: Mapping[str, float]
    followup_prob: float | None
    sticky: bool
    source: RouteSource
    latency_ms: int
    control_intent: ControlIntent | None = None
    """Set exactly when ``profile`` is ``_control``."""


@dataclass(frozen=True)
class BackendResult:
    """One backend's scores for one request.

    ``profile_probs`` maps every configured choice to a probability; the values sum to 1.
    ``effort_probs`` and ``followup_prob`` are None when the backend does not judge them.
    ``latency_ms`` is how long the backend took; ``backend`` is its ``name``.
    """

    profile_probs: Mapping[str, float]
    effort_probs: Mapping[Effort, float] | None
    followup_prob: float | None
    latency_ms: int
    backend: str
    control_intent: ControlIntent | None = None
    """The intent the request expresses if it is a ``_control`` request; None when the backend
    cannot tell (the router then assumes ``status``, which changes nothing)."""


class RouterBackend(Protocol):
    """Scores requests against a fixed set of choices."""

    name: str
    """Identifies the backend in decisions and logs, e.g. ``knn``."""

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        """Prepare to score requests against ``profiles``; called once before ``classify``."""
        ...

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        """Score ``req``: probabilities for profile, effort, and follow-up.

        ``deadline_ms`` is how long the caller will wait; a backend may give up after it. Raises on
        failure; the router falls back.
        """
        ...

    async def close(self) -> None:
        """Release what the backend holds open, such as an HTTP session; it may be used again after."""
        ...
