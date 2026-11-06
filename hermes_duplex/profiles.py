"""Each person's active profile: the Hermes profile their requests run on until they switch.

The store resolves a default for everyone from config, remembers explicit switches in
``$HERMES_HOME/duplex/active_profiles.json`` across conversations and restarts, and tells the
conversations listening for a person when that person switches.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NamedTuple, Protocol

from .config import Config
from .people import PermissionChecker
from .switching import resolve_profile

logger = logging.getLogger(__name__)

SwitchOutcome = Literal["switched", "already_active", "refused", "unknown"]
"""``unknown``: no profile by that name in config. ``refused``: the person may not use it."""

SwitchListener = Callable[[str], Awaitable[bool]]
"""Called with the person's new active profile after they switch; returns whether a live voice
conversation now uses it (False, for example, for a conversation the person has left, or for a page
that only shows the profile)."""


class ProfileResolver(Protocol):
    """Resolves a person's words for an agent to a profile beyond its exact name or alias
    (``router.knn.ProfileMatcher``, which matches by meaning)."""

    async def resolve(self, words: str) -> str | None:
        """The profile ``words`` name, exactly or otherwise; None when they name no single profile."""
        ...


class SwitchResult(NamedTuple):
    """What ``ActiveProfiles.switch`` did."""

    outcome: SwitchOutcome

    notified_live: bool
    """Whether a listener for the person reported that a live voice conversation now uses the new
    profile; False means the switch only takes effect from the person's next conversation."""


class ActiveProfiles:
    """One active profile per person, persisted to ``path`` and announced to subscribed listeners.

    A person's default profile is ``people.<p>.default_profile``, else ``router.default_profile`` when
    the person may use it, else the first profile in the person's ``profiles`` list. Every method takes
    a person's name from config's ``people`` table; ``get`` raises ``KeyError`` for any other name.

    Methods are meant to be called from one event loop thread. A person's switches run one at a time,
    listeners included, so every listener hears them in the order they were made.
    """

    def __init__(
        self,
        path: Path,
        permissions: PermissionChecker,
        config: Config,
        *,
        resolver: ProfileResolver | None = None,
    ) -> None:
        """``path`` is the file switches persist to (normally ``$HERMES_HOME/duplex/active_profiles.json``);
        nothing is read until ``load``, so until then everyone is on their default. ``resolver``
        resolves words that name no profile exactly (``resolve``); None resolves only exact names
        and aliases."""
        self._path = path
        self._resolver = resolver
        self._permissions = permissions
        self._profiles = frozenset(config.profiles)
        self.names: Mapping[str, Sequence[str]] = MappingProxyType(
            {name: profile.aliases for name, profile in config.profiles.items()}
        )
        """Every configured profile, in config order, to the spoken aliases a switch may name it by."""
        self._defaults = {name: _default_profile(config, name, permissions) for name in config.people}
        self._saved: dict[str, str] = {}
        self._fallbacks: dict[str, str] = {}
        self._listeners: dict[str, list[SwitchListener]] = {}
        self._switching: dict[str, asyncio.Lock] = {}
        """Per person, held while one of their switches runs."""

    def load(self) -> None:
        """Replace the saved profiles with those in the file.

        Never raises: a missing file (nobody has switched yet) is normal, and an unreadable or corrupt
        one is logged as an error; either leaves everyone on their default until the next switch
        writes the file. A saved profile that config no longer defines, or that the person may no
        longer use, is dropped, and the person's default becomes their ``fallback_pending``. Entries
        for people no longer in config are dropped. When anything was dropped, the file is rewritten
        without it, so the fallback is announced once, not after every restart, and a profile added
        back to config later does not return unasked.
        """
        self._saved = {}
        self._fallbacks = {}
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.info("no %s yet, so everyone starts on their default profile", self._path)
            return
        except (OSError, ValueError) as error:  # UnicodeDecodeError is a ValueError
            logger.error(
                "could not read %s, so everyone starts on their default profile: %r", self._path, error
            )
            return
        if not isinstance(saved, dict) or not all(isinstance(value, str) for value in saved.values()):
            logger.error(
                "%s is not a map of person to profile, so everyone starts on their default profile",
                self._path,
            )
            return

        for person, profile in saved.items():
            if person not in self._defaults:
                continue
            if self._may_use(person, profile):
                self._saved[person] = profile
            else:
                default = self._defaults[person]
                logger.warning(
                    "%s's saved profile '%s' is no longer usable; back to '%s'", person, profile, default
                )
                self._fallbacks[person] = default
        if self._saved != saved:
            self._save()

    def get(self, person: str) -> str:
        """The profile ``person``'s requests run on: their last switch, else their default."""
        return self._saved.get(person) or self._defaults[person]

    def permitted(self, person: str) -> list[str]:
        """The profiles ``person`` may switch to, in config order; empty for anyone not in config."""
        return [profile for profile in self.names if self._may_use(person, profile)]

    async def resolve(self, words: str) -> str | None:
        """The profile ``words`` name: matched exactly as a spoken switch request's target is
        (``switching.resolve_profile``: a profile name first, then an alias), else by the
        ``resolver`` ("the stock trader" by meaning); None when they name no single profile. Every way
        to switch resolves its words here. A resolver that fails is logged and leaves only the exact
        match."""
        if self._resolver is None:
            return resolve_profile(words, self.names)
        try:
            return await self._resolver.resolve(words)
        except Exception:
            logger.exception("could not resolve %r by meaning; only exact names count", words)
            return resolve_profile(words, self.names)

    def fallback_pending(self, person: str) -> str | None:
        """The default ``person`` was moved back to by ``load`` because their saved profile became
        unusable, returned once so it can be announced; None on every later call and for everyone else.
        """
        return self._fallbacks.pop(person, None)

    async def switch(self, person: str, profile: str) -> SwitchResult:
        """Make ``profile`` ``person``'s active profile, save it, and await every listener for them.

        Changes nothing for an ``unknown`` profile, one the person may not use (``refused``, also the
        answer for a person not in config), or the one already active (``already_active``). On
        ``switched``, a file that cannot be written is logged as an error and the switch still holds
        until restart; a listener that raises is logged, counts as not live, and does not stop the
        others. Waits for any switch of ``person``'s still running, so the last one made wins
        everywhere.
        """
        if profile not in self._profiles:
            return SwitchResult("unknown", notified_live=False)
        if person not in self._defaults or not self._may_use(person, profile):
            return SwitchResult("refused", notified_live=False)

        async with self._switching.setdefault(person, asyncio.Lock()):
            if profile == self.get(person):
                return SwitchResult("already_active", notified_live=False)
            self._saved[person] = profile
            self._fallbacks.pop(person, None)
            self._save()
            notified_live = False
            for listener in list(self._listeners.get(person, ())):
                try:
                    notified_live = await listener(profile) or notified_live
                except Exception:
                    logger.exception("a listener for %s's switch to '%s' failed", person, profile)
            return SwitchResult("switched", notified_live=notified_live)

    def subscribe(self, person: str, listener: SwitchListener) -> Callable[[], None]:
        """Call ``listener`` with ``person``'s new profile on each of their switches, until the returned
        function is called. A person may have several listeners; unsubscribing twice is harmless."""
        listeners = self._listeners.setdefault(person, [])
        listeners.append(listener)

        def unsubscribe() -> None:
            if listener in listeners:
                listeners.remove(listener)

        return unsubscribe

    def _may_use(self, person: str, profile: str) -> bool:
        entry = self._permissions.by_name(person)
        return entry is not None and profile in self._profiles and self._permissions.allowed(entry, profile)

    def _save(self) -> None:
        """Write the saved profiles atomically: a reader sees the old file or the new one, never part."""
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self._saved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as error:
            logger.error(
                "could not save active profiles to %s; they last until restart: %r", self._path, error
            )


def _default_profile(config: Config, person: str, permissions: PermissionChecker) -> str:
    entry = config.people[person]
    if entry.default_profile is not None:
        return entry.default_profile
    allowed = permissions.by_name(person)
    if allowed is not None and permissions.allowed(allowed, config.router.default_profile):
        return config.router.default_profile
    # Config rejects an empty list, and "*" grants the router default, so this is a profile name.
    return entry.profiles[0]
