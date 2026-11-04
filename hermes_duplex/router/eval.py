"""Router eval: how well the router judges effort and suggests switches, with each backend alone and
with the composite router, over a file of labelled phrases.

A phrase file is YAML with a ``phrases`` list and an optional ``profiles`` section.
``tests/fixtures/router_eval/phrases.yaml`` is the reference file and documents the format:

- ``profiles`` maps each profile to its ``description`` and ``examples``, in the shape of
  duplex.yaml's ``profiles`` section; it is what the router is configured with.
- Each phrase has the ``text`` a speaker says, the ``profile`` it belongs to (``_control`` for status
  and cancel requests), the speaker's ``active`` profile when they say it, and the ``effort`` it
  deserves. The router should suggest switching exactly when ``profile`` is a profile other than
  ``active``. A follow-up names the phrase it ``follows``, said earlier by the same speaker. A
  ``control_intent`` key is allowed and not scored.

For each target the eval reports effort accuracy, suggestion precision and recall, how often the
deciding backend's top choice is the labelled one (with macro-F1 and the confusion matrix), latency
percentiles, and the deadline-miss rate (``EvalReport``).
"""

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args

import numpy as np
import yaml

from ..config import CONTROL_PROFILE, Config, ProfileConfig, RouterConfig
from .base import Effort, RouterBackend
from .composite import CompositeRouter
from .factory import build_backends
from .knn import Embedder, KnnBackend

logger = logging.getLogger(__name__)

COMPOSITE = "composite"
"""The target name of the composite router, beside the backend names."""

NO_ANSWER = "no_answer"
"""The confusion-matrix column of phrases no backend answered in time."""

_EFFORTS: tuple[Effort, ...] = get_args(Effort)
_PHRASE_KEYS = frozenset({"text", "profile", "active", "effort", "follows", "control_intent"})
_PROFILE_KEYS = frozenset({"description", "examples"})


class PhraseFileError(ValueError):
    """The phrase file is unreadable or malformed, or its labels do not match the router's profiles."""


@dataclass(frozen=True)
class LabelledPhrase:
    """One utterance and where it belongs. ``active`` is the profile the speaker is on when they say
    it; ``follows`` is the text of an earlier phrase that the same speaker said just before it, or
    None."""

    text: str
    profile: str
    effort: Effort
    active: str
    follows: str | None = None


@dataclass(frozen=True)
class PhraseFile:
    """A loaded phrase file. ``profiles`` is None when the file has no ``profiles`` section."""

    profiles: Mapping[str, ProfileConfig] | None
    phrases: tuple[LabelledPhrase, ...]


@dataclass(frozen=True)
class EvalReport:
    """The scores of one target (a backend alone, or ``COMPOSITE``) over a phrase file.

    Rates are fractions in [0, 1], and None when nothing was there to score. Latencies are wall-clock
    times of each request, in ms, by the nearest-rank method.
    """

    target: str
    phrase_count: int
    deadline_ms: int
    effort_accuracy: float | None
    """Share of task phrases (labelled with a profile, not ``_control``) whose effort is the labelled
    one. A backend that judges no effort is scored on the router's default effort."""
    effort_off_by_one_rate: float | None
    """Share of task phrases exactly one effort level off (low vs medium, medium vs high)."""
    suggestions_made: int
    suggestion_precision: float | None
    """Of the suggestions made, the share naming the labelled profile while it differs from
    ``active``; None when none were made."""
    suggestion_recall: float | None
    """Of the phrases labelled with a profile other than ``active``, the share that got a suggestion
    of that profile; None when there are none."""
    choice_accuracy: float
    """Share of phrases whose top-scored choice (profile or ``_control``) is the labelled one."""
    macro_f1: float
    """Mean F1 of the top-scored choice over every label that was expected or predicted."""
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    deadline_miss_rate: float
    """Phrases not answered within ``deadline_ms``: unanswered, or answered later."""
    unanswered: int
    """Phrases no backend answered in time (each raised or missed the deadline), so the router fell
    back."""
    confusion: Mapping[str, Mapping[str, int]]
    """Expected label → top-scored choice → phrase count, over every profile and ``_control`` in
    order, plus a ``NO_ANSWER`` column when ``unanswered`` is not zero."""


@dataclass(frozen=True)
class _Outcome:
    """What a target decided for one phrase; ``choice`` None means no backend answered."""

    choice: str | None
    effort: Effort
    suggestion: str | None
    latency_ms: float
    in_time: bool


def load_phrases(path: str | Path) -> PhraseFile:
    """Load and check the phrase file at ``path``.

    Raises ``PhraseFileError`` when it cannot be read, a key is unknown or of the wrong type, there
    are no phrases, two phrases share a text, an effort is not low, medium, or high, a phrase has no
    ``active`` profile or one the ``profiles`` section (when present) does not define, or a phrase
    ``follows`` a text that no earlier phrase has.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise PhraseFileError(f"{path}: cannot read phrase file: {error}") from None
    if not isinstance(raw, dict):
        raise PhraseFileError(f"{path}: expected a mapping with a 'phrases' list")
    raw = cast(dict[str, Any], raw)
    _reject_unknown(raw, frozenset({"profiles", "phrases"}), str(path))
    profiles = _profiles(raw["profiles"], f"{path}: profiles") if "profiles" in raw else None
    return PhraseFile(profiles=profiles, phrases=_phrases(raw.get("phrases"), profiles, f"{path}: phrases"))


async def evaluate_config(
    phrase_file: PhraseFile,
    config: Config | None,
    *,
    only: str | None = None,
    embed: Embedder | None = None,
) -> list[EvalReport]:
    """Evaluate the router ``config`` describes over ``phrase_file``, then close its backends.

    ``config`` supplies the router settings and API keys; None means the ``knn`` backend alone with
    default settings. The router's profiles are the file's ``profiles``, else ``config``'s.
    ``only`` is a backend name, to evaluate just that backend alone (replacing the configured main
    path, without a fast path), or ``COMPOSITE``; None evaluates each configured backend alone, then
    the composite. ``embed`` replaces the ``knn`` embedding model.

    Raises ``PhraseFileError`` as ``evaluate`` does, or when there are no profiles; ``ConfigError``
    when ``only`` is an API backend whose settings ``config`` lacks; ``ValueError`` when ``only``
    is an API backend and ``config`` is None (its key comes from the config).
    """
    profiles = phrase_file.profiles
    if config is None:
        if only not in (None, COMPOSITE, KnnBackend.name):
            raise ValueError(f"the {only} backend needs a config file for its API key")
        router = RouterConfig(backend=KnnBackend.name)
        backends: list[RouterBackend] = [KnnBackend(embed)]
    else:
        router = config.router
        if only not in (None, COMPOSITE):
            router = dataclasses.replace(router, backend=only, fast_path=None)
        backends = build_backends(dataclasses.replace(config, router=router), embed=embed)
        profiles = profiles if profiles is not None else config.profiles
    if profiles is None:
        raise PhraseFileError("the phrase file has no profiles section and no config gives them")

    targets = [only] if only is not None else [*(backend.name for backend in backends), COMPOSITE]
    try:
        return await evaluate(phrase_file.phrases, profiles, router, backends, targets)
    finally:
        for backend in backends:
            await backend.close()


async def evaluate(
    phrases: Sequence[LabelledPhrase],
    profiles: Mapping[str, ProfileConfig],
    router: RouterConfig,
    backends: Sequence[RouterBackend],
    targets: Sequence[str],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> list[EvalReport]:
    """Route every phrase through each target in turn and score it; one report per target, in order.

    A target is a backend's name or ``COMPOSITE``. Each target is a ``CompositeRouter`` over
    ``router``'s settings (deadline, circuit breaker, suggestion rule): a backend's name means that
    backend alone as the main path, without a fast path; ``COMPOSITE`` means ``router``'s own paths
    over ``backends``. Phrases are routed in order, each with its ``active`` profile and every profile
    permitted. A follow-up is sent by the speaker of the phrase it follows, with that speaker's earlier
    phrases as history; each other phrase starts a new speaker. ``clock`` returns seconds and times
    each request; the deadline is ``router.deadline_ms``. ``phrases`` must not be empty.

    Raises ``PhraseFileError`` when a phrase's profile is neither a profile nor ``_control``, or its
    ``active`` is not a profile; ``ValueError`` when a target is neither a backend in ``backends`` nor
    ``COMPOSITE``, or ``CompositeRouter`` rejects its arguments. A backend raising from ``classify``
    leaves the phrase unanswered; one raising from ``configure`` propagates.
    """
    choices = (*profiles, CONTROL_PROFILE)
    if unknown := sorted({phrase.profile for phrase in phrases} - set(choices)):
        raise PhraseFileError(f"phrases expect profiles the router does not have: {', '.join(unknown)}")
    if unknown := sorted({phrase.active for phrase in phrases} - set(profiles)):
        raise PhraseFileError(
            f"phrases are active on profiles the router does not have: {', '.join(unknown)}"
        )
    by_name = {backend.name: backend for backend in backends}
    if missing := [target for target in targets if target != COMPOSITE and target not in by_name]:
        raise ValueError(f"no backend named '{missing[0]}' to evaluate")

    speakers = _speakers(phrases)
    reports: list[EvalReport] = []
    for target in targets:
        if target == COMPOSITE:
            composite = CompositeRouter(router, profiles, backends, clock=clock)
        else:
            alone = dataclasses.replace(router, backend=target, fast_path=None)
            composite = CompositeRouter(alone, profiles, [by_name[target]], clock=clock)
        outcomes = await _run(composite, choices, phrases, speakers, router.deadline_ms, clock)
        reports.append(_report(target, phrases, outcomes, choices, router.deadline_ms))
    return reports


def to_json(reports: Sequence[EvalReport]) -> dict[str, Any]:
    """The reports as a JSON-ready object: ``{"reports": [<EvalReport fields>, ...]}``."""
    return {"reports": [dataclasses.asdict(report) for report in reports]}


def format_table(reports: Sequence[EvalReport]) -> str:
    """The reports as a human-readable table, one block per target."""
    blocks: list[str] = []
    for report in reports:
        latencies = " / ".join(
            f"{ms:.1f}" for ms in (report.latency_p50_ms, report.latency_p95_ms, report.latency_p99_ms)
        )
        rows = (
            ("effort accuracy", _rate(report.effort_accuracy)),
            ("effort off-by-one", _rate(report.effort_off_by_one_rate)),
            ("suggestions made", str(report.suggestions_made)),
            ("suggestion precision", _rate(report.suggestion_precision)),
            ("suggestion recall", _rate(report.suggestion_recall)),
            ("choice accuracy", _rate(report.choice_accuracy)),
            ("choice macro-F1", _rate(report.macro_f1)),
            ("latency p50/p95/p99", f"{latencies} ms"),
            ("deadline misses", f"{report.deadline_miss_rate:.3f}  ({report.unanswered} unanswered)"),
        )
        lines = [
            f"{report.target}  ({report.phrase_count} phrases, deadline {report.deadline_ms} ms)",
            *(f"  {label:<22}{value}" for label, value in rows),
            "  confusion of the top choice (rows: expected, columns: predicted)",
        ]
        columns = list(next(iter(report.confusion.values())))
        label_width = max(len(label) for label in report.confusion)
        widths = [max(len(column), 3) for column in columns]
        header = "".join(f"  {column:>{width}}" for column, width in zip(columns, widths, strict=True))
        lines.append(f"    {'':<{label_width}}{header}")
        for expected, row in report.confusion.items():
            cells = "".join(
                f"  {row[column]:>{width}}" for column, width in zip(columns, widths, strict=True)
            )
            lines.append(f"    {expected:<{label_width}}{cells}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _speakers(phrases: Sequence[LabelledPhrase]) -> list[tuple[str, tuple[str, ...]]]:
    """Each phrase's speaker and history (that speaker's earlier phrases, oldest first).

    Assumes every ``follows`` names an earlier phrase, as ``load_phrases`` checks.
    """
    speaker_of: dict[str, str] = {}
    said: dict[str, list[str]] = {}
    speakers: list[tuple[str, tuple[str, ...]]] = []
    for index, phrase in enumerate(phrases):
        speaker = speaker_of[phrase.follows] if phrase.follows is not None else f"eval-{index}"
        history = said.setdefault(speaker, [])
        speakers.append((speaker, tuple(history)))
        history.append(phrase.text)
        speaker_of[phrase.text] = speaker
    return speakers


async def _run(
    router: CompositeRouter,
    choices: Sequence[str],
    phrases: Sequence[LabelledPhrase],
    speakers: Sequence[tuple[str, tuple[str, ...]]],
    deadline_ms: int,
    clock: Callable[[], float],
) -> list[_Outcome]:
    await router.configure()
    outcomes: list[_Outcome] = []
    for phrase, (speaker, history) in zip(phrases, speakers, strict=True):
        started = clock()
        decision = await router.route(phrase.text, speaker, active_profile=phrase.active, history=history)
        latency_ms = (clock() - started) * 1000
        probs = decision.probabilities
        choice = max(choices, key=lambda choice: probs.get(choice, 0.0)) if probs else None
        in_time = choice is not None and latency_ms <= deadline_ms
        outcomes.append(_Outcome(choice, decision.effort, decision.suggestion, latency_ms, in_time))
    return outcomes


def _report(
    target: str,
    phrases: Sequence[LabelledPhrase],
    outcomes: Sequence[_Outcome],
    choices: Sequence[str],
    deadline_ms: int,
) -> EvalReport:
    count = len(phrases)
    scored = list(zip(phrases, outcomes, strict=True))
    unanswered = sum(outcome.choice is None for outcome in outcomes)
    columns = [*choices, NO_ANSWER] if unanswered else list(choices)
    confusion = {expected: dict.fromkeys(columns, 0) for expected in choices}
    for phrase, outcome in scored:
        confusion[phrase.profile][outcome.choice or NO_ANSWER] += 1

    steps = [
        abs(_EFFORTS.index(outcome.effort) - _EFFORTS.index(phrase.effort))
        for phrase, outcome in scored
        if phrase.profile != CONTROL_PROFILE
    ]
    suggested = [(phrase, outcome) for phrase, outcome in scored if outcome.suggestion is not None]
    right_suggestions = sum(
        outcome.suggestion == phrase.profile and phrase.profile != phrase.active
        for phrase, outcome in suggested
    )
    switches = [
        (phrase, outcome)
        for phrase, outcome in scored
        if phrase.profile not in (CONTROL_PROFILE, phrase.active)
    ]
    p50, p95, p99 = np.percentile(
        [outcome.latency_ms for outcome in outcomes], [50, 95, 99], method="inverted_cdf"
    )
    return EvalReport(
        target=target,
        phrase_count=count,
        deadline_ms=deadline_ms,
        effort_accuracy=_share(steps.count(0), len(steps)),
        effort_off_by_one_rate=_share(steps.count(1), len(steps)),
        suggestions_made=len(suggested),
        suggestion_precision=_share(right_suggestions, len(suggested)),
        suggestion_recall=_share(
            sum(outcome.suggestion == phrase.profile for phrase, outcome in switches), len(switches)
        ),
        choice_accuracy=sum(confusion[choice][choice] for choice in choices) / count,
        macro_f1=_macro_f1(confusion, choices),
        latency_p50_ms=float(p50),
        latency_p95_ms=float(p95),
        latency_p99_ms=float(p99),
        deadline_miss_rate=sum(not outcome.in_time for outcome in outcomes) / count,
        unanswered=unanswered,
        confusion=confusion,
    )


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _macro_f1(confusion: Mapping[str, Mapping[str, int]], choices: Sequence[str]) -> float:
    """Mean F1 over the choices that were expected or predicted at least once; F1 is 0 for a choice
    with no correct prediction."""
    scores: list[float] = []
    for choice in choices:
        expected = sum(confusion[choice].values())
        predicted = sum(row[choice] for row in confusion.values())
        if not expected and not predicted:
            continue
        correct = confusion[choice][choice]
        scores.append(2 * correct / (expected + predicted))
    return sum(scores) / len(scores)


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _profiles(raw: Any, where: str) -> Mapping[str, ProfileConfig]:
    if not isinstance(raw, dict) or not raw:
        raise PhraseFileError(f"{where}: expected a mapping of profile names")
    profiles: dict[str, ProfileConfig] = {}
    for name, body in cast(dict[Any, Any], raw).items():
        here = f"{where}.{name}"
        if not isinstance(name, str) or name == CONTROL_PROFILE:
            raise PhraseFileError(f"{here}: not a valid profile name")
        body = {} if body is None else body
        if not isinstance(body, dict):
            raise PhraseFileError(f"{here}: expected a mapping")
        body = cast(dict[str, Any], body)
        _reject_unknown(body, _PROFILE_KEYS, here)
        description = body.get("description", "")
        examples = body.get("examples", [])
        if not isinstance(description, str):
            raise PhraseFileError(f"{here}.description: expected a string")
        if not isinstance(examples, list) or not all(isinstance(e, str) for e in cast(list[Any], examples)):
            raise PhraseFileError(f"{here}.examples: expected a list of strings")
        profiles[name] = ProfileConfig(description=description, examples=tuple(cast(list[str], examples)))
    return profiles


def _phrases(
    raw: Any, profiles: Mapping[str, ProfileConfig] | None, where: str
) -> tuple[LabelledPhrase, ...]:
    """The phrases of ``raw``; each ``active`` must be one of ``profiles``, or any profile name when
    ``profiles`` is None (``evaluate`` checks it against the router's)."""
    if not isinstance(raw, list) or not raw:
        raise PhraseFileError(f"{where}: expected a non-empty list")
    phrases: list[LabelledPhrase] = []
    texts: set[str] = set()
    for index, item in enumerate(cast(list[Any], raw)):
        here = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise PhraseFileError(f"{here}: expected a mapping")
        item = cast(dict[str, Any], item)
        _reject_unknown(item, _PHRASE_KEYS, here)
        text, profile, active, effort, follows = (
            item.get(key) for key in ("text", "profile", "active", "effort", "follows")
        )
        if not isinstance(text, str) or not text:
            raise PhraseFileError(f"{here}.text: expected a non-empty string")
        if text in texts:
            raise PhraseFileError(f"{here}.text: '{text}' appears twice")
        if not isinstance(profile, str):
            raise PhraseFileError(f"{here}.profile: expected a string")
        if not isinstance(active, str) or active == CONTROL_PROFILE:
            raise PhraseFileError(f"{here}.active: expected the profile the speaker is on")
        if profiles is not None and active not in profiles:
            raise PhraseFileError(f"{here}.active: '{active}' is not a profile in the profiles section")
        if effort not in _EFFORTS:
            raise PhraseFileError(f"{here}.effort: expected one of {', '.join(_EFFORTS)}")
        if follows is not None and follows not in texts:
            raise PhraseFileError(f"{here}.follows: no earlier phrase is '{follows}'")
        texts.add(text)
        phrases.append(
            LabelledPhrase(
                text=text, profile=profile, effort=cast(Effort, effort), active=active, follows=follows
            )
        )
    return tuple(phrases)


def _reject_unknown(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if unknown := sorted(set(raw) - allowed):
        raise PhraseFileError(f"{where}: unknown key '{unknown[0]}'")
