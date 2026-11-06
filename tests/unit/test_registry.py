"""The provider registry: every configured name resolves, and anything else fails closed."""

import pytest

from hermes_duplex.config import PROVIDER_NAMES, ConfigError, ProviderConfig, Secrets
from hermes_duplex.prompts.base import PromptContext
from hermes_duplex.providers.fake.adapter import FakeProvider
from hermes_duplex.providers.gpt_live.adapter import GptLiveProvider
from hermes_duplex.providers.gpt_live.prompt import GptLivePrompter
from hermes_duplex.providers.realtime.adapter import RealtimeApiProvider
from hermes_duplex.providers.realtime.openai.prompt import OpenAiRealtimePrompter
from hermes_duplex.providers.realtime.xai.dialect import XAI
from hermes_duplex.providers.realtime.xai.prompt import GrokPrompter
from hermes_duplex.providers.registry import provider_factory, provider_spec

CONTEXT = PromptContext(assistant_name="Iris", profiles=(("general", "Anything else"),))


def test_fake_resolves_to_a_factory_that_builds_the_fake_provider() -> None:
    config = ProviderConfig(name="fake", model=None, voice=None, api_key_env=None, turn_detection=None)

    provider = provider_factory("fake")(config, Secrets({}), CONTEXT)

    assert isinstance(provider, FakeProvider)
    assert isinstance(provider.prompter, GptLivePrompter)
    assert provider.prompt_context == CONTEXT


def test_gpt_live_resolves_to_a_factory_that_builds_the_gpt_live_provider() -> None:
    config = ProviderConfig(
        name="gpt-live", model=None, voice="marin", api_key_env="OPENAI_API_KEY", turn_detection=None
    )

    provider = provider_factory("gpt-live")(config, Secrets({"OPENAI_API_KEY": "sk-test"}), CONTEXT)

    assert isinstance(provider, GptLiveProvider)
    assert isinstance(provider.prompter, GptLivePrompter)
    assert provider.prompt_context == CONTEXT


def test_openai_realtime_resolves_to_a_factory_that_builds_the_openai_dialect() -> None:
    config = ProviderConfig(
        name="openai-realtime",
        model=None,
        voice=None,
        api_key_env="OPENAI_API_KEY",
        turn_detection={"type": "server_vad"},
    )

    provider = provider_factory("openai-realtime")(config, Secrets({"OPENAI_API_KEY": "sk-test"}), CONTEXT)

    assert isinstance(provider, RealtimeApiProvider)
    assert provider.name == "openai-realtime"
    assert isinstance(provider.prompter, OpenAiRealtimePrompter)
    assert provider.prompt_context == CONTEXT


def test_xai_realtime_resolves_to_a_factory_that_builds_the_xai_dialect() -> None:
    config = ProviderConfig(
        name="xai-realtime",
        model=None,
        voice="ara",
        api_key_env="XAI_API_KEY",
        turn_detection={"type": "server_vad"},
    )

    provider = provider_factory("xai-realtime")(config, Secrets({"XAI_API_KEY": "xai-test"}), CONTEXT)

    assert isinstance(provider, RealtimeApiProvider)
    assert provider.name == "xai-realtime"
    assert provider.capabilities == XAI.capabilities
    assert isinstance(provider.prompter, GrokPrompter)
    assert provider.prompt_context == CONTEXT
    assert provider_spec("xai-realtime").dialect is XAI


@pytest.mark.parametrize("name", sorted(PROVIDER_NAMES))
def test_every_configurable_provider_has_a_spec(name: str) -> None:
    spec = provider_spec(name)

    assert spec.name == name
    assert provider_factory(name) is spec.factory


@pytest.mark.parametrize("name", ["gpt-4o-realtime", "", "FAKE"])
def test_unknown_provider_name_is_rejected_on_the_name_key(name: str) -> None:
    with pytest.raises(ConfigError) as caught:
        provider_factory(name)

    assert caught.value.key == "provider.name"
