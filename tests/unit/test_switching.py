"""Switch requests by name or alias, refused targets, a further request after the target, and short
yes/no answers."""

import pytest

from hermes_duplex.switching import (
    SwitchRequest,
    TargetWords,
    is_affirmative,
    is_negative,
    read_switch_request,
    resolve_profile,
    spoken_form,
    target_words,
)

NAMES = {
    "general": ["everyday"],
    "stock-picker": ["stocks", "the stock picker", "portfolio"],
    "home": ["the house"],
}


def _target(text: str, names: dict[str, list[str]]) -> str | None:
    """The target of the switch request ``text`` makes, which must make one."""
    request = read_switch_request(text, names)
    assert request is not None, f"{text!r} makes no switch request"
    return request.target


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
        # Lead-ins, one after another (commas between them are fine; a comma never opens a switch).
        ("Hmm, let me talk to stocks", "stock-picker"),
        ("I need to talk to the stock picker", "stock-picker"),
        ("Can we go back to everyday", "general"),
        ("hmm, ok, let me talk to portfolio", "stock-picker"),
        ("Oh, could we change to the house", "home"),
        ("Well, I'd rather talk to stocks", "stock-picker"),
        ("Sorry, I'd love to talk to the stock picker", "stock-picker"),
        ("Right, let's go back to everyday", "general"),
        ("um, uh, can we switch to stocks", "stock-picker"),
        ("so I want to talk to the house", "home"),
        ("No, switch to stocks", "stock-picker"),
        ("Yeah, back to stocks", "stock-picker"),
        # An exact profile name counts wherever the phrase stands.
        ("should I switch to stock-picker", "stock-picker"),
        ("I'm done here so back to general", "general"),
        # "go to", where "go" is also a lead-in.
        ("go to stocks", "stock-picker"),
        ("let me go to the stock picker", "stock-picker"),
        ("Okay, go to general please", "general"),
    ],
)
def test_switch_phrase_names_a_profile(text: str, profile: str) -> None:
    assert _target(text, NAMES) == profile


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("switch to finance", "finance"),
        ("let me talk to the finance agent", "finance agent"),
        ("Can I change to accounts, please?", "accounts"),
    ],
)
def test_unknown_target_of_a_switch_that_opens_what_is_said_is_left_to_its_meaning(
    text: str, words: str
) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(None, words, None)


def test_a_bare_switch_to_a_word_that_is_no_profile_still_is_a_switch_request() -> None:
    # Indistinguishable from "switch to finance" without knowing what the word means.
    assert read_switch_request("switch to Celsius", NAMES) == SwitchRequest(None, "Celsius", None)


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
        "Hmm, don't switch to stocks",
        "I'm done with this, talk to stocks",
        "If rates fall, switch to stocks",
        "Sell my bonds, switch to stocks",
        "In my IRA, switch to stocks",
        "my advisor said, switch to stocks",
        "I don't want to, um, talk to stocks",
        "I don't want to, uh, switch to stocks",
        "well, I never want to talk to the stock picker",
        # A target that is no profile, said with more than the switch, or after "back to".
        "Back to Tesla, what's it at?",
        "Switch to Celsius. What's the weather in Paris?",
        "switch to Celsius and tell me the weather",
        "change to metric, what's five miles in kilometres",
        "talk to Sarah, she knows the address",
        "Talk to Mom about dinner",
        "what's the weather? Switch to Celsius",
        "back to work",
        "go back to bed",
        "ok, back to the weather",
        "switch to finance and check NVDA",
    ],
)
def test_a_phrase_that_asks_for_no_switch_is_none(text: str) -> None:
    assert read_switch_request(text, NAMES) is None


def test_alias_that_fits_two_profiles_is_ambiguous() -> None:
    names = {"stock-picker": ["the markets"], "news": ["the markets"]}

    assert read_switch_request("talk to the markets", names) == SwitchRequest("ambiguous", "markets", None)


def test_exact_name_wins_over_another_profiles_alias() -> None:
    names = {"general": [], "home": ["general"]}

    assert _target("switch to general", names) == "general"


def test_longest_fit_wins_across_names_and_aliases() -> None:
    names = {"general": [], "news": ["general news"]}

    assert _target("talk to general news", names) == "news"
    assert _target("talk to general", names) == "general"
    assert resolve_profile("General News", names) == "news"


def test_longest_alias_wins() -> None:
    names = {"stock-picker": ["stocks"], "funds": ["stocks and funds"]}

    assert _target("talk to stocks and funds", names) == "funds"
    assert _target("talk to stocks about Tesla", names) == "stock-picker"


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
    assert read_switch_request(text, NAMES) is None


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


@pytest.mark.parametrize(
    ("text", "target", "words"),
    [
        ("switch to stock-picker", "stock-picker", "stock-picker"),
        ("let me talk to the stock picker please", "stock-picker", "stock picker"),
        ("switch to stocks, thanks!", "stock-picker", "stocks"),
        ("go back to general and thank you", "general", "general"),
        ("switch to the general profile", "general", "general profile"),
        ("switch to general and", "general", "general"),
        ("talk to stocks about", "stock-picker", "stocks"),
    ],
)
def test_polite_or_unjoined_words_after_the_target_are_no_request(text: str, target: str, words: str) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(target, words, None)


def test_about_joins_a_request_only_to_a_switch_that_opens_what_is_said() -> None:
    assert read_switch_request("I'd like to talk to general about Paris", NAMES) == SwitchRequest(
        "general", "general", "Paris"
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


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("ok, switch to finance please", "finance"),
        ("Hmm, can we talk to the finance agent?", "finance agent"),
    ],
)
def test_an_unknown_target_said_alone_after_lead_ins_is_a_switch_request(text: str, words: str) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(None, words, None)


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


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("go to stocks and check NVDA", "check NVDA"),
        ("Go to the house, then turn on the lights", "turn on the lights"),
    ],
)
def test_go_to_a_profile_and_a_request_switches_then_hands_off_the_rest(text: str, rest: str) -> None:
    request = read_switch_request(text, NAMES)

    assert request is not None and request.rest == rest


@pytest.mark.parametrize(
    "text",
    [
        "go to the gym",
        "remind me to go to the dentist",
        "I want to go to Paris",
        "go to sleep",
        "don't go to stocks",
    ],
)
def test_go_to_a_place_is_no_switch_request(text: str) -> None:
    assert read_switch_request(text, NAMES) is None


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("go to the stock trader profile", "stock trader profile"),
        ("go back to the markets agent please", "markets agent"),
        ("let me go to the money agent", "money agent"),
    ],
)
def test_go_to_an_agent_that_fits_no_name_is_left_to_its_meaning(text: str, words: str) -> None:
    assert read_switch_request(text, NAMES) == SwitchRequest(None, words, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Each phrasing of the table, with a target that ends in an agent noun.
        (
            "use my portfolio agent",
            SwitchRequest(None, "my portfolio agent", None),
        ),  # "my" keeps it from the alias
        ("use my retirement agent", SwitchRequest(None, "my retirement agent", None)),
        (
            "connect me with the stock research assistant",
            SwitchRequest(None, "stock research assistant", None),
        ),
        ("connect me to the general bot", SwitchRequest("general", "general bot", None)),
        ("give me the house profile", SwitchRequest("home", "house profile", None)),
        ("I need the trading agent", SwitchRequest(None, "trading agent", None)),
        ("I want the money assistant please", SwitchRequest(None, "money assistant", None)),
        ("put me on the trading agent", SwitchRequest(None, "trading agent", None)),
        ("take me to the everyday assistant", SwitchRequest("general", "everyday assistant", None)),
        ("can I speak with the markets bot", SwitchRequest(None, "markets bot", None)),
        ("let me talk with stocks", SwitchRequest("stock-picker", "stocks", None)),
        # A place phrasing with a name or alias needs no agent noun.
        ("move me to stocks", SwitchRequest("stock-picker", "stocks", None)),
        ("put me on the house", SwitchRequest("home", "house", None)),
        # A target that ends in an agent noun keeps a further request apart.
        (
            "connect me with the investing agent, what's Apple at",
            SwitchRequest(None, "investing agent", "what's Apple at"),
        ),
        (
            "switch to the investing agent and check NVDA",
            SwitchRequest(None, "investing agent", "check NVDA"),
        ),
        ("talk to the travel agent about my trip", SwitchRequest(None, "travel agent", "my trip")),
    ],
)
def test_switch_phrasings_and_agent_nouns(text: str, expected: SwitchRequest) -> None:
    assert read_switch_request(text, NAMES) == expected


@pytest.mark.parametrize(
    "text",
    [
        "can I talk to Sarah about the stock picker",
        "connect me with my accountant",
        "find me a travel agent",
        "I need a travel agent",
        "change my profile picture",
        "use a different approach",
        "use the stock picker",  # an agent phrasing always needs an agent noun
        "use stocks agent",  # and "the" or "my"
        "call my agent",
        "go to the gym",
        "put me on hold",
        "take me to the airport",
        "give me the weather",
        "connect me to the trading desk",
        "I need the latest report from my real estate agent",  # too long for a target
        "use my agent to book a flight",
        "help me pick a different agent for my house sale",
        "remind me to use the stock agent",  # mid-sentence, only an exact name counts
    ],
)
def test_wordings_that_only_sound_like_a_switch_are_none(text: str) -> None:
    assert read_switch_request(text, NAMES) is None


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("go to the stock trader profile", TargetWords("stock trader", True)),
        ("Can I talk to my portfolio agent, please?", TargetWords("portfolio", True)),
        ("connect me with the agent for reminders", TargetWords("for reminders", True)),
        ("my stock broker", TargetWords("stock broker", False)),
        ("the stock trader", TargetWords("stock trader", False)),
        # Ends where the parser ends a target: at a connector, "about", or a sentence break.
        ("connect me with the investing agent, what's Apple at", TargetWords("investing", True)),
        ("talk to Sarah about the stock picker", TargetWords("sarah", False)),
        ("switch to the money agent and check NVDA", TargetWords("money", True)),
        ("I want to switch to the stock agent", TargetWords("stock", True)),
        ("", TargetWords("", False)),
    ],
)
def test_target_words_drop_the_asking(text: str, words: TargetWords) -> None:
    assert target_words(text) == words
