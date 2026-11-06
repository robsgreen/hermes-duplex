"""Maps a configured provider name to its ``ProviderSpec``: the factory that builds its adapter and,
for the Realtime family, its dialect. The prompter its model reads is the built provider's own
``RealtimeProvider.prompter``.

The provider is always the one ``provider.name`` names; it is never chosen from which API keys
happen to be set.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from ..config import ConfigError, ProviderConfig, Secrets
from ..prompts.base import PromptContext
from .base import RealtimeProvider
from .fake.adapter import FakeProvider
from .gpt_live.adapter import GptLiveProvider
from .realtime.adapter import Dialect, RealtimeApiProvider
from .realtime.openai.dialect import OPENAI
from .realtime.xai.dialect import XAI

type ProviderFactory = Callable[[ProviderConfig, Secrets, PromptContext], RealtimeProvider]
"""Builds one provider session object from the ``provider`` config section, the loaded secrets, and
the deployment its instructions describe."""


@dataclass(frozen=True)
class ProviderSpec:
    """Everything registered for one ``provider.name``."""

    name: str
    """The ``provider.name`` that selects this provider, e.g. ``gpt-live``."""

    factory: ProviderFactory

    dialect: Dialect | None = None
    """The Realtime dialect the factory speaks; None for a provider outside the Realtime family."""


def _realtime(dialect: Dialect) -> ProviderSpec:
    """The spec of the Realtime-family provider that speaks ``dialect``."""
    return ProviderSpec(
        name=dialect.provider_name,
        factory=partial(RealtimeApiProvider.from_config, dialect=dialect),
        dialect=dialect,
    )


PROVIDERS: Mapping[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(name="gpt-live", factory=GptLiveProvider.from_config),
        _realtime(OPENAI),
        _realtime(XAI),
        ProviderSpec(name="fake", factory=FakeProvider.from_config),
    )
}
"""Every implemented provider, by the ``provider.name`` that selects it."""


def provider_spec(name: str) -> ProviderSpec:
    """Return the spec of the provider called ``name``.

    Raises ``ConfigError`` on ``provider.name`` for a name with no registered provider.
    """
    if name not in PROVIDERS:
        allowed = ", ".join(sorted(PROVIDERS))
        raise ConfigError("provider.name", f"unknown provider '{name}' (allowed: {allowed})")
    return PROVIDERS[name]


def provider_factory(name: str) -> ProviderFactory:
    """Return the factory for the provider called ``name``.

    Raises ``ConfigError`` on ``provider.name`` for a name with no registered provider.
    """
    return provider_spec(name).factory
