"""Recognise a spoken request to switch profiles, and a short yes or no to a switch question.

Pure text helpers with no I/O and no state. Every match ignores case, punctuation, and extra
whitespace: text is compared word by word, a hyphen counts as a space ("stock-picker" and "stock
picker" are the same words), and apostrophes are dropped ("don't" reads as "dont").
"""

import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, NamedTuple

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
"""One word, apostrophes inside it included ("don't")."""
_APOSTROPHES = re.compile(r"['’]")
_SENTENCE_BREAK = re.compile(r"[.,;:!?]")
_SENTENCE_END = re.compile(r"[.;!?]")

_RETURN_PHRASE = ("back", "to")
"""The switch phrase people also use to return to a topic ("back to Tesla"), so with a target that is no
profile it never asks which one is meant."""
_SWITCH_PHRASES = frozenset({("switch", "to"), ("talk", "to"), _RETURN_PHRASE, ("change", "to")})
NOT_A_TARGET = frozenset({"me", "us", "you"})
"""Words that, said right after a switch phrase, make it no switch request: "talk to me about the
weather" is a request for the voice model."""
_ARTICLE = "the"
_TARGET_NOUNS = frozenset({"profile", "agent"})
"""Words that may follow a target without making a request: "switch to the general profile"."""

# What a person may say before a switch phrase that opens what they say, or one of its sentences ("let
# me talk to stocks", "hmm, ok, go back to general"); lead-ins may follow one another. A phrase
# anywhere else is a switch only when it names a profile exactly.
_LEAD_INS = frozenset(
    {
        ("let", "me"),
        ("lets",),
        ("can", "i"),
        ("could", "i"),
        ("may", "i"),
        ("can", "we"),
        ("could", "we"),
        ("can", "you"),
        ("could", "you"),
        ("would", "you"),
        ("will", "you"),
        ("i", "want", "to"),
        ("i", "wanna"),
        ("i", "need", "to"),
        ("id", "like", "to"),
        ("i", "would", "like", "to"),
        ("id", "love", "to"),
        ("i", "would", "love", "to"),
        ("id", "rather"),
        ("i", "would", "rather"),
        ("please",),
        ("go",),
        ("ok",),
        ("okay",),
        ("alright",),
        ("right",),
        ("well",),
        ("now",),
        ("so",),
        ("just",),
        ("actually",),
        ("hey",),
        ("no",),
        ("yes",),
        ("yeah",),
        ("sure",),
        ("oh",),
        ("hmm",),
        ("um",),
        ("uh",),
        ("sorry",),
        ("yes",),
        ("yeah",),
        ("thanks",),
        ("thank", "you"),
    }
)
_LONGEST_LEAD_IN = max(len(lead_in) for lead_in in _LEAD_INS)
_NEGATIONS = frozenset({"dont", "not", "never", "no", "wont", "cant", "cannot", "shouldnt"})
"""Words that, earlier in a switch phrase's clause, make it no request: "don't switch to stocks"."""

UNKNOWN_TARGET_MAX_WORDS = 3
"""A switch phrase that names no profile asks which one is meant only when it is all that is said and
its target holds at most this many words besides polite ones ("switch to finance", "let me talk to the
finance agent"); a longer one ("talk to Sarah about the party tomorrow night") is an ordinary request."""

# What joins a further request to the switch ("switch to stocks and then check NVDA"), and the words
# that alone make no request ("switch to stocks, thanks").
_CONNECTORS = frozenset({"and", "then", "so"})
_TOPIC_CONNECTOR = "about"
"""Joins a request to a switch that opens what is said: "talk to stocks about Tesla"."""
_POLITE_WORDS = frozenset({"please", "thanks", "thank", "you", "now", "ok", "okay"})

# Each answer is a run of its phrases and polite words and nothing else, holding at least one phrase:
# "yes, please do" is a yes, "no thanks" a no, "thanks" neither, and "yes, and what's Tesla at" is not
# an answer at all.
_POLITE_PHRASES = frozenset({("please",), ("thanks",), ("thank", "you")})
_AFFIRMATIVE_PHRASES = frozenset(
    {
        ("yes",),
        ("yeah",),
        ("yep",),
        ("yup",),
        ("sure",),
        ("ok",),
        ("okay",),
        ("switch",),
        ("do", "it"),
        ("do", "that"),
        ("please", "do"),
        ("go", "ahead"),
        ("sure", "thing"),
    }
)
_NEGATIVE_PHRASES = frozenset(
    {
        ("no",),
        ("nope",),
        ("nah",),
        ("dont",),
        ("dont", "switch"),
        ("dont", "do", "it"),
        ("not", "now"),
        ("no", "need"),
        ("stay",),
        ("stay", "here"),
    }
)


class SwitchRequest(NamedTuple):
    """A switch request read out of hand-off text by ``read_switch_request``."""

    target: str | Literal["ambiguous"]
    """The profile name, or ``"ambiguous"`` when the words name no single profile."""

    rest: str | None
    """The further request said after the target, as spoken, when it is joined to the target by "and",
    "then", "so", a sentence break, or, for a switch that opens what is said, "about", and holds more
    than polite words: "switch to stocks and check NVDA" has the rest "check NVDA", "talk to stocks
    about Tesla" the rest "Tesla". None otherwise: "switch to stocks", "switch to stocks, thanks", and
    "switch to the stocks profile" have no further request."""


def read_switch_request(text: str, names: Mapping[str, Sequence[str]]) -> SwitchRequest | None:
    """Read a spoken switch request ("let me talk to the stock picker") out of hand-off text, with any
    further request said after it.

    ``names`` maps each profile name to its spoken aliases. A switch request is a switch phrase
    ("switch to", "talk to", "back to", "change to") followed by a target; a leading "the" before the
    target is ignored. Where the phrase stands decides what the target may be:

    - When it opens what is said or one of its sentences (after ".", ";", "!" or "?", not a comma), after
      lead-ins such as "let me", "can we", "I need to", "I'd rather", "please", "go", "hmm", or "ok",
      one after another or none, the target is a profile's name or one of its aliases
      (``resolve_profile``). A target that fits neither is ``"ambiguous"`` ("switch to finance") only
      when the phrase is all that is said: lead-ins before it, a short target
      (``UNKNOWN_TARGET_MAX_WORDS``), and nothing after it but polite words. Said with anything else
      ("Switch to Celsius. What's the weather in Paris?", "switch to finance and check NVDA"), or with
      "back to" ("back to Tesla, what's it at?"), it makes no switch request.
    - Anywhere else, only a profile's exact name is a target: "remind me to talk to Sarah" and "should
      I move my savings back to stocks" make no request, even with an alias "stocks".

    A phrase whose sentence holds a negation before it (a lone "no," interjection aside) ("don't switch to stocks", "I never want to talk
    to general") makes no request, nor does one followed by "me", "us", or "you" ("talk to me about
    Tesla"). After the target, the clause may hold only polite words, "profile" or "agent", or a joined
    further request (``SwitchRequest.rest``); anything else ("switch to stocks from bonds") makes no
    request. Returns the first phrase that names a profile, else ``"ambiguous"`` when one asked which,
    else None.
    """
    spans = list(_WORD.finditer(text))
    words = [_spoken(span.group()) for span in spans]
    breaks = [
        index
        for index in range(1, len(words))
        if _SENTENCE_BREAK.search(text, spans[index - 1].end(), spans[index].start())
    ]
    sentence_starts = [
        index for index in breaks if _SENTENCE_END.search(text, spans[index - 1].end(), spans[index].start())
    ]
    openings = {_after_lead_ins(words, start) for start in (0, *sentence_starts)}
    result: SwitchRequest | None = None

    for start in range(len(words) - 1):
        phrase = (words[start], words[start + 1])
        if phrase not in _SWITCH_PHRASES:
            continue
        sentence_start = max((index for index in sentence_starts if index <= start), default=0)
        if any(
            word in _NEGATIONS and not (word == "no" and index + 1 in breaks)
            for index, word in enumerate(words[sentence_start:start], sentence_start)
        ):
            continue
        opening = start in openings
        target_start = start + 2
        if words[target_start : target_start + 1] == [_ARTICLE]:
            target_start += 1
        clause_end = min((index for index in breaks if index > target_start), default=len(words))
        target = words[target_start:clause_end]
        if not target or target[0] in NOT_A_TARGET:
            continue

        profile, length = _resolve(target, names, aliases=opening)
        if profile is None:
            if phrase != _RETURN_PHRASE and _asks_only_to_switch(words, start, target_start):
                result = result or SwitchRequest("ambiguous", None)
            continue
        if profile == "ambiguous":
            if opening:
                result = result or SwitchRequest("ambiguous", None)
            continue
        fits, rest = _after_target(text, spans, words, target_start + length, breaks, opening=opening)
        if fits:
            return SwitchRequest(profile, rest)

    return result


def resolve_profile(words: str, names: Mapping[str, Sequence[str]]) -> str | None:
    """The one profile ``words`` name, as a switch target is resolved: ``names`` maps each profile name
    to its spoken aliases, and ``words`` (compared as ``spoken_form`` gives them) start with a profile
    name or an alias; among several fits the longest one wins, and a name wins a tie with an alias.
    None when they fit no name or alias, or when the longest fit is an alias of two profiles."""
    profile, _ = _resolve(spoken_form(words), names, aliases=True)
    return None if profile == "ambiguous" else profile


def spoken_form(name: str) -> tuple[str, ...]:
    """The words of a profile name or alias as people say them and a switch target is compared with
    them: casefolded, split at spaces, hyphens, and punctuation, apostrophes dropped, and a leading
    "the" dropped ("The Stock-Picker" is ``("stock", "picker")``). Empty when ``name`` has no words."""
    words = _words(name)
    if words[:1] == [_ARTICLE]:
        words = words[1:]
    return tuple(words)


def is_affirmative(text: str) -> bool:
    """True when ``text`` is only a short yes ("yes", "yeah", "sure", "do it", "please do").

    A longer request that starts with "yes" but carries other content is False.
    """
    return _is_short_answer(text, _AFFIRMATIVE_PHRASES)


def is_negative(text: str) -> bool:
    """True when ``text`` is only a short no ("no", "nope", "no thanks", "don't", "stay here").

    A longer request that starts with "no" but carries other content is False.
    """
    return _is_short_answer(text, _NEGATIVE_PHRASES)


def _words(text: str) -> list[str]:
    return [_spoken(word) for word in _WORD.findall(text)]


def _spoken(word: str) -> str:
    """``word`` as it is compared: casefolded, apostrophes dropped."""
    return _APOSTROPHES.sub("", word.casefold())


def _after_lead_ins(words: Sequence[str], start: int) -> int:
    """The index of the first word from ``start`` on that is not part of a lead-in (``_LEAD_INS``)."""
    position = start
    while True:
        fits = (tuple(words[position : position + n]) for n in range(_LONGEST_LEAD_IN, 0, -1))
        lead_in = next((candidate for candidate in fits if candidate in _LEAD_INS), None)
        if lead_in is None:
            return position
        position += len(lead_in)


def _asks_only_to_switch(words: Sequence[str], start: int, target_start: int) -> bool:
    """Whether the switch phrase at word ``start``, whose target starts at word ``target_start``, is all
    that ``words`` say: only lead-ins before it, a target of at most ``UNKNOWN_TARGET_MAX_WORDS`` words
    besides polite ones, "profile", and "agent", and after the target only polite words and connectors.
    The target ends at the first connector, "about", or sentence break ("talk to Mom about dinner" is
    more than a switch)."""
    if _after_lead_ins(words, 0) != start:
        return False
    ends = _CONNECTORS | {_TOPIC_CONNECTOR}
    target = list(itertools.takewhile(lambda word: word not in ends, words[target_start:]))
    told = [word for word in target if word not in _POLITE_WORDS | _TARGET_NOUNS]
    after = words[target_start + len(target) :]
    return len(told) <= UNKNOWN_TARGET_MAX_WORDS and all(
        word in _POLITE_WORDS | _CONNECTORS for word in after
    )


def _resolve(
    target: Sequence[str], names: Mapping[str, Sequence[str]], *, aliases: bool
) -> tuple[str | Literal["ambiguous"] | None, int]:
    """The profile whose spoken name, or (when ``aliases``) one of whose spoken aliases, ``target`` starts
    with, the longest fit winning and a name winning a tie with an alias, and that fit's length in
    words; ``"ambiguous"`` when the longest fit belongs to two profiles, None (and 0) when nothing fits."""
    matches, length = _longest_matches(target, {profile: [spoken_form(profile)] for profile in names})
    if aliases:
        spoken_aliases = {profile: [spoken_form(alias) for alias in names[profile]] for profile in names}
        alias_matches, alias_length = _longest_matches(target, spoken_aliases)
        if alias_length > length:
            matches, length = alias_matches, alias_length
    if not matches:
        return None, 0
    return (matches.pop() if len(matches) == 1 else "ambiguous"), length


def _after_target(
    text: str,
    spans: Sequence[re.Match[str]],
    words: Sequence[str],
    after: int,
    breaks: Sequence[int],
    *,
    opening: bool,
) -> tuple[bool, str | None]:
    """Whether the words of ``text`` from word ``after`` on, which follow a switch target, leave it a
    switch request, and the further request they make (``SwitchRequest.rest``).

    ``spans`` are the words of ``text`` as matched there, ``words`` the same words as compared, and
    ``breaks`` the indexes of the words a sentence break precedes; ``opening`` says whether the switch
    phrase opens what is said.
    """
    position = after
    if position < len(words) and position not in breaks and words[position] in _TARGET_NOUNS:
        position += 1
    if position < len(words) and position not in breaks:
        if words[position] in _CONNECTORS:
            return True, _request_from(text, spans, words, position)
        if opening and words[position] == _TOPIC_CONNECTOR:
            return True, _request_from(text, spans, words, position + 1)
        clause_end = min((index for index in breaks if index > position), default=len(words))
        if not all(word in _POLITE_WORDS for word in words[position:clause_end]):
            return False, None
        position = clause_end
    return True, _request_from(text, spans, words, position)


def _request_from(text: str, spans: Sequence[re.Match[str]], words: Sequence[str], first: int) -> str | None:
    """The request ``text`` makes from word ``first`` on, connectors before it dropped, as spoken;
    None when it holds only polite words."""
    while first < len(words) and words[first] in _CONNECTORS:
        first += 1
    if all(word in _POLITE_WORDS for word in words[first:]):
        return None
    return text[spans[first].start() :].strip()


def _longest_matches(
    target: Sequence[str], spoken: Mapping[str, Iterable[tuple[str, ...]]]
) -> tuple[set[str], int]:
    """Profiles with a spoken form that ``target`` starts with, keeping only the longest such forms,
    and those forms' length in words (0 when none fits)."""
    best_length = 0
    best: set[str] = set()
    for profile, forms in spoken.items():
        for form in forms:
            if not form or len(form) < best_length or tuple(target[: len(form)]) != form:
                continue
            if len(form) > best_length:
                best_length, best = len(form), set()
            best.add(profile)
    return best, best_length


def _is_short_answer(text: str, phrases: frozenset[tuple[str, ...]]) -> bool:
    """True when ``text`` is ``phrases`` and polite words back to back, with at least one of ``phrases``.

    Each step takes the longest phrase that fits.
    """
    allowed = phrases | _POLITE_PHRASES
    longest = max(len(phrase) for phrase in allowed)
    words = _words(text)
    position = 0
    holds_answer = False
    while position < len(words):
        fits = (tuple(words[position : position + n]) for n in range(longest, 0, -1))
        phrase = next((candidate for candidate in fits if candidate in allowed), None)
        if phrase is None:
            return False
        holds_answer = holds_answer or phrase in phrases
        position += len(phrase)
    return holds_answer
