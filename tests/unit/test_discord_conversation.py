"""DiscordConversation: the 20 ms input tick, output, flush, and rejoin, with a fake voice client."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import discord
import numpy as np
import pytest

from hermes_duplex.audio.frames import frame_bytes, silent_frame
from hermes_duplex.config import AudioConfig
from hermes_duplex.transports.base import InputFrame
from hermes_duplex.transports.discord.bot import DiscordConversation
from tests.fakes.fake_discord import ROB_ID, STRANGER_ID, FakeVoiceChannel, ReceiverRecorder, allowlist

INPUT_RATE = 24000
OUTPUT_RATE = 24000
DISCORD_FRAME = frame_bytes(48000, 2)


def _tone(samples: int, channels: int = 1, amplitude: int = 8000) -> bytes:
    """A 440 Hz sine of ``samples`` samples per channel (at 48 kHz for stereo, 24 kHz for mono)."""
    rate = 48000 if channels == 2 else 24000
    wave = (amplitude * np.sin(2 * np.pi * 440 * np.arange(samples) / rate)).astype("<i2")
    return np.repeat(wave, channels).tobytes()


def _peak(pcm16: bytes) -> int:
    return int(np.abs(np.frombuffer(pcm16, dtype="<i2").astype(np.int32)).max())


async def _wait_until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.005)


async def _take(frames: AsyncIterator[InputFrame], count: int) -> list[InputFrame]:
    return [await anext(frames) for _ in range(count)]


@pytest.fixture
def channel() -> FakeVoiceChannel:
    return FakeVoiceChannel()


@pytest.fixture
def receivers() -> ReceiverRecorder:
    return ReceiverRecorder()


@pytest.fixture
async def conversation(
    tmp_path: Path, channel: FakeVoiceChannel, receivers: ReceiverRecorder
) -> AsyncIterator[DiscordConversation]:
    voice_client = await channel.connect()
    started = DiscordConversation(
        "discord-test",
        voice_client,
        permissions=allowlist(tmp_path),
        audio=AudioConfig(),
        input_rate=INPUT_RATE,
        output_rate=OUTPUT_RATE,
        receiver_factory=receivers,
        rejoin_delays_s=(0.0, 0.01, 0.02),
    )
    started.start()
    yield started
    await started.close("test over")


def _source(channel: FakeVoiceChannel) -> discord.AudioSource:
    source = channel.voice_clients[-1].source
    assert source is not None
    return source


def _queued_audio_frames(source: discord.AudioSource) -> int:
    """Read the source until it plays silence; returns how many frames of audio it held."""
    silence = silent_frame(48000, 2)
    count = 0
    while source.read() != silence:
        count += 1
    return count


async def test_a_conversation_started_by_nobody_known_has_no_person(
    conversation: DiscordConversation,
) -> None:
    assert conversation.person is None


async def test_present_is_whoever_is_in_the_voice_channel_now(
    conversation: DiscordConversation, channel: FakeVoiceChannel
) -> None:
    assert not conversation.present("rob")

    channel.voice_states[ROB_ID] = object()
    channel.voice_states[STRANGER_ID] = object()
    assert conversation.present("rob")
    assert not conversation.present("alex")

    del channel.voice_states[ROB_ID]
    assert not conversation.present("rob")


async def test_start_receives_and_plays_on_the_voice_client(
    conversation: DiscordConversation, channel: FakeVoiceChannel, receivers: ReceiverRecorder
) -> None:
    [receiver] = receivers.built
    assert receiver.started
    assert receiver.voice_client is channel.voice_clients[0]
    assert _source(channel).read() == silent_frame(48000, 2)


async def test_ticks_every_20_ms_with_consecutive_t_ms(conversation: DiscordConversation) -> None:
    frames = conversation.frames()
    await anext(frames)
    begun = time.monotonic()
    taken = await _take(frames, 10)
    elapsed_s = time.monotonic() - begun

    assert [frame.t_ms for frame in taken] == [20 * n for n in range(1, 11)]
    assert all(
        frame.rate == INPUT_RATE and len(frame.pcm16_mono) == frame_bytes(INPUT_RATE) for frame in taken
    )
    # Ten ticks of 20 ms, with slack for the resampler's start-up delay and a busy machine.
    assert 0.15 <= elapsed_s < 0.5


async def test_feeds_paced_silence_when_no_packets_arrive(conversation: DiscordConversation) -> None:
    taken = await _take(conversation.frames(), 5)

    # The resampler's dither leaves at most ±1 in silence.
    assert all(_peak(frame.pcm16_mono) <= 1 for frame in taken)
    assert all(frame.voiced_speakers == frozenset() for frame in taken)


async def test_mixes_received_voices_and_marks_who_is_voiced(
    conversation: DiscordConversation, receivers: ReceiverRecorder
) -> None:
    [receiver] = receivers.built
    for _ in range(3):
        receiver.on_frame("rob", _tone(960, channels=2))
        receiver.on_frame("alex", silent_frame(48000, 2))
        receiver.on_frame("stranger", _tone(960, channels=2))

    taken = await _take(conversation.frames(), 6)

    assert {"rob"} == set().union(*(frame.voiced_speakers for frame in taken))
    assert max(_peak(frame.pcm16_mono) for frame in taken) > 1000
    assert taken[-1].voiced_speakers == frozenset()


async def test_mixes_40_and_60_ms_packets_one_20_ms_frame_per_tick(
    conversation: DiscordConversation, receivers: ReceiverRecorder
) -> None:
    [receiver] = receivers.built
    receiver.on_frame("rob", _tone(1920, channels=2))  # one 40 ms packet: 7680 bytes
    receiver.on_frame("alex", _tone(2880, channels=2))  # one 60 ms packet: 11520 bytes

    taken = await _take(conversation.frames(), 8)  # mix_allowed raised on the long frames before

    assert {"rob", "alex"} == set().union(*(frame.voiced_speakers for frame in taken))
    assert taken[-1].voiced_speakers == frozenset(), "both packets are spent within three ticks"


async def test_play_queues_the_audio_for_playback_at_once(
    conversation: DiscordConversation, channel: FakeVoiceChannel
) -> None:
    await conversation.play(_tone(INPUT_RATE // 5), OUTPUT_RATE)  # 200 ms, as the session's pacer sends it

    queued = _queued_audio_frames(_source(channel))

    # 200 ms is 10 frames; the resampler can hold back part of one.
    assert 8 <= queued <= 10


async def test_flush_drops_the_queued_audio_within_one_tick(
    conversation: DiscordConversation, channel: FakeVoiceChannel
) -> None:
    await conversation.play(_tone(INPUT_RATE // 5), OUTPUT_RATE)
    source = _source(channel)
    for _ in range(3):
        source.read()

    await conversation.flush_playback()

    assert source.read() == silent_frame(48000, 2)


async def test_play_rejects_another_rate(conversation: DiscordConversation) -> None:
    with pytest.raises(ValueError, match="24000 Hz"):
        await conversation.play(bytes(320), 16000)


async def test_notify_does_nothing(conversation: DiscordConversation) -> None:
    await conversation.notify({"transcript": {"speaker": "user", "text": "hi"}})


async def test_membership_change_refreshes_the_voice_keys(
    conversation: DiscordConversation, receivers: ReceiverRecorder
) -> None:
    conversation.members_changed()

    assert receivers.built[0].refreshes == ["voice channel membership changed"]


async def test_rejoins_after_a_disconnect_and_keeps_the_frames_going(
    conversation: DiscordConversation, channel: FakeVoiceChannel, receivers: ReceiverRecorder
) -> None:
    channel.connect_results = [TimeoutError(), discord.ClientException("busy")]
    frames = conversation.frames()
    first = await anext(frames)

    conversation.bot_moved(None)
    await _wait_until(lambda: len(channel.voice_clients) == 2)

    old_client, new_client = channel.voice_clients
    assert old_client.disconnected and old_client.stopped
    assert receivers.built[0].stopped
    assert receivers.built[1].started and receivers.built[1].voice_client is new_client
    assert new_client.source is old_client.source
    assert [frame.t_ms for frame in await _take(frames, 3)] == [first.t_ms + 20 * n for n in range(1, 4)]
    assert channel.sent == []


async def test_ignores_its_own_disconnect_while_rejoining(
    conversation: DiscordConversation, channel: FakeVoiceChannel
) -> None:
    conversation.bot_moved(None)
    conversation.bot_moved(None)
    await _wait_until(lambda: len(channel.voice_clients) == 2)
    conversation.bot_moved(cast(discord.VoiceChannel, channel))
    await asyncio.sleep(0.05)

    assert len(channel.voice_clients) == 2


async def test_follows_a_move_to_another_channel(conversation: DiscordConversation) -> None:
    elsewhere = FakeVoiceChannel(channel_id=501, name="kitchen")

    conversation.bot_moved(cast(discord.VoiceChannel, elsewhere))
    await _wait_until(lambda: len(elsewhere.voice_clients) == 1)

    assert conversation.channel is cast(discord.VoiceChannel, elsewhere)


async def test_ends_with_a_message_after_three_failed_rejoins(
    conversation: DiscordConversation, channel: FakeVoiceChannel
) -> None:
    channel.connect_results = [TimeoutError(), TimeoutError(), discord.ClientException("no"), None]
    frames = conversation.frames()
    await anext(frames)

    conversation.bot_moved(None)

    async with asyncio.timeout(2):
        async for _ in frames:
            pass
    assert len(channel.voice_clients) == 1
    assert channel.connect_results == [None]
    [message] = channel.sent
    assert "could not rejoin" in message


async def test_close_leaves_the_channel_and_ends_the_frames(
    conversation: DiscordConversation, channel: FakeVoiceChannel, receivers: ReceiverRecorder
) -> None:
    frames = conversation.frames()
    await anext(frames)

    await conversation.close("bye")
    await conversation.close("bye again")

    assert channel.voice_clients[0].disconnected
    assert receivers.built[0].stopped
    async with asyncio.timeout(1):
        async for _ in frames:  # ends at the next tick
            pass
    conversation.bot_moved(None)
    assert len(channel.voice_clients) == 1
