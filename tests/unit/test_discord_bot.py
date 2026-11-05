"""DiscordTransport: who may bring the bot in and out, the commands, profile switches, voice state
events, start and stop."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import yaml

from hermes_duplex.config import AudioConfig, Config, ConfigError, load_config
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.prompts.notices import already_active, switch_refused, switched_to, which_profile
from hermes_duplex.transports.base import Conversation
from hermes_duplex.transports.discord.bot import (
    REFUSAL,
    DiscordConversation,
    DiscordTransport,
    switched_for_next_time,
)
from tests.fakes.fake_discord import ALEX_ID, ROB_ID, STRANGER_ID, FakeVoiceChannel, ReceiverRecorder

BOT_ID = 1


def _config(tmp_path: Path) -> Config:
    """Three profiles; ``rob`` (``ROB_ID``) may use every one, ``alex`` (``ALEX_ID``) general and notes."""
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {}, "stock-picker": {"aliases": ["the stock picker", "stocks"]}, "notes": {}},
        "people": {
            "rob": {"discord_id": str(ROB_ID), "profiles": ["*"]},
            "alex": {"discord_id": str(ALEX_ID), "profiles": ["general", "notes"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={"HERMES_HOME": str(tmp_path)})


def _transport(tmp_path: Path, **overrides: Any) -> DiscordTransport:
    """A bot for ``_config``'s people, switching profiles in a store under ``tmp_path``."""
    config = _config(tmp_path)
    permissions = PermissionChecker(config)
    arguments: dict[str, Any] = {
        "active_profiles": ActiveProfiles(tmp_path / "active_profiles.json", permissions, config),
        "input_rate": 24000,
        "output_rate": 24000,
        **overrides,
    }
    return DiscordTransport("bot-token", AudioConfig(), permissions, **arguments)


def _member(user_id: int, channel: FakeVoiceChannel | None = None) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.display_name = f"user {user_id}"
    member.guild = discord.Object(900)
    member.voice = SimpleNamespace(channel=channel) if channel is not None else None
    return cast(discord.Member, member)


def _interaction(user: discord.Member) -> Any:
    return SimpleNamespace(
        user=user, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock())
    )


def _message(author: discord.Member, content: str) -> Any:
    return SimpleNamespace(author=author, content=content, guild=discord.Object(900), reply=AsyncMock())


def _callback(group: discord.app_commands.Group, name: str) -> Callable[..., Awaitable[None]]:
    """The function a subcommand of ``group`` runs, called with the interaction and its options."""
    command = group.get_command(name)
    assert isinstance(command, discord.app_commands.Command)
    return cast(Callable[..., Awaitable[None]], command.callback)


def _duplex(transport: DiscordTransport) -> discord.app_commands.Group:
    return cast(discord.app_commands.Group, transport.commands.get_command("duplex"))


class Consumer:
    """An ``on_conversation`` that keeps each conversation it gets and reads its frames until it ends."""

    def __init__(self) -> None:
        self.conversations: list[Conversation] = []

    async def __call__(self, conversation: Conversation) -> None:
        self.conversations.append(conversation)
        async for _ in conversation.frames():
            pass


@pytest.fixture
def channel() -> FakeVoiceChannel:
    return FakeVoiceChannel()


@pytest.fixture
def consumer() -> Consumer:
    return Consumer()


@pytest.fixture
def receivers() -> ReceiverRecorder:
    return ReceiverRecorder()


@pytest.fixture
def store(tmp_path: Path) -> ActiveProfiles:
    config = _config(tmp_path)
    return ActiveProfiles(tmp_path / "active_profiles.json", PermissionChecker(config), config)


@pytest.fixture
async def transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: Consumer,
    receivers: ReceiverRecorder,
    store: ActiveProfiles,
) -> AsyncIterator[DiscordTransport]:
    bot = _transport(
        tmp_path, active_profiles=store, receiver_factory=receivers, rejoin_delays_s=(0.0, 0.0, 0.0)
    )
    monkeypatch.setattr(bot.client, "login", AsyncMock())
    monkeypatch.setattr(bot.client, "connect", AsyncMock())
    monkeypatch.setattr(bot.client, "close", AsyncMock())
    monkeypatch.setattr(bot.commands, "sync", AsyncMock())
    monkeypatch.setattr(discord.Client, "user", property(lambda client: discord.Object(BOT_ID)))
    await bot.start(consumer)
    yield bot
    await bot.stop()


def test_uses_its_own_token_and_the_intents_it_needs(tmp_path: Path) -> None:
    bot = _transport(tmp_path)

    intents = bot.client.intents
    assert intents.guilds and intents.voice_states and intents.guild_messages and intents.message_content
    assert not intents.members and not intents.presences


async def test_start_logs_in_registers_the_commands_and_connects(transport: DiscordTransport) -> None:
    cast(AsyncMock, transport.client.login).assert_awaited_once_with("bot-token")
    cast(AsyncMock, transport.commands.sync).assert_awaited_once()
    await asyncio.sleep(0)
    cast(AsyncMock, transport.client.connect).assert_awaited_once()
    duplex = transport.commands.get_command("duplex")
    assert isinstance(duplex, discord.app_commands.Group)
    assert sorted(command.name for command in duplex.commands) == ["join", "leave", "profile"]


async def test_start_reports_a_rejected_token_as_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _transport(tmp_path)
    monkeypatch.setattr(bot.client, "login", AsyncMock(side_effect=discord.LoginFailure("Improper token")))

    with pytest.raises(ConfigError, match="transports.discord.bot_token_env"):
        await bot.start(Consumer())
    await bot.client.close()


async def test_join_refuses_people_not_on_the_allowlist(
    transport: DiscordTransport, channel: FakeVoiceChannel, consumer: Consumer
) -> None:
    assert await transport.join(_member(STRANGER_ID, channel)) == REFUSAL
    assert await transport.leave(_member(STRANGER_ID, channel)) == REFUSAL
    assert channel.voice_clients == []


async def test_join_asks_for_a_voice_channel_first(transport: DiscordTransport) -> None:
    assert "voice channel first" in await transport.join(_member(ROB_ID))


async def test_join_joins_the_invokers_channel_once(
    transport: DiscordTransport, channel: FakeVoiceChannel, consumer: Consumer
) -> None:
    assert await transport.join(_member(ROB_ID, channel)) == f"Joined {channel.mention}."
    await asyncio.sleep(0)

    [conversation] = consumer.conversations
    assert isinstance(conversation, DiscordConversation)
    assert conversation.channel is cast(discord.VoiceChannel, channel)
    assert conversation.person == "rob", "the voice prompt starts on the active profile of who joined it"
    assert len(channel.voice_clients) == 1
    assert "already in" in await transport.join(_member(ROB_ID, channel))
    assert len(channel.voice_clients) == 1


async def test_join_reports_a_failed_connection(
    transport: DiscordTransport, channel: FakeVoiceChannel
) -> None:
    channel.connect_results = [TimeoutError()]

    assert "couldn't join" in await transport.join(_member(ROB_ID, channel))


async def test_leave_ends_the_conversation(
    transport: DiscordTransport, channel: FakeVoiceChannel, consumer: Consumer
) -> None:
    await transport.join(_member(ROB_ID, channel))

    assert await transport.leave(_member(ROB_ID, channel)) == f"Left {channel.mention}."
    assert channel.voice_clients[0].disconnected
    assert "not in a voice channel" in await transport.leave(_member(ROB_ID, channel))
    await asyncio.sleep(0.05)
    assert await transport.join(_member(ROB_ID, channel)) == f"Joined {channel.mention}."


async def test_slash_commands_reply_privately(transport: DiscordTransport, channel: FakeVoiceChannel) -> None:
    join, leave = (_callback(_duplex(transport), name) for name in ("join", "leave"))

    refused = _interaction(_member(STRANGER_ID, channel))
    await join(refused)
    refused.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    refused.followup.send.assert_awaited_once_with(REFUSAL, ephemeral=True)
    assert channel.voice_clients == []

    joined = _interaction(_member(ROB_ID, channel))
    await join(joined)
    joined.followup.send.assert_awaited_once_with(f"Joined {channel.mention}.", ephemeral=True)

    left = _interaction(_member(ROB_ID, channel))
    await leave(left)
    left.followup.send.assert_awaited_once_with(f"Left {channel.mention}.", ephemeral=True)


async def test_profile_switch_heard_by_a_live_conversation_is_confirmed_there(
    transport: DiscordTransport, store: ActiveProfiles
) -> None:
    heard: list[str] = []

    async def live_conversation(profile: str) -> bool:
        heard.append(profile)
        return True

    store.subscribe("rob", live_conversation)
    interaction = _interaction(_member(ROB_ID))

    await _callback(_duplex(transport), "profile")(interaction, name="stock-picker")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once_with(switched_to("stock-picker"), ephemeral=True)
    assert heard == ["stock-picker"]
    assert store.get("rob") == "stock-picker"


async def test_profile_switch_with_no_live_conversation_is_saved_for_next_time(
    transport: DiscordTransport, store: ActiveProfiles
) -> None:
    reply = await transport.switch_profile(_member(ROB_ID), "the stock picker")

    assert reply == switched_for_next_time("stock-picker")
    assert "next time" in reply
    assert store.get("rob") == "stock-picker"


@pytest.mark.parametrize(
    ("user_id", "name", "reply"),
    [
        (ALEX_ID, "stock-picker", switch_refused("stock-picker")),
        (ALEX_ID, "finance", which_profile(["general", "notes"])),
        (ROB_ID, "general", already_active("general")),
        (STRANGER_ID, "general", REFUSAL),
    ],
    ids=["refused", "unknown", "already-active", "not-allowlisted"],
)
async def test_profile_switch_that_changes_nothing_says_why(
    transport: DiscordTransport, store: ActiveProfiles, user_id: int, name: str, reply: str
) -> None:
    assert await transport.switch_profile(_member(user_id), name) == reply
    assert (store.get("rob"), store.get("alex")) == ("general", "general")


@pytest.mark.parametrize(
    ("user_id", "typed", "choices"),
    [
        (ROB_ID, "", ["general", "notes", "stock-picker"]),
        (ALEX_ID, "", ["general", "notes"]),
        (ALEX_ID, "NO", ["notes"]),
        (STRANGER_ID, "", []),
    ],
    ids=["everything", "permitted-only", "filtered", "not-allowlisted"],
)
async def test_profile_autocomplete_offers_only_the_permitted_profiles(
    transport: DiscordTransport, user_id: int, typed: str, choices: list[str]
) -> None:
    command = _duplex(transport).get_command("profile")
    assert isinstance(command, discord.app_commands.Command)
    response = SimpleNamespace(is_done=lambda: False, autocomplete=AsyncMock())
    interaction = cast(discord.Interaction, SimpleNamespace(user=_member(user_id), response=response))

    # Discord sends what the member has typed so far as the option's value.
    typing = cast(discord.app_commands.Namespace, SimpleNamespace(name=typed))

    await command._invoke_autocomplete(interaction, "name", typing)

    [offered] = response.autocomplete.await_args.args
    assert [(choice.name, choice.value) for choice in offered] == [(name, name) for name in choices]


async def test_text_join_from_an_allowlisted_person_joins(
    transport: DiscordTransport, channel: FakeVoiceChannel
) -> None:
    message = _message(_member(ROB_ID, channel), "  Join ")

    await transport.on_message(message)

    message.reply.assert_awaited_once_with(f"Joined {channel.mention}.", mention_author=False)
    assert len(channel.voice_clients) == 1


@pytest.mark.parametrize("user_id, content", [(STRANGER_ID, "join"), (ROB_ID, "join us later")])
async def test_text_join_ignores_strangers_and_other_messages(
    transport: DiscordTransport, channel: FakeVoiceChannel, user_id: int, content: str
) -> None:
    message = _message(_member(user_id, channel), content)

    await transport.on_message(message)

    message.reply.assert_not_awaited()
    assert channel.voice_clients == []


async def test_voice_state_updates_rejoin_the_bot_and_refresh_the_keys(
    transport: DiscordTransport, channel: FakeVoiceChannel, receivers: ReceiverRecorder
) -> None:
    await transport.join(_member(ROB_ID, channel))
    in_channel = cast(discord.VoiceState, SimpleNamespace(channel=channel))
    nowhere = cast(discord.VoiceState, SimpleNamespace(channel=None))

    await transport.on_voice_state_update(_member(ROB_ID), nowhere, in_channel)
    assert receivers.built[0].refreshes == ["voice channel membership changed"]
    await transport.on_voice_state_update(_member(BOT_ID), in_channel, nowhere)
    await asyncio.sleep(0.05)

    assert len(channel.voice_clients) == 2
    assert channel.voice_clients[0].disconnected


async def test_stop_closes_conversations_and_disconnects_the_bot(
    transport: DiscordTransport, channel: FakeVoiceChannel, consumer: Consumer
) -> None:
    await transport.join(_member(ROB_ID, channel))
    await asyncio.sleep(0)

    await transport.stop()

    assert channel.voice_clients[0].disconnected
    cast(AsyncMock, transport.client.close).assert_awaited()
    async with asyncio.timeout(1):
        async for _ in consumer.conversations[0].frames():
            pass
