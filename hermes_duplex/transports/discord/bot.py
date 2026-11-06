"""The Discord transport: the plugin's own bot, and one conversation per voice channel it joins.

The bot logs in with its own token (never Hermes's). Allowlisted people bring it into their voice
channel with ``/duplex join`` or by typing ``join`` in a server channel, send it away with
``/duplex leave``, and switch their active profile with ``/duplex profile <name>``; everyone else is
refused. While joined, the conversation mixes the allowlisted
voices every 20 ms for the provider and plays the provider's audio back into the channel.
"""

import asyncio
import functools
import itertools
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol

import discord
from discord import app_commands
from discord.channel import VocalGuildChannel

from ...audio.frames import FRAME_MS, FrameCarry
from ...audio.mixer import MIX_CHANNELS, MIX_RATE, mix_allowed
from ...audio.resample import StreamResampler
from ...config import AudioConfig, ConfigError
from ...people import PermissionChecker
from ...profiles import ActiveProfiles
from ...prompts.notices import switch_reply, switched_to
from ..base import Conversation, InputFrame
from .playback import DuplexAudioSource
from .receiver import VoiceReceiver

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10.0
"""How long one attempt to join a voice channel may take, in seconds."""

DISCONNECT_TIMEOUT_S = 2.0
"""How long leaving a voice channel may take before the conversation gives up waiting, in seconds."""

REJOIN_DELAYS_S = (1.0, 2.0, 4.0)
"""Wait before each rejoin attempt after the voice connection drops, in seconds; one attempt each."""

JITTER_FRAMES = 5
"""Most received frames held per person between ticks; older ones are dropped to bound the delay."""

MAX_TICK_LAG_S = 0.1
"""When the tick loop falls this far behind real time, it skips ahead instead of catching up in a burst."""

REFUSAL = "Sorry, you are not on this voice assistant's list of people."

MAX_AUTOCOMPLETE_CHOICES = 25
"""The most suggestions Discord accepts for one command option."""


def switched_for_next_time(profile: str) -> str:
    """Reply to a switch to ``profile`` that no live conversation heard, so it applies from the
    person's next one."""
    return f"{switched_to(profile)} You're not in a voice conversation, so it applies the next time you talk to me."


class Receiver(Protocol):
    """What a conversation needs from a voice receiver; ``VoiceReceiver`` is the real one."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def refresh_credentials(self, reason: str) -> None: ...


type ReceiverFactory = Callable[
    [discord.VoiceClient, PermissionChecker, Callable[[str, bytes], None]], Receiver
]
"""Builds the receiver for one voice connection: ``(voice_client, permissions, on_frame)``."""


class DiscordConversation:
    """One voice channel the bot has joined: the allowlisted people in it share one voice session.

    Input: each person's decoded audio is re-chunked into 20 ms frames (Discord packets may carry
    20, 40, or 60 ms). ``frames()`` ticks every 20 ms at real-time pace. Each tick mixes one received
    frame per allowlisted person (silence when nobody sent one, so the provider's voice activity
    detection still hears the pause), resamples the mix to ``input_rate`` mono, and yields it.
    ``t_ms`` counts the frames yielded (× 20, from 0); ``voiced_speakers`` is who was louder than
    ``audio.gate_threshold_dbfs`` in the tick.

    Output: ``play`` resamples the audio and queues it for the channel's playback at once; the
    session's pacer keeps what reaches it within its lead, and ``flush_playback`` discards it.

    When the bot is disconnected or moved, the conversation rejoins (the channel it was moved to,
    or the one it was in) after each of ``rejoin_delays_s``, with input carrying on as silence so
    the provider session survives. If every attempt fails, it posts why in the channel's text chat,
    since it can no longer speak there, and closes.

    Use it from one event loop.
    """

    def __init__(
        self,
        conversation_id: str,
        voice_client: discord.VoiceClient,
        *,
        person: str | None = None,
        permissions: PermissionChecker,
        audio: AudioConfig,
        input_rate: int,
        output_rate: int,
        receiver_factory: ReceiverFactory = VoiceReceiver,
        rejoin_delays_s: Sequence[float] = REJOIN_DELAYS_S,
    ) -> None:
        """``voice_client`` must be connected. ``person`` is who brought the bot in, if known.
        ``input_rate`` and ``output_rate`` are the provider's mono sample rates in Hz; ``play`` accepts
        only ``output_rate``."""
        self.id = conversation_id
        self.person = person
        """Who brought the bot in (``/duplex join``), whose active profile the voice prompt starts on;
        everyone allowed in the channel shares the conversation."""
        self._voice_client: discord.VoiceClient | None = voice_client
        self._channel: VocalGuildChannel = voice_client.channel
        self._permissions = permissions
        self._receiver_factory = receiver_factory
        self._rejoin_delays_s = tuple(rejoin_delays_s)
        self._receiver: Receiver | None = None

        self._allowed = frozenset(
            entry.name
            for discord_id in permissions.allowlisted_discord_ids
            if (entry := permissions.by_discord_id(discord_id)) is not None
        )
        self._voiced_threshold_dbfs = audio.gate_threshold_dbfs
        self._received: dict[str, deque[bytes]] = {}
        self._received_carries: dict[str, FrameCarry] = {}
        """Each person's decoded audio not yet filling a 20 ms frame."""
        self._input_rate = input_rate
        self._input_resampler = StreamResampler(MIX_RATE, input_rate, in_channels=MIX_CHANNELS)
        self._input_carry = FrameCarry(input_rate)
        self._frames_sent = 0

        self._output_rate = output_rate
        self._output_resampler = self._new_output_resampler()
        self._source = DuplexAudioSource(audio.playback_queue_s)

        self._rejoining: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def channel(self) -> VocalGuildChannel:
        """The voice channel the conversation is in, or is rejoining."""
        return self._channel

    @property
    def closed(self) -> bool:
        """Whether ``close`` has run (the conversation left its channel and ``frames()`` is ending)."""
        return self._closed

    def present(self, person: str) -> bool:
        """Whether ``person`` is in the voice channel now, as the channel's voice states tell."""
        return any(
            (entry := self._permissions.by_discord_id(member_id)) is not None and entry.name == person
            for member_id in self._channel.voice_states
        )

    def alone(self, person: str) -> bool:
        """Whether ``person`` is in the voice channel and every other member is a bot, as the channel's
        voice states and the guild's member cache tell; a member missing from the cache counts as a
        person."""
        found = False
        for member_id in self._channel.voice_states:
            entry = self._permissions.by_discord_id(member_id)
            if entry is not None and entry.name == person:
                found = True
                continue
            member = self._channel.guild.get_member(member_id)
            if member is None or not member.bot:
                return False
        return found

    def start(self) -> None:
        """Start receiving and playing in the channel. Call it once, on the event loop."""
        self._attach(self._voice_client)

    async def frames(self) -> AsyncIterator[InputFrame]:
        """Yield the mixed input every 20 ms until the conversation closes. Iterate it from one consumer."""
        next_tick_s = time.monotonic()
        while not self._closed:
            ticked = {person: queue.popleft() for person, queue in self._received.items() if queue}
            mixed = mix_allowed(ticked, self._allowed, self._voiced_threshold_dbfs)
            # The resampler holds back a few ms, so a tick can yield no frame at first and two later.
            for pcm in self._input_carry.push(self._input_resampler.push(mixed.pcm16_stereo)):
                yield InputFrame(
                    pcm16_mono=pcm,
                    rate=self._input_rate,
                    voiced_speakers=mixed.voiced_speakers,
                    t_ms=self._frames_sent * FRAME_MS,
                )
                self._frames_sent += 1
            next_tick_s += FRAME_MS / 1000
            now = time.monotonic()
            if now - next_tick_s > MAX_TICK_LAG_S:
                next_tick_s = now
            await asyncio.sleep(max(0.0, next_tick_s - now))

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        """Queue ``pcm16_mono`` for the channel; ``rate`` must be ``output_rate`` (``ValueError`` otherwise)."""
        if rate != self._output_rate:
            raise ValueError(f"this conversation plays {self._output_rate} Hz audio, got {rate} Hz")
        if self._closed:
            return
        self._source.feed(self._output_resampler.push(pcm16_mono))

    async def flush_playback(self) -> None:
        """Discard the audio queued for the channel; playback stops within one 20 ms tick."""
        # The resampler's filter still holds a few ms of the discarded audio.
        self._output_resampler = self._new_output_resampler()
        self._source.flush()

    async def notify(self, event: dict[str, Any]) -> None:
        """Does nothing: a voice channel has no transcript or task display."""

    async def close(self, reason: str) -> None:
        """Leave the voice channel and end ``frames()``; ``reason`` is logged. Idempotent."""
        if self._closed:
            return
        self._closed = True
        logger.info("Discord conversation %s closing: %s", self.id, reason)
        if self._rejoining is not None and self._rejoining is not asyncio.current_task():
            self._rejoining.cancel()
        await self._detach()

    def bot_moved(self, channel: VocalGuildChannel | None) -> None:
        """React to the bot's own voice state: ``channel`` is where it is now, None when disconnected.

        A disconnect or a move to another channel starts a rejoin unless one is already running.
        """
        if self._closed or self._rejoining is not None or channel == self._channel:
            return
        if channel is not None:
            self._channel = channel
        logger.warning(
            "Discord conversation %s %s; rejoining %s",
            self.id,
            "was disconnected" if channel is None else "was moved",
            self._channel.name,
        )
        self._rejoining = asyncio.create_task(self._rejoin())

    def members_changed(self) -> None:
        """Someone joined or left the channel: re-read the voice keys, which may have rotated."""
        if self._receiver is not None:
            self._receiver.refresh_credentials("voice channel membership changed")

    def _on_frame(self, person_id: str, pcm: bytes) -> None:
        if self._closed:
            return
        carry = self._received_carries.setdefault(person_id, FrameCarry(MIX_RATE, MIX_CHANNELS))
        self._received.setdefault(person_id, deque(maxlen=JITTER_FRAMES)).extend(carry.push(pcm))

    def _new_output_resampler(self) -> StreamResampler:
        return StreamResampler(self._output_rate, MIX_RATE, out_channels=MIX_CHANNELS)

    def _attach(self, voice_client: discord.VoiceClient | None) -> None:
        """Receive from and play into ``voice_client``, a fresh connection to ``self._channel``."""
        assert voice_client is not None
        self._voice_client = voice_client
        self._received.clear()
        self._received_carries.clear()
        self._receiver = self._receiver_factory(voice_client, self._permissions, self._on_frame)
        self._receiver.start()
        voice_client.play(self._source)

    async def _detach(self) -> None:
        """Stop receiving and playing, and leave the voice connection if it is still up."""
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None
        voice_client, self._voice_client = self._voice_client, None
        if voice_client is None:
            return
        voice_client.stop()
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT_S):
                await voice_client.disconnect(force=True)
        except (TimeoutError, discord.DiscordException, OSError) as error:
            logger.warning("Discord conversation %s: leaving voice failed: %s", self.id, error)

    async def _rejoin(self) -> None:
        await self._detach()
        attempts = len(self._rejoin_delays_s)
        for attempt, delay_s in enumerate(self._rejoin_delays_s, start=1):
            await asyncio.sleep(delay_s)
            try:
                voice_client = await self._channel.connect(timeout=CONNECT_TIMEOUT_S, self_deaf=False)
            except (TimeoutError, discord.DiscordException, OSError) as error:
                logger.warning(
                    "Discord conversation %s: rejoin attempt %d of %d failed: %s",
                    self.id,
                    attempt,
                    attempts,
                    error,
                )
                continue
            self._rejoining = None
            self._attach(voice_client)
            logger.info("Discord conversation %s rejoined %s", self.id, self._channel.name)
            return
        self._rejoining = None
        reason = f"lost the voice connection to {self._channel.name} and could not rejoin"
        try:
            await self._channel.send(f"Ending the voice conversation: I {reason}.")
        except discord.DiscordException as error:
            logger.warning("Discord conversation %s: could not post the end message: %s", self.id, error)
        await self.close(reason)


class DiscordTransport:
    """The plugin's own Discord bot; each voice channel it joins becomes a ``DiscordConversation``.

    The bot needs the guilds, voice states, guild messages, and message content intents (the last
    must be enabled for the bot in Discord's developer portal). It joins at most one channel per
    server. Each conversation is passed to ``on_conversation``, which runs alongside it; the
    conversation ends when someone uses ``/duplex leave``, when it cannot rejoin, when it is closed,
    or when ``on_conversation`` returns or raises.
    """

    name = "discord"

    def __init__(
        self,
        token: str,
        audio: AudioConfig,
        permissions: PermissionChecker,
        *,
        active_profiles: ActiveProfiles,
        input_rate: int,
        output_rate: int,
        receiver_factory: ReceiverFactory = VoiceReceiver,
        rejoin_delays_s: Sequence[float] = REJOIN_DELAYS_S,
    ) -> None:
        """``token`` is the bot's token; the rates are the provider's, in Hz.

        ``/duplex profile`` switches in ``active_profiles``, by a profile's name or one of its aliases.
        ``receiver_factory`` and ``rejoin_delays_s`` are passed to each conversation.
        """
        self._token = token
        self._permissions = permissions
        self._active_profiles = active_profiles
        self._new_conversation = functools.partial(
            DiscordConversation,
            permissions=permissions,
            audio=audio,
            input_rate=input_rate,
            output_rate=output_rate,
            receiver_factory=receiver_factory,
            rejoin_delays_s=rejoin_delays_s,
        )
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        """The discord.py client; logged in by ``start``."""
        self.commands = app_commands.CommandTree(self.client)
        """The bot's app commands: the ``/duplex`` group."""
        self.commands.add_command(self._duplex_commands())
        self.client.event(self.on_message)
        self.client.event(self.on_voice_state_update)

        self._on_conversation: Callable[[Conversation], Awaitable[None]] | None = None
        self._gateway: asyncio.Task[None] | None = None
        self._conversations: dict[int, DiscordConversation] = {}
        """Guild id → the conversation in that server."""
        self._consumers: set[asyncio.Task[None]] = set()
        self._conversation_numbers = itertools.count(1)

    async def start(self, on_conversation: Callable[[Conversation], Awaitable[None]]) -> None:
        """Log in, register ``/duplex``, and connect to Discord's gateway in the background.

        Raises ``ConfigError`` on ``transports.discord.bot_token_env`` when Discord rejects the token.
        A failure to register the commands is logged; the text ``join`` still works then.
        """
        self._on_conversation = on_conversation
        try:
            await self.client.login(self._token)
        except discord.LoginFailure:
            raise ConfigError("transports.discord.bot_token_env", "Discord rejected the bot token") from None
        try:
            await self.commands.sync()
        except discord.DiscordException as error:
            logger.warning("could not register the /duplex Discord commands: %s", error)
        self._gateway = asyncio.create_task(self._run_gateway())
        logger.info("Discord bot logged in as %s", self.client.user)

    async def stop(self) -> None:
        """Close every conversation (leaving its channel) and disconnect the bot. Does nothing if not started."""
        for conversation in list(self._conversations.values()):
            await conversation.close("server stopping")
        await self.client.close()
        if self._gateway is not None:
            await asyncio.gather(self._gateway, return_exceptions=True)
            self._gateway = None

    async def join(self, member: discord.Member | discord.User) -> str:
        """Join ``member``'s current voice channel and start a conversation there, whose voice prompt
        starts on ``member``'s active profile.

        Returns the reply for ``member``: a refusal when they are not on the allowlist, a hint when
        they are not in a voice channel or the bot is already in one in that server, or what happened.
        """
        if (person := self._permissions.by_discord_id(member.id)) is None:
            return REFUSAL
        voice = member.voice if isinstance(member, discord.Member) else None
        channel = voice.channel if voice is not None else None
        if channel is None:
            return "Join a voice channel first, then ask me again."
        if (existing := self._conversation_in(channel.guild.id)) is not None:
            return f"I'm already in {existing.channel.mention}; use /duplex leave first."
        try:
            voice_client = await channel.connect(timeout=CONNECT_TIMEOUT_S, self_deaf=False)
        except (TimeoutError, discord.DiscordException, OSError) as error:
            logger.warning("could not join Discord voice channel %s: %s", channel.name, error)
            return f"I couldn't join {channel.mention}; try again in a moment."

        conversation = self._new_conversation(
            f"discord-{channel.id}-{next(self._conversation_numbers)}", voice_client, person=person.name
        )
        self._conversations[channel.guild.id] = conversation
        conversation.start()
        consumer = asyncio.create_task(self._run_consumer(channel.guild.id, conversation))
        self._consumers.add(consumer)
        consumer.add_done_callback(self._consumers.discard)
        logger.info("Discord conversation %s started in %s", conversation.id, channel.name)
        return f"Joined {channel.mention}."

    async def leave(self, member: discord.Member | discord.User) -> str:
        """End the conversation in ``member``'s server; returns the reply for ``member``."""
        if self._permissions.by_discord_id(member.id) is None:
            return REFUSAL
        guild = member.guild if isinstance(member, discord.Member) else None
        conversation = self._conversation_in(guild.id) if guild is not None else None
        if conversation is None:
            return "I'm not in a voice channel in this server."
        await conversation.close(f"{member.display_name} asked me to leave")
        return f"Left {conversation.channel.mention}."

    async def switch_profile(self, member: discord.Member | discord.User, name: str) -> str:
        """Make the profile ``name`` (a profile name or one of its aliases) ``member``'s active profile;
        returns the reply for ``member``.

        Every live conversation that has heard ``member`` and that they are still in confirms the
        switch aloud. With none, the switch is saved and the reply says it applies from their next
        conversation. A name that fits no single profile is answered with the profiles they may use.
        """
        person = self._permissions.by_discord_id(member.id)
        if person is None:
            return REFUSAL
        target = self._active_profiles.resolve(name)
        if target is None:
            return switch_reply("unknown", None, self._active_profiles.permitted(person.name))
        result = await self._active_profiles.switch(person.name, target)
        logger.info("Discord switch of %s to profile %s: %s", person.name, target, result.outcome)
        if result.outcome == "switched" and not result.notified_live:
            return switched_for_next_time(target)
        return switch_reply(result.outcome, target, self._active_profiles.permitted(person.name))

    def permitted_profiles(self, member: discord.Member | discord.User) -> list[str]:
        """The profiles ``member`` may switch to, in config order; empty when they are not allowlisted."""
        person = self._permissions.by_discord_id(member.id)
        return [] if person is None else self._active_profiles.permitted(person.name)

    async def on_message(self, message: discord.Message) -> None:
        """discord.py event: a server message reading ``join`` from an allowlisted person joins their
        channel. Anyone else's ``join`` is ignored, since a plain message cannot be refused privately."""
        if message.author.bot or message.guild is None or message.content.strip().lower() != "join":
            return
        if self._permissions.by_discord_id(message.author.id) is None:
            logger.debug(
                "ignoring 'join' from Discord user %d, who is not on the allowlist", message.author.id
            )
            return
        await message.reply(await self.join(message.author), mention_author=False)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """discord.py event: rejoin when the bot is disconnected or moved, and re-read the voice keys
        when someone joins or leaves a channel the bot is in."""
        conversation = self._conversation_in(member.guild.id)
        if conversation is None:
            return
        if self.client.user is not None and member.id == self.client.user.id:
            conversation.bot_moved(after.channel)
        elif conversation.channel in (before.channel, after.channel):
            conversation.members_changed()

    def _conversation_in(self, guild_id: int) -> DiscordConversation | None:
        """The open conversation in server ``guild_id``; a closed one is ignored while it winds down."""
        conversation = self._conversations.get(guild_id)
        return conversation if conversation is not None and not conversation.closed else None

    def _duplex_commands(self) -> app_commands.Group:
        duplex = app_commands.Group(name="duplex", description="Talk to Hermes by voice", guild_only=True)

        @duplex.command(name="join", description="Join your voice channel")
        async def join(interaction: discord.Interaction) -> None:
            # Joining can take longer than the 3 s Discord allows before the first response.
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await self.join(interaction.user), ephemeral=True)

        @duplex.command(name="leave", description="Leave the voice channel")
        async def leave(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await self.leave(interaction.user), ephemeral=True)

        @duplex.command(name="profile", description="Switch the Hermes profile your requests run on")
        @app_commands.describe(name="The profile to switch to")
        async def profile(interaction: discord.Interaction, name: str) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await self.switch_profile(interaction.user, name), ephemeral=True)

        @profile.autocomplete("name")
        async def profile_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            typed = current.casefold()
            matching = [
                name for name in self.permitted_profiles(interaction.user) if typed in name.casefold()
            ]
            return [
                app_commands.Choice(name=name, value=name) for name in matching[:MAX_AUTOCOMPLETE_CHOICES]
            ]

        return duplex

    async def _run_gateway(self) -> None:
        try:
            await self.client.connect(reconnect=True)
        except Exception:
            logger.exception("the Discord bot's gateway connection ended")

    async def _run_consumer(self, guild_id: int, conversation: DiscordConversation) -> None:
        assert self._on_conversation is not None
        reason = "conversation ended"
        try:
            await self._on_conversation(conversation)
        except Exception:
            logger.exception("Discord conversation %s failed", conversation.id)
            reason = "internal error"
        finally:
            await conversation.close(reason)
            if self._conversations.get(guild_id) is conversation:
                del self._conversations[guild_id]
            logger.info("Discord conversation %s ended", conversation.id)
