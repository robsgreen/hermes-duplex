"""Router eval: how well each backend alone, and the composite router, route a file of labelled phrases.

A phrase file is YAML with a ``phrases`` list and an optional ``profiles`` section.
``tests/fixtures/router_eval/phrases.yaml`` is the reference file and documents the format:

- ``profiles`` maps each profile to its ``description`` and ``examples``, in the shape of
  duplex.yaml's ``profiles`` section; it is what the router is configured with.
- Each phrase has the ``text`` a speaker says, the ``profile`` it should route to (``_control`` for
  status and cancel requests), and the ``effort`` it deserves. A follow-up names the phrase it
  ``follows``, said earlier by the same speaker. A ``control_intent`` key is allowed and not scored.

For each target the eval reports accuracy, macro-F1, the confusion matrix, effort exact and
off-by-one rates, latency percentiles, and the deadline-miss rate (``EvalReport``).
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
from .base import Effort, ProfileSpec, RouterBackend, RouteRequest, choice_specs
from .composite import CompositeRouter
from .factory import build_backends
from .knn import Embedder, KnnBackend

logger = logging.getLogger(__name__)

COMPOSITE = "composite"
"""The target name of the composite router, beside the backend names."""

NO_ANSWER = "no_answer"
"""The confusion-matrix column of phrases a backend gave no answer for (it raised)."""

_EFFORTS: tuple[Effort, ...] = get_args(Effort)
_PHRASE_KEYS = frozenset({"text", "profile", "effort", "follows", "control_intent"})
_PROFILE_KEYS = frozenset({"description", "examples"})


class PhraseFileError(ValueError):
    """The phrase file is unreadable or malformed, or its labels do not match the router's profiles."""


@dataclass(frozen=True)
class LabelledPhrase:
    """One utterance and where it should go. ``follows`` is the text of an earlier phrase that the same
    speaker said just before it, or None."""

    text: str
    profile: str
    effort: Effort
    follows: str | None = None


@dataclass(frozen=True)
class PhraseFile:
    """A loaded phrase file. ``profiles`` is None when the file has no ``profiles`` section."""

    profiles: Mapping[str, ProfileConfig] | None
    phrases: tuple[LabelledPhrase, ...]


@dataclass(frozen=True)
class EvalReport:
    """The scores of one target (a backend alone, or ``COMPOSITE``) over a phrase file.

    Rates are fractions of ``phrase_count`` in [0, 1]. A phrase the target gave no answer for counts
    as wrong and as a deadline miss. The effort rates are None when the target judges no effort (a
    backend that never returns effort probabilities); ``effort_off_by_one_rate`` counts phrases
    exactly one level off (low vs medium, medium vs high). Latencies are wall-clock times of each
    request, in ms, by the nearest-rank method.
    """

    target: str
    phrase_count: int
    deadline_ms: int
    accuracy: float
    macro_f1: float
    """Mean F1 over every label that was expected or predicted."""
    effort_exact_rate: float | None
    effort_off_by_one_rate: float | None
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    deadline_miss_rate: float
    """Phrases not answered within ``deadline_ms``: the target raised, or took longer."""
    errors: int
    """Phrases the target raised for."""
    confusion: Mapping[str, Mapping[str, int]]
    """Expected label → predicted label → phrase count, over every profile and ``_control`` in
    order, plus a ``NO_ANSWER`` column when ``errors`` is not zero."""


@dataclass(frozen=True)
class _Outcome:
    """What a target answered for one phrase; ``profile`` None means it raised."""

    profile: str | None
    effort: Effort | None
    latency_ms: float
    in_time: bool


def load_phrases(path: str | Path) -> PhraseFile:
    """Load and check the phrase file at ``path``.

    Raises ``PhraseFileError`` when it cannot be read, a key is unknown or of the wrong type, there
    are no phrases, two phrases share a text, an effort is not low, medium, or high, or a phrase
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
    return PhraseFile(profiles=profiles, phrases=_phrases(raw.get("phrases"), f"{path}: phrases"))


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

    A target is a backend's name (that backend alone, configured with every profile and ``_control``)
    or ``COMPOSITE`` (a ``CompositeRouter`` over ``router`` and ``backends``). Phrases are routed in
    order. A follow-up is sent by the speaker of the phrase it follows, with that speaker's earlier
    phrases as history; each other phrase starts a new speaker. Each target gets that speaker's last
    predicted profile as ``active_profile``, starting from ``router.default_profile`` (the first
    profile when that is not one of ``profiles``). The composite's prediction is ``_control`` for a
    control request, else its decision's most probable choice. ``clock`` returns seconds and times
    each request; the deadline is ``router.deadline_ms``. ``phrases`` must not be empty.

    Raises ``PhraseFileError`` when a phrase's profile is neither a profile nor ``_control``, and
    ``ValueError`` when a target is neither a backend in ``backends`` nor ``COMPOSITE``, or
    ``CompositeRouter`` rejects its arguments. A backend raising from ``classify`` is a wrong answer,
    not an error; one raising from ``configure`` propagates.
    """
    choices = (*profiles, CONTROL_PROFILE)
    if unknown := sorted({phrase.profile for phrase in phrases} - set(choices)):
        raise PhraseFileError(f"phrases expect profiles the router does not have: {', '.join(unknown)}")
    by_name = {backend.name: backend for backend in backends}
    if missing := [target for target in targets if target != COMPOSITE and target not in by_name]:
        raise ValueError(f"no backend named '{missing[0]}' to evaluate")
    composite = CompositeRouter(router, profiles, backends, clock=clock) if COMPOSITE in targets else None

    speakers = _speakers(phrases)
    first_active = router.default_profile if router.default_profile in profiles else next(iter(profiles))
    reports: list[EvalReport] = []
    for target in targets:
        if target == COMPOSITE:
            assert composite is not None
            outcomes = await _run_composite(
                composite, choices, phrases, speakers, first_active, router.deadline_ms, clock
            )
        else:
            specs = choice_specs(profiles, router.control_examples)
            outcomes = await _run_backend(
                by_name[target], specs, choices, phrases, speakers, first_active, router.deadline_ms, clock
            )
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
        lines = [
            f"{report.target}  ({report.phrase_count} phrases, deadline {report.deadline_ms} ms)",
            f"  accuracy             {report.accuracy:.3f}",
            f"  macro-F1             {report.macro_f1:.3f}",
            f"  effort exact         {_rate(report.effort_exact_rate)}",
            f"  effort off-by-one    {_rate(report.effort_off_by_one_rate)}",
            f"  latency p50/p95/p99  {latencies} ms",
            f"  deadline misses      {report.deadline_miss_rate:.3f}  ({report.errors} errors)",
            "  confusion (rows: expected, columns: predicted)",
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


async def _run_backend(
    backend: RouterBackend,
    specs: Sequence[ProfileSpec],
    choices: Sequence[str],
    phrases: Sequence[LabelledPhrase],
    speakers: Sequence[tuple[str, tuple[str, ...]]],
    first_active: str,
    deadline_ms: int,
    clock: Callable[[], float],
) -> list[_Outcome]:
    await backend.configure(specs)
    active: dict[str, str] = {}
    outcomes: list[_Outcome] = []
    for phrase, (speaker, history) in zip(phrases, speakers, strict=True):
        req = RouteRequest(
            text=phrase.text,
            speaker=speaker,
            history=history,
            active_profile=active.get(speaker, first_active),
        )
        started = clock()
        try:
            result = await backend.classify(req, deadline_ms)
        except Exception:
            latency_ms = (clock() - started) * 1000
            logger.exception("router backend %s failed on %r", backend.name, phrase.text)
            outcomes.append(_Outcome(profile=None, effort=None, latency_ms=latency_ms, in_time=False))
            continue
        latency_ms = (clock() - started) * 1000
        probs = result.profile_probs
        profile = max(choices, key=lambda choice: probs.get(choice, 0.0))
        if profile != CONTROL_PROFILE:
            active[speaker] = profile
        efforts = result.effort_probs
        effort = max(efforts, key=efforts.__getitem__) if efforts else None
        outcomes.append(_Outcome(profile, effort, latency_ms, in_time=latency_ms <= deadline_ms))
    return outcomes


async def _run_composite(
    router: CompositeRouter,
    choices: Sequence[str],
    phrases: Sequence[LabelledPhrase],
    speakers: Sequence[tuple[str, tuple[str, ...]]],
    first_active: str,
    deadline_ms: int,
    clock: Callable[[], float],
) -> list[_Outcome]:
    await router.configure()
    active: dict[str, str] = {}
    outcomes: list[_Outcome] = []
    for phrase, (speaker, history) in zip(phrases, speakers, strict=True):
        started = clock()
        decision = await router.route(
            phrase.text, speaker, active_profile=active.get(speaker, first_active), history=history
        )
        latency_ms = (clock() - started) * 1000
        probs = decision.probabilities
        if decision.control_intent is not None:
            profile = CONTROL_PROFILE
        elif probs:
            profile = max(choices, key=lambda choice: probs.get(choice, 0.0))
        else:
            profile = active.get(speaker, first_active)
        if profile != CONTROL_PROFILE:
            active[speaker] = profile
        outcomes.append(_Outcome(profile, decision.effort, latency_ms, latency_ms <= deadline_ms))
    return outcomes


def _report(
    target: str,
    phrases: Sequence[LabelledPhrase],
    outcomes: Sequence[_Outcome],
    choices: Sequence[str],
    deadline_ms: int,
) -> EvalReport:
    count = len(phrases)
    errors = sum(outcome.profile is None for outcome in outcomes)
    columns = [*choices, NO_ANSWER] if errors else list(choices)
    confusion = {expected: dict.fromkeys(columns, 0) for expected in choices}
    for phrase, outcome in zip(phrases, outcomes, strict=True):
        confusion[phrase.profile][outcome.profile or NO_ANSWER] += 1

    judged_effort = any(outcome.effort is not None for outcome in outcomes)
    steps = [
        abs(_EFFORTS.index(outcome.effort) - _EFFORTS.index(phrase.effort))
        for phrase, outcome in zip(phrases, outcomes, strict=True)
        if outcome.effort is not None
    ]
    p50, p95, p99 = np.percentile(
        [outcome.latency_ms for outcome in outcomes], [50, 95, 99], method="inverted_cdf"
    )
    return EvalReport(
        target=target,
        phrase_count=count,
        deadline_ms=deadline_ms,
        accuracy=sum(confusion[choice][choice] for choice in choices) / count,
        macro_f1=_macro_f1(confusion, choices),
        effort_exact_rate=steps.count(0) / count if judged_effort else None,
        effort_off_by_one_rate=steps.count(1) / count if judged_effort else None,
        latency_p50_ms=float(p50),
        latency_p95_ms=float(p95),
        latency_p99_ms=float(p99),
        deadline_miss_rate=sum(not outcome.in_time for outcome in outcomes) / count,
        errors=errors,
        confusion=confusion,
    )


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


def _phrases(raw: Any, where: str) -> tuple[LabelledPhrase, ...]:
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
        text, profile, effort, follows = (item.get(key) for key in ("text", "profile", "effort", "follows"))
        if not isinstance(text, str) or not text:
            raise PhraseFileError(f"{here}.text: expected a non-empty string")
        if text in texts:
            raise PhraseFileError(f"{here}.text: '{text}' appears twice")
        if not isinstance(profile, str):
            raise PhraseFileError(f"{here}.profile: expected a string")
        if effort not in _EFFORTS:
            raise PhraseFileError(f"{here}.effort: expected one of {', '.join(_EFFORTS)}")
        if follows is not None and follows not in texts:
            raise PhraseFileError(f"{here}.follows: no earlier phrase is '{follows}'")
        texts.add(text)
        phrases.append(
            LabelledPhrase(text=text, profile=profile, effort=cast(Effort, effort), follows=follows)
        )
    return tuple(phrases)


def _reject_unknown(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if unknown := sorted(set(raw) - allowed):
        raise PhraseFileError(f"{where}: unknown key '{unknown[0]}'")
