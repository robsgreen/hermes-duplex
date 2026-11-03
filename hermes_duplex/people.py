"""Who may talk to the service, how each transport identifies them, and which profiles they may use."""

import hmac
from dataclasses import dataclass

from .config import ALL_PROFILES, Config, ConfigError


@dataclass(frozen=True)
class Person:
    """One person from config's ``people`` table.

    ``profiles`` is the set of profiles the person may use, with ``"*"`` already expanded to every
    profile defined in config.
    """

    name: str
    discord_id: int | None
    profiles: frozenset[str]


class PermissionChecker:
    """Maps Discord user ids and web tokens to people, and answers whether a person may use a profile.

    Each Discord id and each web token belongs to exactly one person; a duplicate is a ``ConfigError``
    naming the second person's key.
    """

    def __init__(self, config: Config) -> None:
        self._by_name: dict[str, Person] = {}
        self._by_discord_id: dict[int, Person] = {}
        self._tokens: list[tuple[bytes, Person]] = []
        defined_profiles = frozenset(config.profiles)

        for name, entry in config.people.items():
            profiles = defined_profiles if ALL_PROFILES in entry.profiles else frozenset(entry.profiles)
            person = Person(name=name, discord_id=entry.discord_id, profiles=profiles)
            self._by_name[name] = person

            if entry.discord_id is not None:
                if (owner := self._by_discord_id.get(entry.discord_id)) is not None:
                    raise ConfigError(
                        f"people.{name}.discord_id", f"is already used by person '{owner.name}'"
                    )
                self._by_discord_id[entry.discord_id] = person

            if entry.web_token_env is not None:
                token = config.secrets.get(entry.web_token_env).encode()
                if (owner := self._match_token(token)) is not None:
                    raise ConfigError(
                        f"people.{name}.web_token_env", f"names the same token as person '{owner.name}'"
                    )
                self._tokens.append((token, person))

    @property
    def allowlisted_discord_ids(self) -> frozenset[int]:
        """Discord user ids of every person who has one; audio and commands from others are dropped."""
        return frozenset(self._by_discord_id)

    def by_name(self, name: str) -> Person | None:
        """The person ``name`` from config's ``people`` table, or None for anyone not in config."""
        return self._by_name.get(name)

    def by_discord_id(self, discord_id: int) -> Person | None:
        """The person with this Discord user id, or None for anyone not in config."""
        return self._by_discord_id.get(discord_id)

    def by_web_token(self, token: str) -> Person | None:
        """The person whose web token equals ``token``, or None.

        Compares against every token in constant time per token, so timing reveals neither a match
        nor how much of a token matched.
        """
        return self._match_token(token.encode())

    def allowed(self, person: Person, profile: str) -> bool:
        """Whether ``person`` may run tasks on ``profile``."""
        return profile in person.profiles

    def _match_token(self, token: bytes) -> Person | None:
        match = None
        for candidate, person in self._tokens:
            if hmac.compare_digest(candidate, token):
                match = person
        return match
