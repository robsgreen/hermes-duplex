"""Recognise a spoken request to switch profiles, and a short yes or no to a switch question.

Pure text helpers with no I/O and no state. Every match ignores case, punctuation, and extra
whitespace: text is compared word by word, a hyphen counts as a space ("stock-picker" and "stock
picker" are the same words), and apostrophes are dropped ("don't" reads as "dont").
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, NamedTuple

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
"""One word, apostrophes inside it included ("don't")."""
_APOSTROPHES = re.compile(r"['’]")
_SENTENCE_BREAK = re.compile(r"[.,;:!?]")

_SWITCH_PHRASES = frozenset({("switch", "to"), ("talk", "to"), ("back", "to"), ("change", "to")})
# "talk to me about the weather" is a request for the voice model, not a switch.
_NOT_A_TARGET = frozenset({"me", "us", "you"})
_ARTICLE = "the"

# What joins a further request to the switch ("switch to stocks and then check NVDA"), and the words
# that alone make no request ("switch to stocks, thanks").
_CONNECTORS = frozenset({"and", "then", "so"})
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
    "then", "so", or a sentence break and holds more than polite words: "switch to stocks and check
    NVDA" has the rest "check NVDA". None otherwise: "switch to stocks", "switch to stocks, thanks",
    and "switch to the stocks profile" have no further request."""


def read_switch_request(text: str, names: Mapping[str, Sequence[str]]) -> SwitchRequest | None:
    """Read a spoken switch request ("let me talk to the stock picker") out of hand-off text, with any
    further request said after it.

    ``names`` maps each profile name to its spoken aliases. A switch request is a switch phrase
    ("switch to", "talk to", "back to", "change to") followed by a target, anywhere in the text; a
    leading "the" before the target is ignored. The target resolves to an exact profile name first and
    to an alias only when no name fits; among several fits the longest one wins. Words after the
    target are the ``rest`` (see ``SwitchRequest``) or are ignored ("switch to general please").

    The target is ``"ambiguous"`` when the longest fitting alias belongs to two profiles, or when it
    fits no name or alias ("switch to finance"). Returns None when the text holds no switch phrase with
    a target. A phrase followed by "me", "us", or "you" ("talk to me about Tesla") has no target. When
    the text holds several switch phrases, the first one that names a profile wins.
    """
    spoken_names = {profile: [_target_words(profile)] for profile in names}
    spoken_aliases = {
        profile: [_target_words(alias) for alias in aliases] for profile, aliases in names.items()
    }
    spans = list(_WORD.finditer(text))
    words = [_spoken(span.group()) for span in spans]
    result: SwitchRequest | None = None

    for start in range(len(words) - 1):
        if (words[start], words[start + 1]) not in _SWITCH_PHRASES:
            continue
        target_start = start + 2
        if words[target_start : target_start + 1] == [_ARTICLE]:
            target_start += 1
        target = words[target_start:]
        if not target or target[0] in _NOT_A_TARGET:
            continue

        matches, length = _longest_matches(target, spoken_names)
        if not matches:
            matches, length = _longest_matches(target, spoken_aliases)
        if len(matches) == 1:
            return SwitchRequest(matches.pop(), _rest(text, spans, words, target_start + length))
        result = SwitchRequest("ambiguous", None)

    return result


def parse_switch_request(text: str, names: Mapping[str, Sequence[str]]) -> str | Literal["ambiguous"] | None:
    """The target of the switch request in ``text`` (``read_switch_request``), or None when it holds
    none; any further request is ignored."""
    request = read_switch_request(text, names)
    return None if request is None else request.target


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


def _rest(text: str, spans: Sequence[re.Match[str]], words: Sequence[str], after: int) -> str | None:
    """The further request in ``text`` from word ``after`` on (``SwitchRequest.rest``), or None.

    ``spans`` are the words of ``text`` as matched there, and ``words`` the same words as compared.
    """
    if not 0 < after < len(words):
        return None
    joined = _SENTENCE_BREAK.search(text, spans[after - 1].end(), spans[after].start()) is not None
    first = after
    while first < len(words) and words[first] in _CONNECTORS:
        first += 1
    joined = joined or first > after
    if not joined or all(word in _POLITE_WORDS for word in words[first:]):
        return None
    return text[spans[first].start() :].strip()


def _target_words(name: str) -> tuple[str, ...]:
    """The words of a profile name or alias as a target is compared with them, leading "the" dropped."""
    words = _words(name)
    if words[:1] == [_ARTICLE]:
        words = words[1:]
    return tuple(words)


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
