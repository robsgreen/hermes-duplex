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
_SENTENCE_END = re.compile(r"[.;!?]")


class _Phrasing(NamedTuple):
    """One way of asking to switch: its words, and what may follow them as the target."""

    words: tuple[str, ...]
    kind: Literal["switch", "place", "named", "agent"]
    """What the words after it must be to make a target:

    - ``"switch"``: said mostly of agents, so a profile's name or alias, words that end in an agent
      noun ("talk to the investing agent"), or, when the phrasing and polite words are all that is
      said, any few words ("switch to stock pickers"), whose meaning then decides
      (``profiles.resolve_switch_request``).
    - ``"place"``: also said of places, topics, and things ("go to the gym", "back to Tesla", "put me
      on hold"), so a profile's name or alias, or words that end in an agent noun ("go to the stock
      trader profile").
    - ``"named"``: said of things too ("I'd like the salmon"), so the target must start with "the" or
      "my" and be a profile's name or alias ("I'd like the coder") or end in an agent noun ("hand me
      over to the news agent").
    - ``"agent"``: said of anything ("use a different approach", "give me the weather", "get me the
      news"), so every target, a profile's name included, must start with "the" or "my" and end in an
      agent noun ("use my portfolio agent", "connect me with the stock picker agent").
    """


_PHRASINGS: tuple[_Phrasing, ...] = (
    _Phrasing(("switch", "me", "over", "to"), "switch"),
    _Phrasing(("switch", "me", "to"), "switch"),
    _Phrasing(("switch", "over", "to"), "switch"),
    _Phrasing(("switch", "to"), "switch"),
    _Phrasing(("change", "me", "to"), "switch"),
    _Phrasing(("change", "to"), "switch"),
    _Phrasing(("swap", "to"), "switch"),
    _Phrasing(("talk", "to"), "switch"),
    _Phrasing(("talk", "with"), "switch"),
    _Phrasing(("speak", "to"), "switch"),
    _Phrasing(("speak", "with"), "switch"),
    _Phrasing(("back", "to"), "place"),
    _Phrasing(("go", "to"), "place"),
    _Phrasing(("move", "me", "to"), "place"),
    _Phrasing(("take", "me", "to"), "place"),
    _Phrasing(("put", "me", "through", "to"), "place"),
    _Phrasing(("put", "me", "on"), "place"),
    _Phrasing(("id", "like"), "named"),
    _Phrasing(("hand", "me", "over", "to"), "named"),
    _Phrasing(("use",), "agent"),
    _Phrasing(("connect", "me", "with"), "agent"),
    _Phrasing(("connect", "me", "to"), "agent"),
    _Phrasing(("give", "me"), "agent"),
    _Phrasing(("get", "me"), "agent"),
    _Phrasing(("bring", "up"), "agent"),
    _Phrasing(("can", "i", "have"), "agent"),
    _Phrasing(("i", "need"), "agent"),
    _Phrasing(("i", "want"), "agent"),
)
"""Every way of asking to switch the parser reads, longest first wherever two start alike. The
switch-request parser (``read_switch_request``) and the words a target is resolved from
(``target_words``) both read them from here."""

_LONGEST_PHRASING = max(len(phrasing.words) for phrasing in _PHRASINGS)
_AGENT_NOUNS = frozenset({"agent", "profile", "assistant", "bot"})
"""Words that say the target is an agent ("the stock trader profile"). They may follow a profile's
name ("switch to the general profile"), end a target that names no profile by name, and let a
target's meaning be matched against what each profile is for (``router.knn.ProfileMatcher``)."""
_NAME_NOUNS = _AGENT_NOUNS | {"one"}
"""Words that may follow a profile's name or alias in a target ("the general profile", "the news
one"). "one" counts only there: "give me the latest one" says nothing of an agent."""
NOT_A_TARGET = frozenset({"me", "us", "you"})
"""Words that, said right after a switch phrase, make it no switch request: "talk to me about the
weather" is a request for the voice model."""
_ARTICLE = "the"
_AGENT_DETERMINERS = frozenset({_ARTICLE, "my"})
"""What a ``"named"`` or ``"agent"`` phrasing's target starts with ("use my portfolio agent"); a
target that starts with "a" ("use a different approach", "I need a travel agent") is no switch."""

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
"""A target that names no profile by name or alias counts only when it holds at most this many words
besides "the", "my", polite words, and an agent noun ("switch to finance", "connect me with the stock
research assistant"); a longer one ("talk to Sarah about the party tomorrow night", "I need the
latest report from my real estate agent") is an ordinary request."""

# What joins a further request to the switch ("switch to stocks and then check NVDA"), and the words
# that alone make no request ("switch to stocks, thanks").
_CONNECTORS = frozenset({"and", "then", "so"})
_TOPIC_CONNECTOR = "about"
"""Joins a request to a switch that opens what is said: "talk to stocks about Tesla"."""
_POLITE_WORDS = frozenset({"please", "thanks", "thank", "you", "now", "ok", "okay"})
_TARGET_ENDS = _CONNECTORS | {_TOPIC_CONNECTOR}
"""Words a target never runs past: what follows them is a further request or a topic."""
_NOT_TOLD = _AGENT_DETERMINERS | {"a", "an", "other", "another", "different"} | _POLITE_WORDS | _AGENT_NOUNS
"""Words of a target that do not say which agent it is ("switch to another agent")."""
_UNCOUNTED = _AGENT_DETERMINERS | _POLITE_WORDS | _AGENT_NOUNS
"""Words a target may hold besides ``UNKNOWN_TARGET_MAX_WORDS`` others ("a" counts: "switch to a
cheaper phone plan" is too long)."""

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

    target: str | Literal["ambiguous"] | None
    """The profile ``words`` name by its name or one of its aliases; ``"ambiguous"`` when the longest
    name or alias they fit belongs to two profiles; None when they fit no name or alias, so only their
    meaning can tell which agent they ask for (``router.knn.ProfileMatcher``)."""

    words: str
    """The words said for the agent, as spoken, without a leading "the": "stock picker", "my portfolio
    agent", "finance"."""

    rest: str | None
    """The further request said after the target, as spoken, when it is joined to the target by "and",
    "then", "so", a sentence break, or, for a switch that opens what is said, "about", and holds more
    than polite words: "switch to stocks and check NVDA" has the rest "check NVDA", "talk to stocks
    about Tesla" the rest "Tesla". None otherwise: "switch to stocks", "switch to stocks, thanks", and
    "switch to the stocks profile" have no further request."""


class TargetWords(NamedTuple):
    """What a person's words for an agent say about it, as ``target_words`` reads them."""

    words: str
    """The words that say which agent, as compared (``spoken_form`` per word), space-separated:
    "stock trader" for "go to the stock trader profile"; empty when nothing else is said."""

    names_an_agent: bool
    """Whether they call it an agent, profile, assistant, or bot ("my portfolio agent")."""


def read_switch_request(text: str, names: Mapping[str, Sequence[str]]) -> SwitchRequest | None:
    """Read a spoken switch request ("let me talk to the stock picker") out of hand-off text, with any
    further request said after it.

    ``names`` maps each profile name to its spoken aliases. A switch request is a switch phrasing
    (``_PHRASINGS``: "switch to", "switch me over to", "talk to", "back to", "go to", "put me through
    to", "I'd like", "use", "connect me with", "get me", ...) followed by a target; a leading "the"
    before the target is ignored. Where the phrasing stands decides what the target may be:

    - When it opens what is said or one of its sentences (after ".", ";", "!" or "?", not a comma), after
      lead-ins such as "let me", "can we", "I need to", "I'd rather", "please", "go", "hmm", or "ok",
      one after another or none, the target is a profile's name or one of its aliases
      (``resolve_profile``).
    - When it opens all that is said (only lead-ins before it), the target may also be words that fit
      no name or alias (``SwitchRequest.target`` None), if they hold at most
      ``UNKNOWN_TARGET_MAX_WORDS`` words and either end in an agent noun ("connect me with the
      investing agent, what's Apple at", which has a further request) or, for a ``"switch"`` phrasing,
      are all that is said but polite words ("switch to finance"); whether such words switch at
      all is ``profiles.resolve_switch_request``'s rule. Said otherwise ("Switch to Celsius. What's
      the weather in Paris?", "switch to finance and check NVDA", "back to Tesla, what's it at?", "go
      to the gym"), such words make no switch request.
    - Anywhere else, only a profile's exact name is a target: "remind me to talk to Sarah" and "should
      I move my savings back to stocks" make no request, even with an alias "stocks".

    An ``"agent"`` phrasing ("use", "connect me with", "give me", "get me", "can I have", ...) counts
    only with a target that starts with "the" or "my" and ends in an agent noun ("use my portfolio
    agent"; not "use a different approach" or "connect me with my accountant"); a ``"named"`` one ("I'd
    like", "hand me over to") also with "the" or "my" and a profile's name or alias ("I'd like the
    coder"). A phrasing whose sentence holds a negation before it (a lone "no," interjection aside)
    ("don't switch to stocks", "I never want to talk to general") makes no request, nor does one
    followed by "me", "us", or "you" ("talk to me about Tesla"). After the target, the clause may hold
    only polite words, an agent noun (or "one" after a name or alias: "switch me over to the news
    one"), or a joined further request (``SwitchRequest.rest``); anything else ("switch to stocks from
    bonds") makes no request. Returns the first request whose target names a profile, else the first
    other one, else None.
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
    openings = {position for start in (0, *sentence_starts) for position in _lead_in_ends(words, start)}
    opens_all = set(_lead_in_ends(words, 0))
    result: SwitchRequest | None = None

    for start in range(len(words)):
        phrasing = _phrasing_at(words, start)
        if phrasing is None:
            continue
        sentence_start = max((index for index in sentence_starts if index <= start), default=0)
        if any(
            word in _NEGATIONS and not (word == "no" and index + 1 in breaks)
            for index, word in enumerate(words[sentence_start:start], sentence_start)
        ):
            continue
        opening = start in openings
        target_start = start + len(phrasing.words)
        if words[target_start : target_start + 1] == [_ARTICLE]:
            target_start += 1
        clause_end = min((index for index in breaks if index > target_start), default=len(words))
        target = words[target_start:clause_end]
        if not target or target[0] in NOT_A_TARGET:
            continue

        profile, length = _resolve(target, names, aliases=opening)
        if profile is not None:
            after = target_start + length
            named_an_agent = after < clause_end and words[after] in _NAME_NOUNS
            if phrasing.kind == "agent" and not named_an_agent:
                continue
            if profile == "ambiguous":
                if opening:
                    result = result or SwitchRequest(
                        "ambiguous", _said(text, spans, target_start, after), None
                    )
                continue
            after += named_an_agent
            fits, rest = _after_target(text, spans, words, after, breaks, opening=opening)
            if fits:
                return SwitchRequest(profile, _said(text, spans, target_start, after), rest)
            continue

        if start not in opens_all or result is not None:
            continue
        noun = _agent_noun(words, target_start, clause_end)
        if noun is not None:
            fits, rest = _after_target(text, spans, words, noun + 1, breaks, opening=True)
            if fits and _few_words(words[target_start:noun]):
                result = SwitchRequest(None, _said(text, spans, target_start, noun + 1), rest)
        elif phrasing.kind == "switch" and (end := _only_target_end(words, target_start)) is not None:
            result = SwitchRequest(None, _said(text, spans, target_start, end), None)

    return result


def resolve_profile(words: str, names: Mapping[str, Sequence[str]]) -> str | None:
    """The one profile ``words`` name, as a switch target is resolved: ``names`` maps each profile name
    to its spoken aliases, and ``words`` (compared as ``spoken_form`` gives them) start with a profile
    name or an alias; among several fits the longest one wins, and a name wins a tie with an alias.
    None when they fit no name or alias, or when the longest fit is an alias of two profiles."""
    profile, _ = _resolve(spoken_form(words), names, aliases=True)
    return None if profile == "ambiguous" else profile


def target_words(text: str) -> TargetWords:
    """What ``text``, a person's words for an agent ("the stock trader", "can I talk to my portfolio
    agent, please?"), says about the agent it asks for.

    The words are read as a switch request's target is: after the first switch phrasing, if any
    (``_PHRASINGS``), up to the first connector ("and", "then", "so"), "about", or sentence break, so
    a further request is never part of them ("connect me with the investing agent, what's Apple at" is
    "investing"). "the", "my", "a", "an", polite words, and agent nouns are left out of
    ``TargetWords.words``; an agent noun among them sets ``TargetWords.names_an_agent``.
    """
    spans = list(_WORD.finditer(text))
    words = [_spoken(span.group()) for span in spans]
    begin = next(
        (
            start + len(phrasing.words)
            for start in range(len(words))
            if (phrasing := _phrasing_at(words, start))
        ),
        0,
    )
    end = begin
    while end < len(words) and words[end] not in _TARGET_ENDS:
        end += 1
        if end < len(words) and _SENTENCE_BREAK.search(text, spans[end - 1].end(), spans[end].start()):
            break
    span = words[begin:end]
    told = [word for word in span if word not in _NOT_TOLD]
    return TargetWords(" ".join(told), any(word in _AGENT_NOUNS for word in span))


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


def _lead_in_ends(words: Sequence[str], start: int) -> list[int]:
    """``start`` and the index after each lead-in (``_LEAD_INS``) that follows from there, one after
    another: where a switch phrase may stand and still open what is said. A phrase may start with a
    word that is also a lead-in ("go to stocks", where "go" also leads in "go back to general")."""
    positions = [start]
    while True:
        position = positions[-1]
        fits = (tuple(words[position : position + n]) for n in range(_LONGEST_LEAD_IN, 0, -1))
        lead_in = next((candidate for candidate in fits if candidate in _LEAD_INS), None)
        if lead_in is None:
            return positions
        positions.append(position + len(lead_in))


def _phrasing_at(words: Sequence[str], start: int) -> _Phrasing | None:
    """The longest switch phrasing (``_PHRASINGS``) that ``words`` hold at index ``start``, or None; a
    ``"named"`` or ``"agent"`` phrasing counts only when "the" or "my" follows it."""
    for length in range(_LONGEST_PHRASING, 0, -1):
        said = tuple(words[start : start + length])
        phrasing = next((phrasing for phrasing in _PHRASINGS if phrasing.words == said), None)
        if phrasing is None:
            continue
        follows = start + length
        if phrasing.kind in ("named", "agent") and (
            follows >= len(words) or words[follows] not in _AGENT_DETERMINERS
        ):
            return None
        return phrasing
    return None


def _agent_noun(words: Sequence[str], target_start: int, clause_end: int) -> int | None:
    """The index of the first agent noun in the target from ``target_start``, which ends at
    ``clause_end`` or at the first connector or "about" before it; None when it holds none."""
    for index in range(target_start, clause_end):
        if words[index] in _TARGET_ENDS:
            return None
        if words[index] in _AGENT_NOUNS:
            return index
    return None


def _few_words(target: Sequence[str]) -> bool:
    """Whether ``target`` holds at most ``UNKNOWN_TARGET_MAX_WORDS`` words that say which agent."""
    return sum(word not in _UNCOUNTED for word in target) <= UNKNOWN_TARGET_MAX_WORDS


def _only_target_end(words: Sequence[str], target_start: int) -> int | None:
    """Where the target from ``target_start`` ends when it and polite words are all that ``words`` say
    from there on, connectors aside, and it holds few words (``_few_words``); None when more is said.
    The target ends at the first connector or "about" ("talk to Mom about dinner" is more than a
    switch), and polite words after it are not part of it."""
    end = target_start
    while end < len(words) and words[end] not in _TARGET_ENDS:
        end += 1
    if not all(word in _POLITE_WORDS | _CONNECTORS for word in words[end:]) or not _few_words(
        words[target_start:end]
    ):
        return None
    while end > target_start and words[end - 1] in _POLITE_WORDS:
        end -= 1
    return end if end > target_start else None


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


def _said(text: str, spans: Sequence[re.Match[str]], first: int, end: int) -> str:
    """``text`` from word ``first`` up to word ``end`` (exclusive), as spoken; ``spans`` are its words."""
    return text[spans[first].start() : spans[end - 1].end()]


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
