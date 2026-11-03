"""DiscordTransport: who may bring the bot in and out, the commands, voice state events, start and stop."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hermes_duplex.config import AudioConfig, ConfigError
from hermes_duplex.transports.base import Conversation
from hermes_duplex.transports.discord.bot import REFUSAL, DiscordConversation, DiscordTransport
from tests.fakes.fake_discord import ROB_ID, STRANGER_ID, FakeVoiceChannel, ReceiverRecorder, allowlist

BOT_ID = 1


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


def _callback(group: discord.app_commands.Group, name: str) -> Callable[[Any], Awaitable[None]]:
    """The function a subcommand of ``group`` runs, called with just the interaction."""
    command = group.get_command(name)
    assert isinstance(command, discord.app_commands.Command)
    return cast(Callable[[Any], Awaitable[None]], command.callback)


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
async def transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: Consumer, receivers: ReceiverRecorder
) -> AsyncIterator[DiscordTransport]:
    bot = DiscordTransport(
        "bot-token",
        AudioConfig(),
        allowlist(tmp_path),
        input_rate=24000,
        output_rate=24000,
        receiver_factory=receivers,
        rejoin_delays_s=(0.0, 0.0, 0.0),
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
    bot = DiscordTransport(
        "bot-token", AudioConfig(), allowlist(tmp_path), input_rate=24000, output_rate=24000
    )

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
    assert sorted(command.name for command in duplex.commands) == ["join", "leave"]


async def test_start_reports_a_rejected_token_as_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = DiscordTransport("bad", AudioConfig(), allowlist(tmp_path), input_rate=24000, output_rate=24000)
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
    duplex = cast(discord.app_commands.Group, transport.commands.get_command("duplex"))
    join, leave = (_callback(duplex, name) for name in ("join", "leave"))

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
