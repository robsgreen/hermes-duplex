"""Router eval: effort and suggestion metrics, follow-up threading, the phrase file, and the
``eval-router`` command."""

import argparse
import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex import cli
from hermes_duplex.config import CONTROL_PROFILE, ProfileConfig, RouterConfig, load_config
from hermes_duplex.router.base import BackendResult, Effort, ProfileSpec, RouteRequest
from hermes_duplex.router.eval import (
    COMPOSITE,
    NO_ANSWER,
    EvalReport,
    LabelledPhrase,
    PhraseFileError,
    evaluate,
    evaluate_config,
    load_phrases,
)
from tests.fakes.fake_embedder import fake_embed

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "router_eval" / "phrases.yaml"
PROFILES: Mapping[str, ProfileConfig] = {
    "general": ProfileConfig(description="Anything else"),
    "stock-picker": ProfileConfig(description="Portfolio and trades"),
}
ROUTER = RouterConfig(backend="knn", deadline_ms=300)

PHRASES = (
    LabelledPhrase("p1", "stock-picker", "high", active="general"),
    LabelledPhrase("p2", "stock-picker", "medium", active="stock-picker"),
    LabelledPhrase("p3", "general", "low", active="general"),
    LabelledPhrase("p4", "general", "low", active="stock-picker"),
    LabelledPhrase("p5", CONTROL_PROFILE, "medium", active="general"),
)


class _Clock:
    def __init__(self) -> None:
        self.now_s = 100.0

    def __call__(self) -> float:
        return self.now_s


@dataclass(frozen=True)
class _Answer:
    """What the stub says for one text after ``latency_ms``: ``profile`` with ``effort``, or, when
    ``profile`` is empty, an exception."""

    latency_ms: float
    profile: str = ""
    effort: Effort | None = None


GENERAL = _Answer(1.0, "general")


class _StubBackend:
    """Answers each text as scripted (``default`` for a text without a script; raising when the answer
    names no profile), sure enough of the answer's profile for the router to suggest it. Advances
    ``clock`` by the answer's latency and records each request."""

    def __init__(
        self,
        clock: _Clock,
        answers: Mapping[str, _Answer],
        *,
        name: str = "knn",
        default: _Answer = GENERAL,
    ) -> None:
        self.name = name
        self.requests: list[RouteRequest] = []
        self.closed = False
        self._clock = clock
        self._answers = answers
        self._default = default
        self._choices: tuple[str, ...] = ()

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        self._choices = tuple(spec.name for spec in profiles)

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        self.requests.append(req)
        answer = self._answers.get(req.text, self._default)
        self._clock.now_s += answer.latency_ms / 1000
        if not answer.profile:
            raise RuntimeError("backend down")
        effort_probs: Mapping[Effort, float] | None = None
        if answer.effort is not None:
            effort_probs = {answer.effort: 0.9}
        return BackendResult(
            profile_probs={choice: 0.9 if choice == answer.profile else 0.05 for choice in self._choices},
            effort_probs=effort_probs,
            latency_ms=round(answer.latency_ms),
            backend=self.name,
        )

    async def close(self) -> None:
        self.closed = True


ANSWERS = {
    "p1": _Answer(10, "stock-picker", "high"),  # right; effort exact; suggests stock-picker (right)
    "p2": _Answer(20, "general", "low"),  # wrong; effort one off; suggests general (wrong)
    "p3": _Answer(30, "general", "high"),  # right; effort two off; no suggestion: general is active
    "p4": _Answer(400),  # raises after 400 ms: default effort medium, one off; no suggestion (missed)
    "p5": _Answer(350, CONTROL_PROFILE, "medium"),  # right, late; a control request: effort not scored
}


async def _evaluate(targets: Sequence[str], answers: Mapping[str, _Answer] = ANSWERS) -> list[EvalReport]:
    clock = _Clock()
    stub = _StubBackend(clock, answers)
    return await evaluate(PHRASES, PROFILES, ROUTER, [stub], targets, clock=clock)


async def test_scores_a_backend_alone() -> None:
    [report] = await _evaluate(["knn"])

    assert report.target == "knn"
    assert report.phrase_count == 5
    assert report.deadline_ms == 300
    # Over the four task phrases p1-p4: steps 0, 1, 2, 1.
    assert report.effort_accuracy == pytest.approx(1 / 4)
    assert report.effort_off_by_one_rate == pytest.approx(2 / 4)
    assert report.suggestions_made == 2
    assert report.suggestion_precision == pytest.approx(1 / 2)
    # p1 and p4 belong to a profile other than the active one; only p1 got its suggestion.
    assert report.suggestion_recall == pytest.approx(1 / 2)
    assert report.choice_accuracy == pytest.approx(3 / 5)
    # F1: general 2·1/(2+2), stock-picker 2·1/(2+1), _control 2·1/(1+1).
    assert report.macro_f1 == pytest.approx((1 / 2 + 2 / 3 + 1) / 3)
    assert report.confusion == {
        "general": {"general": 1, "stock-picker": 0, CONTROL_PROFILE: 0, NO_ANSWER: 1},
        "stock-picker": {"general": 1, "stock-picker": 1, CONTROL_PROFILE: 0, NO_ANSWER: 0},
        CONTROL_PROFILE: {"general": 0, "stock-picker": 0, CONTROL_PROFILE: 1, NO_ANSWER: 0},
    }
    # Nearest rank over 10, 20, 30, 350, 400 ms.
    assert (report.latency_p50_ms, report.latency_p95_ms, report.latency_p99_ms) == pytest.approx(
        (30.0, 400.0, 400.0)
    )
    assert report.deadline_miss_rate == pytest.approx(2 / 5)
    assert report.unanswered == 1


async def test_a_phrase_on_its_own_profile_counts_no_suggestion_as_right_and_a_suggestion_as_wrong() -> None:
    phrases = (
        LabelledPhrase("weather", "general", "low", active="general"),
        LabelledPhrase("portfolio", "stock-picker", "low", active="stock-picker"),
    )
    clock = _Clock()

    quiet = _StubBackend(clock, {"portfolio": _Answer(1.0, "stock-picker")})
    [report] = await evaluate(phrases, PROFILES, ROUTER, [quiet], ["knn"], clock=clock)
    assert report.suggestions_made == 0
    assert (report.suggestion_precision, report.suggestion_recall) == (None, None)

    eager = _StubBackend(clock, {"portfolio": _Answer(1.0, "general")})
    [report] = await evaluate(phrases, PROFILES, ROUTER, [eager], ["knn"], clock=clock)
    assert report.suggestions_made == 1
    assert report.suggestion_precision == 0.0


async def test_a_backend_that_judges_no_effort_is_scored_on_the_default_effort() -> None:
    [report] = await _evaluate(["knn"], {"p1": _Answer(1.0, "stock-picker")})

    # Every task phrase gets medium: p1 is one off, p2 exact, p3 and p4 one off.
    assert report.effort_accuracy == pytest.approx(1 / 4)
    assert report.effort_off_by_one_rate == pytest.approx(3 / 4)
    assert NO_ANSWER not in report.confusion["general"]


async def test_the_composite_runs_the_configured_main_path_and_a_backend_target_runs_alone() -> None:
    clock = _Clock()
    knn = _StubBackend(clock, ANSWERS)
    decisions = _StubBackend(clock, {}, name="decisions", default=_Answer(5.0, "general", "low"))
    router = dataclasses.replace(ROUTER, backend="decisions")

    reports = await evaluate(PHRASES, PROFILES, router, [knn, decisions], ["knn", COMPOSITE], clock=clock)

    knn_report, composite = reports
    assert knn_report.effort_accuracy == pytest.approx(1 / 4)
    assert composite.target == COMPOSITE
    # decisions says general with low effort every time: p3 and p4 exact; it suggests general to the
    # two speakers on stock-picker, rightly for p4 only.
    assert composite.effort_accuracy == pytest.approx(2 / 4)
    assert composite.suggestions_made == 2
    assert composite.suggestion_precision == pytest.approx(1 / 2)
    assert composite.choice_accuracy == pytest.approx(2 / 5)
    assert composite.unanswered == 0
    assert len(knn.requests) == len(PHRASES)  # only for its own target


async def test_a_follow_up_comes_from_the_same_speaker_with_history_and_its_active_profile() -> None:
    phrases = (
        LabelledPhrase("how is my portfolio", "stock-picker", "medium", active="general"),
        LabelledPhrase("what's the weather", "general", "low", active="general"),
        LabelledPhrase(
            "and Tesla", "stock-picker", "medium", active="stock-picker", follows="how is my portfolio"
        ),
    )
    clock = _Clock()
    stub = _StubBackend(clock, {"how is my portfolio": _Answer(1.0, "stock-picker")})

    await evaluate(phrases, PROFILES, ROUTER, [stub], ["knn"], clock=clock)

    first, other, follow_up = stub.requests
    assert follow_up.speaker == first.speaker != other.speaker
    assert follow_up.history == ("how is my portfolio",)
    assert (first.history, first.active_profile) == ((), "general")
    assert follow_up.active_profile == "stock-picker"


async def test_a_label_the_router_does_not_have_is_a_phrase_file_error() -> None:
    with pytest.raises(PhraseFileError, match="crypto"):
        await evaluate(
            [LabelledPhrase("buy bitcoin", "crypto", "low", active="general")], PROFILES, ROUTER, [], ["knn"]
        )


async def test_an_active_profile_the_router_does_not_have_is_a_phrase_file_error() -> None:
    with pytest.raises(PhraseFileError, match="active on profiles .*crypto"):
        await evaluate(
            [LabelledPhrase("buy bitcoin", "general", "low", active="crypto")], PROFILES, ROUTER, [], ["knn"]
        )


async def test_an_api_backend_alone_from_a_config_whose_main_path_differs(tmp_path: Path) -> None:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:9"},
        "provider": {"name": "fake"},
        "router": {"backend": "decisions", "decisions": {"api_key_env": "OPENAI_KEY"}},
        "profiles": {"general": {}},
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path, env={"OPENAI_KEY": "openai-key-0123456789", "HERMES_HOME": str(tmp_path)})

    reports = await evaluate_config(load_phrases(FIXTURE), config, only="knn", embed=fake_embed)

    assert [report.target for report in reports] == ["knn"]
    assert reports[0].phrase_count == len(load_phrases(FIXTURE).phrases)


def test_the_fixture_loads_with_profiles_active_profiles_and_follow_ups() -> None:
    phrase_file = load_phrases(FIXTURE)

    assert phrase_file.profiles is not None
    assert set(phrase_file.profiles) == {"general", "stock-picker"}
    phrases = phrase_file.phrases
    for label in ("general", "stock-picker", CONTROL_PROFILE):
        assert 30 <= sum(phrase.profile == label for phrase in phrases) <= 50
    assert {phrase.active for phrase in phrases} == {"general", "stock-picker"}
    switches = sum(phrase.profile not in (CONTROL_PROFILE, phrase.active) for phrase in phrases)
    stays = sum(phrase.profile == phrase.active for phrase in phrases)
    assert switches >= 20 and stays >= 20
    assert any(phrase.follows is not None for phrase in phrases)


HI: dict[str, Any] = {"text": "hi", "profile": "general", "active": "general", "effort": "low"}


@pytest.mark.parametrize(
    ("phrases", "message"),
    [
        ([], "non-empty list"),
        ([{**HI, "effort": "huge"}], "effort"),
        ([{**HI, "mood": "happy"}], "unknown key 'mood'"),
        ([HI] * 2, "appears twice"),
        ([{**HI, "text": "and then", "follows": "hi"}], "no earlier phrase"),
        ([{key: value for key, value in HI.items() if key != "active"}], "active"),
        ([{**HI, "active": CONTROL_PROFILE}], "active"),
        ([{**HI, "active": "crypto"}], "'crypto' is not a profile"),
    ],
    ids=[
        "empty",
        "bad-effort",
        "unknown-key",
        "duplicate",
        "follows-nothing",
        "no-active",
        "control-active",
        "undefined-active",
    ],
)
def test_a_malformed_phrase_file_is_rejected(tmp_path: Path, phrases: list[Any], message: str) -> None:
    path = tmp_path / "phrases.yaml"
    profiles = {"general": {"description": "Anything else"}}
    path.write_text(yaml.safe_dump({"profiles": profiles, "phrases": phrases}), encoding="utf-8")

    with pytest.raises(PhraseFileError, match=message):
        load_phrases(path)


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    cli.add_commands(parser)
    return parser.parse_args(argv)


def test_command_prints_json_for_each_backend_and_the_composite(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli.eval_router(_parse("eval-router", str(FIXTURE), "--json"), embed=fake_embed)

    assert status == 0
    output = json.loads(capsys.readouterr().out)
    assert [report["target"] for report in output["reports"]] == ["knn", COMPOSITE]
    for report in output["reports"]:
        assert set(report) == {
            "target",
            "phrase_count",
            "deadline_ms",
            "effort_accuracy",
            "effort_off_by_one_rate",
            "suggestions_made",
            "suggestion_precision",
            "suggestion_recall",
            "choice_accuracy",
            "macro_f1",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "deadline_miss_rate",
            "unanswered",
            "confusion",
        }
        assert set(report["confusion"]) == {"general", "stock-picker", CONTROL_PROFILE}
        assert 0.0 <= report["effort_accuracy"] <= 1.0
        assert report["suggestion_recall"] is not None


def test_command_prints_a_table_for_one_backend(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli.eval_router(_parse("eval-router", str(FIXTURE), "--backend", "knn"), embed=fake_embed)

    assert status == 0
    table = capsys.readouterr().out
    assert table.startswith("knn  (")
    for row in (
        "effort accuracy",
        "effort off-by-one",
        "suggestions made",
        "suggestion precision",
        "suggestion recall",
        "choice accuracy",
        "choice macro-F1",
        "p50/p95/p99",
        "deadline misses",
    ):
        assert row in table
    assert COMPOSITE not in table


def test_command_silences_route_lines_while_it_runs_and_restores_the_level_after(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    route_logger = logging.getLogger("hermes_duplex.router.composite")
    route_logger.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.INFO):
            status = cli.eval_router(
                _parse("eval-router", str(FIXTURE), "--backend", "knn"), embed=fake_embed
            )

        assert status == 0
        assert not [record for record in caplog.records if record.name == route_logger.name]
        assert route_logger.level == logging.DEBUG
    finally:
        route_logger.setLevel(logging.NOTSET)
    capsys.readouterr()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--backend", "decisions"), "needs a config file"),
        (("--config", "/nonexistent/duplex.yaml"), "config file not found"),
    ],
    ids=["api-backend-without-config", "missing-config"],
)
def test_command_reports_an_unusable_setup(
    argv: tuple[str, ...], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.eval_router(_parse("eval-router", str(FIXTURE), *argv), embed=fake_embed) == 1
    assert message in capsys.readouterr().err


def test_command_reports_an_unreadable_phrase_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(_parse("eval-router", str(tmp_path / "missing.yaml"))) == 1
    assert "cannot read phrase file" in capsys.readouterr().err
