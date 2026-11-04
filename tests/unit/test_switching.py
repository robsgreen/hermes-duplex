"""Switch requests by name or alias, refused targets, a further request after the target, and short
yes/no answers."""

import pytest

from hermes_duplex.switching import (
    SwitchRequest,
    is_affirmative,
    is_negative,
    parse_switch_request,
    read_switch_request,
)

NAMES = {
    "general": ["everyday"],
    "stock-picker": ["stocks", "the stock picker", "portfolio"],
    "home": ["the house"],
}


@pytest.mark.parametrize(
    ("text", "profile"),
    [
        ("switch to stock-picker", "stock-picker"),
        ("Switch to Stock Picker.", "stock-picker"),
        ("let me talk to the stock picker", "stock-picker"),
        ("can I talk to stocks, please", "stock-picker"),
        ("go back to general", "general"),
        ("change   to   the  house!", "home"),
        ("switch to the general profile", "general"),
        ("I'd like to talk to someone else; switch to home", "home"),
    ],
)
def test_switch_phrase_names_a_profile(text: str, profile: str) -> None:
    assert parse_switch_request(text, NAMES) == profile


def test_unknown_target_is_ambiguous() -> None:
    assert parse_switch_request("switch to finance", NAMES) == "ambiguous"


def test_alias_that_fits_two_profiles_is_ambiguous() -> None:
    names = {"stock-picker": ["the markets"], "news": ["the markets"]}

    assert parse_switch_request("talk to the markets", names) == "ambiguous"


def test_exact_name_wins_over_another_profiles_alias() -> None:
    names = {"general": [], "home": ["general"]}

    assert parse_switch_request("switch to general", names) == "general"


def test_longest_alias_wins() -> None:
    names = {"stock-picker": ["stocks"], "funds": ["stocks and funds"]}

    assert parse_switch_request("talk to stocks and funds", names) == "funds"
    assert parse_switch_request("talk to stocks about Tesla", names) == "stock-picker"


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather in Paris",
        "talk to me about Tesla",
        "Talk to us about the plan",
        "can you talk to you know who",
        "switch to",
        "let me talk to the",
        "",
        "switching the lights to blue",
    ],
)
def test_text_without_a_switch_request_is_none(text: str) -> None:
    assert parse_switch_request(text, NAMES) is None


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("switch to stock-picker and check NVDA", "check NVDA"),
        ("Switch to the stock picker, and then tell me how NVDA did.", "tell me how NVDA did."),
        ("talk to stocks. How's my portfolio?", "How's my portfolio?"),
        ("go back to general so what's the weather in Paris", "what's the weather in Paris"),
        ("switch to stocks, please check my holdings", "please check my holdings"),
    ],
)
def test_request_after_the_target_is_the_rest(text: str, rest: str) -> None:
    request = read_switch_request(text, NAMES)

    assert request is not None
    assert request.rest == rest
    assert parse_switch_request(text, NAMES) == request.target


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("switch to stock-picker", "stock-picker"),
        ("let me talk to the stock picker please", "stock-picker"),
        ("switch to stocks, thanks!", "stock-picker"),
        ("go back to general and thank you", "general"),
        ("switch to the general profile", "general"),
        ("talk to stocks about Tesla", "stock-picker"),
        ("switch to general and", "general"),
    ],
)
def test_polite_or_unjoined_words_after_the_target_are_no_request(text: str, target: str) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(target, None)


def test_ambiguous_target_carries_no_request() -> None:
    assert read_switch_request("switch to finance and check NVDA", NAMES) == SwitchRequest("ambiguous", None)


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yeah!",
        "sure",
        "Do it.",
        "please do",
        "YES, please",
        "ok, go ahead",
        "yep, switch",
        "Sure thing",
    ],
)
def test_short_yes_is_affirmative(text: str) -> None:
    assert is_affirmative(text)
    assert not is_negative(text)


@pytest.mark.parametrize(
    "text",
    ["no", "Nope.", "no thanks", "No, thank you!", "don't", "Don’t", "stay here", "no, stay", "NAH"],
)
def test_short_no_is_negative(text: str) -> None:
    assert is_negative(text)
    assert not is_affirmative(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "thanks",
        "please",
        "yes and what's Tesla at",
        "no, I meant check my calendar",
        "yes no",
        "what's the weather",
        "don't do it yet, first check the price",
    ],
)
def test_other_text_is_neither_yes_nor_no(text: str) -> None:
    assert not is_affirmative(text)
    assert not is_negative(text)
