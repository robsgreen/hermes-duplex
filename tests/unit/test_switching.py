"""Switch requests by name or alias, refused targets, a further request after the target, and short
yes/no answers."""

import pytest

from hermes_duplex.switching import (
    SwitchRequest,
    is_affirmative,
    is_negative,
    parse_switch_request,
    read_switch_request,
    resolve_profile,
    spoken_form,
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
        ("ok, go back to general", "general"),
        ("Thanks. Now switch to the stock picker", "stock-picker"),
        ("Hmm, what a day. Okay, let me talk to stocks", "stock-picker"),
        ("could you change to the house please", "home"),
        # An exact profile name counts wherever the phrase stands.
        ("should I switch to stock-picker", "stock-picker"),
        ("I'm done here so back to general", "general"),
    ],
)
def test_switch_phrase_names_a_profile(text: str, profile: str) -> None:
    assert parse_switch_request(text, NAMES) == profile


@pytest.mark.parametrize(
    "text", ["switch to finance", "let me talk to the finance agent", "Can I change to accounts, please?"]
)
def test_unknown_target_of_a_switch_that_opens_what_is_said_is_ambiguous(text: str) -> None:
    assert parse_switch_request(text, NAMES) == "ambiguous"


def test_a_bare_switch_to_a_word_that_is_no_profile_still_asks_which() -> None:
    # Indistinguishable from "switch to finance" without knowing what the word means.
    assert parse_switch_request("switch to Celsius", NAMES) == "ambiguous"


@pytest.mark.parametrize(
    "text",
    [
        # A phrase inside a request, with a target that is no profile name: an ordinary request.
        "Remind me to talk to Sarah",
        "remind me to talk to Sarah about the party",
        "book flights back to London",
        "flights back to London",
        "find me flights back to London on Friday",
        "what's the best way to change to metric",
        "set the thermostat to switch to Celsius",
        "how do I switch to a cheaper phone plan",
        "tell the kids it's time to change to pyjamas",
        # An alias after a phrase inside a request does not switch.
        "should I switch to stocks from bonds",
        "move my savings back to stocks",
        "is it time to switch to the house insurance",
        "remind me to talk to portfolio managers",
        # An opening phrase with a long target that is no profile, or more words after the target.
        "talk to Sarah about the party tomorrow night",
        "switch to stocks from bonds",
        "change to the house rules for the game",
        # Negated.
        "don't switch to stocks",
        "do not switch to stock-picker",
        "Never talk to general again",
        "I don't want to talk to the stock picker",
        "not now, don't change to general",
        "no need to switch to stocks",
    ],
)
def test_a_phrase_that_asks_for_no_switch_is_none(text: str) -> None:
    assert parse_switch_request(text, NAMES) is None


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
        ("talk to stocks about Tesla", "Tesla"),
        ("let me talk to the stock picker about my holdings", "my holdings"),
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
        ("switch to general and", "general"),
        ("talk to stocks about", "stock-picker"),
    ],
)
def test_polite_or_unjoined_words_after_the_target_are_no_request(text: str, target: str) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(target, None)


def test_about_joins_a_request_only_to_a_switch_that_opens_what_is_said() -> None:
    assert read_switch_request("I'd like to talk to general about Paris", NAMES) == SwitchRequest(
        "general", "Paris"
    )
    assert read_switch_request("remind me to talk to general about Paris", NAMES) is None


@pytest.mark.parametrize(
    ("words", "profile"),
    [
        ("stock-picker", "stock-picker"),
        ("Stock Picker", "stock-picker"),
        ("the stock picker", "stock-picker"),
        ("stocks", "stock-picker"),
        ("The House", "home"),
        ("general agent", "general"),
        ("finance", None),
        ("", None),
    ],
)
def test_resolve_profile_names_a_profile_by_name_or_alias(words: str, profile: str | None) -> None:
    assert resolve_profile(words, NAMES) == profile


def test_resolve_profile_prefers_a_name_and_refuses_a_shared_alias() -> None:
    assert resolve_profile("general", {"general": [], "home": ["general"]}) == "general"
    assert resolve_profile("markets", {"stock-picker": ["markets"], "news": ["the markets"]}) is None


@pytest.mark.parametrize(
    ("name", "spoken"),
    [
        ("stock-picker", ("stock", "picker")),
        ("The  Stock Picker!", ("stock", "picker")),
        ("Rob's agent", ("robs", "agent")),
        ("the", ()),
        ("--", ()),
    ],
)
def test_spoken_form_is_how_a_name_is_said(name: str, spoken: tuple[str, ...]) -> None:
    assert spoken_form(name) == spoken


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
