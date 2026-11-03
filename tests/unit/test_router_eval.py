"""Router eval: metric values, follow-up threading, the phrase file, and the ``eval-router`` command."""

import argparse
import json
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
    LabelledPhrase("p1", "stock-picker", "high"),
    LabelledPhrase("p2", "stock-picker", "medium"),
    LabelledPhrase("p3", "general", "low"),
    LabelledPhrase("p4", "general", "low"),
    LabelledPhrase("p5", CONTROL_PROFILE, "medium"),
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


class _StubBackend:
    """Answers each text as scripted (raising when the answer names no profile), advancing ``clock``
    by the answer's latency. Records each request."""

    def __init__(self, clock: _Clock, answers: Mapping[str, _Answer], *, name: str = "knn") -> None:
        self.name = name
        self.requests: list[RouteRequest] = []
        self.closed = False
        self._clock = clock
        self._answers = answers
        self._choices: tuple[str, ...] = ()

    async def configure(self, profiles: Sequence[ProfileSpec]) -> None:
        self._choices = tuple(spec.name for spec in profiles)

    async def classify(self, req: RouteRequest, deadline_ms: int) -> BackendResult:
        self.requests.append(req)
        answer = self._answers.get(req.text, _Answer(1.0, "general"))
        self._clock.now_s += answer.latency_ms / 1000
        if not answer.profile:
            raise RuntimeError("backend down")
        effort_probs: Mapping[Effort, float] | None = None
        if answer.effort is not None:
            effort_probs = {answer.effort: 0.9}
        return BackendResult(
            profile_probs={choice: 0.8 if choice == answer.profile else 0.1 for choice in self._choices},
            effort_probs=effort_probs,
            followup_prob=None,
            latency_ms=round(answer.latency_ms),
            backend=self.name,
        )

    async def close(self) -> None:
        self.closed = True


ANSWERS = {
    "p1": _Answer(10, "stock-picker", "high"),  # right, effort exact
    "p2": _Answer(20, "general", "low"),  # wrong, effort one off
    "p3": _Answer(30, "general", "high"),  # right, effort two off
    "p4": _Answer(400),  # raises after 400 ms
    "p5": _Answer(350, CONTROL_PROFILE, "medium"),  # right, effort exact, late
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
    assert report.accuracy == pytest.approx(3 / 5)
    # F1: general 2·1/(2+2), stock-picker 2·1/(2+1), _control 2·1/(1+1).
    assert report.macro_f1 == pytest.approx((1 / 2 + 2 / 3 + 1) / 3)
    assert report.confusion == {
        "general": {"general": 1, "stock-picker": 0, CONTROL_PROFILE: 0, NO_ANSWER: 1},
        "stock-picker": {"general": 1, "stock-picker": 1, CONTROL_PROFILE: 0, NO_ANSWER: 0},
        CONTROL_PROFILE: {"general": 0, "stock-picker": 0, CONTROL_PROFILE: 1, NO_ANSWER: 0},
    }
    assert report.effort_exact_rate == pytest.approx(2 / 5)
    assert report.effort_off_by_one_rate == pytest.approx(1 / 5)
    # Nearest rank over 10, 20, 30, 350, 400 ms.
    assert (report.latency_p50_ms, report.latency_p95_ms, report.latency_p99_ms) == pytest.approx(
        (30.0, 400.0, 400.0)
    )
    assert report.deadline_miss_rate == pytest.approx(2 / 5)
    assert report.errors == 1


async def test_effort_rates_are_none_for_a_backend_that_judges_no_effort() -> None:
    [report] = await _evaluate(["knn"], {"p1": _Answer(1.0, "stock-picker")})

    assert report.effort_exact_rate is None
    assert report.effort_off_by_one_rate is None
    assert NO_ANSWER not in report.confusion["general"]


async def test_scores_the_composite_router() -> None:
    [report] = await _evaluate([COMPOSITE])

    assert report.target == COMPOSITE
    # p4's backend failure falls back to the default profile, general, which is right.
    assert report.accuracy == pytest.approx(4 / 5)
    assert report.errors == 0
    assert NO_ANSWER not in report.confusion["general"]
    # p4 gets the default effort, medium: one off from low.
    assert report.effort_exact_rate == pytest.approx(2 / 5)
    assert report.effort_off_by_one_rate == pytest.approx(2 / 5)
    assert report.deadline_miss_rate == pytest.approx(2 / 5)


async def test_a_follow_up_comes_from_the_same_speaker_with_history_and_active_profile() -> None:
    phrases = (
        LabelledPhrase("how is my portfolio", "stock-picker", "medium"),
        LabelledPhrase("what's the weather", "general", "low"),
        LabelledPhrase("and Tesla", "stock-picker", "medium", follows="how is my portfolio"),
    )
    clock = _Clock()
    stub = _StubBackend(clock, {"how is my portfolio": _Answer(1.0, "stock-picker")})

    await evaluate(phrases, PROFILES, ROUTER, [stub], ["knn"], clock=clock)

    first, other, follow_up = stub.requests
    assert follow_up.speaker == first.speaker != other.speaker
    assert follow_up.history == ("how is my portfolio",)
    assert follow_up.active_profile == "stock-picker"
    assert (first.history, first.active_profile) == ((), None)


async def test_a_label_the_router_does_not_have_is_a_phrase_file_error() -> None:
    with pytest.raises(PhraseFileError, match="crypto"):
        await evaluate([LabelledPhrase("buy bitcoin", "crypto", "low")], PROFILES, ROUTER, [], ["knn"])


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


def test_the_fixture_loads_with_profiles_and_follow_ups() -> None:
    phrase_file = load_phrases(FIXTURE)

    assert phrase_file.profiles is not None
    assert set(phrase_file.profiles) == {"general", "stock-picker"}
    for label in ("general", "stock-picker", CONTROL_PROFILE):
        assert 30 <= sum(phrase.profile == label for phrase in phrase_file.phrases) <= 50
    assert any(phrase.follows is not None for phrase in phrase_file.phrases)


@pytest.mark.parametrize(
    ("phrases", "message"),
    [
        ([], "non-empty list"),
        ([{"text": "hi", "profile": "general", "effort": "huge"}], "effort"),
        ([{"text": "hi", "profile": "general", "effort": "low", "mood": "happy"}], "unknown key 'mood'"),
        ([{"text": "hi", "profile": "general", "effort": "low"}] * 2, "appears twice"),
        ([{"text": "and then", "profile": "general", "effort": "low", "follows": "hi"}], "no earlier phrase"),
    ],
    ids=["empty", "bad-effort", "unknown-key", "duplicate", "follows-nothing"],
)
def test_a_malformed_phrase_file_is_rejected(tmp_path: Path, phrases: list[Any], message: str) -> None:
    path = tmp_path / "phrases.yaml"
    path.write_text(yaml.safe_dump({"phrases": phrases}), encoding="utf-8")

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
            "accuracy",
            "macro_f1",
            "effort_exact_rate",
            "effort_off_by_one_rate",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "deadline_miss_rate",
            "errors",
            "confusion",
        }
        assert set(report["confusion"]) == {"general", "stock-picker", CONTROL_PROFILE}
        assert 0.0 <= report["accuracy"] <= 1.0
    knn, composite = output["reports"]
    assert knn["effort_exact_rate"] is None
    assert composite["effort_exact_rate"] is not None


def test_command_prints_a_table_for_one_backend(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli.eval_router(_parse("eval-router", str(FIXTURE), "--backend", "knn"), embed=fake_embed)

    assert status == 0
    table = capsys.readouterr().out
    assert table.startswith("knn  (")
    for row in (
        "accuracy",
        "macro-F1",
        "effort exact",
        "effort off-by-one",
        "p50/p95/p99",
        "deadline misses",
    ):
        assert row in table
    assert COMPOSITE not in table


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
