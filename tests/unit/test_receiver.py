"""VoiceReceiver with synthetic RTP: decryption, speaker mapping and inference, and what it drops."""

import asyncio
import struct
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import discord
import nacl.secret
import numpy as np
import pytest
import yaml
from discord import opus
from discord.utils import MISSING

from hermes_duplex.config import load_config
from hermes_duplex.people import PermissionChecker
from hermes_duplex.transports.discord import receiver as receiver_module
from hermes_duplex.transports.discord.receiver import REKEY_FAILURE_STREAK, VoiceReceiver

ROB_ID = 123
ALEX_ID = 456
STRANGER_ID = 999
BOT_ID = 1
BOT_SSRC = 10
ROB_SSRC = 11
ALEX_SSRC = 12
KEY = bytes(range(32))
NEW_KEY = bytes(range(32, 64))
FRAME_BYTES = 3840  # 20 ms of PCM16 at 48 kHz stereo
DAVE_PREFIX = b"dave:"


class FakeDecoder:
    """Stands in for ``discord.opus.Decoder``: records each payload and returns a silent frame."""

    decoded: ClassVar[list[bytes]] = []

    def decode(self, data: bytes, *, fec: bool = False) -> bytes:
        FakeDecoder.decoded.append(data)
        return bytes(FRAME_BYTES)


class StubDaveSession:
    """A DAVE session whose "ciphertext" is the plaintext prefixed for one user.

    A payload without any prefix is a sender's unencrypted frame. As davey does, it is returned as it
    is while ``passthrough`` is on, and raises the "Unencrypted" error otherwise. ``on_decrypt`` runs
    inside each decrypt call.
    """

    def __init__(self, *, passthrough: bool = False) -> None:
        self.passthrough = passthrough
        self.calls: list[int] = []
        self.on_decrypt: Callable[[], None] = lambda: None

    def decrypt(self, user_id: int, media_type: Any, packet: bytes) -> bytes:
        self.calls.append(user_id)
        self.on_decrypt()
        prefix = DAVE_PREFIX + str(user_id).encode() + b":"
        if packet.startswith(prefix):
            return packet[len(prefix) :]
        if not packet.startswith(DAVE_PREFIX):
            if self.passthrough:
                return packet
            raise ValueError("Failed to decrypt: UnencryptedWhenPassthroughDisabled")
        raise ValueError("Failed to decrypt: DecryptionFailed")


class FakeConnection:
    """The parts of discord.py's ``VoiceConnectionState`` the receiver uses."""

    def __init__(self, dave_session: StubDaveSession | None = None) -> None:
        self.secret_key = list(KEY)
        self.ssrc = BOT_SSRC
        self.dave_session = dave_session
        self.dave_protocol_version = 1 if dave_session else 0
        self.hook: Any = None
        self.ws: Any = MISSING
        self.listeners: list[Any] = []

    def add_socket_listener(self, callback: Any) -> None:
        self.listeners.append(callback)

    def remove_socket_listener(self, callback: Any) -> None:
        self.listeners.remove(callback)


class FakeVoiceClient:
    def __init__(
        self,
        connection: FakeConnection,
        member_ids: list[int],
        bot_ids: frozenset[int] = frozenset(),
        uncached_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._connection = connection
        self.user = SimpleNamespace(id=BOT_ID)
        members = {uid: SimpleNamespace(id=uid, bot=uid == BOT_ID or uid in bot_ids) for uid in member_ids}
        members[BOT_ID] = SimpleNamespace(id=BOT_ID, bot=True)
        self.channel = SimpleNamespace(
            voice_states=dict.fromkeys([BOT_ID, *member_ids], SimpleNamespace()),
            guild=SimpleNamespace(get_member=lambda uid: None if uid in uncached_ids else members.get(uid)),
        )


class Harness:
    """A started receiver over a fake connection, and the frames it delivered."""

    receiver: VoiceReceiver

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.frames: list[tuple[str, bytes]] = []

    def send(self, packet: bytes) -> None:
        for listener in self.connection.listeners:
            listener(packet)

    async def delivered(self) -> list[tuple[str, bytes]]:
        await asyncio.sleep(0)
        frames, self.frames = self.frames, []
        return frames

    async def speaking(self, ssrc: int, user_id: int) -> None:
        await self.connection.hook(None, {"op": 5, "d": {"ssrc": ssrc, "user_id": user_id, "speaking": 1}})


def rtp(
    ssrc: int,
    payload: bytes,
    *,
    key: bytes = KEY,
    seq: int = 1,
    extension: bytes = b"",
    padding: int = 0,
) -> bytes:
    """An ``aead_xchacha20_poly1305_rtpsize`` voice packet as Discord sends it."""
    first_byte = 0x80 | (0x10 if extension else 0) | (0x20 if padding else 0)
    header = struct.pack(">BBHII", first_byte, 0x78, seq, seq * 960, ssrc)
    body = payload
    if extension:
        header += struct.pack(">HH", 0xBEDE, len(extension) // 4)
        body = extension + payload
    if padding:
        body += bytes(padding - 1) + bytes([padding])
    nonce_suffix = struct.pack(">I", seq)
    nonce = nonce_suffix.ljust(24, b"\0")
    ciphertext = nacl.secret.Aead(key).encrypt(body, header, nonce).ciphertext
    return header + ciphertext + nonce_suffix


def dave(user_id: int, opus_payload: bytes) -> bytes:
    return DAVE_PREFIX + str(user_id).encode() + b":" + opus_payload


@pytest.fixture
def permissions(tmp_path: Path) -> PermissionChecker:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {}},
        "people": {
            "rob": {"discord_id": str(ROB_ID), "profiles": ["*"]},
            "alex": {"discord_id": str(ALEX_ID), "profiles": ["*"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PermissionChecker(load_config(path, env={}))


@pytest.fixture(autouse=True)
def fake_opus(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeDecoder.decoded = []
    monkeypatch.setattr(receiver_module.opus, "Decoder", FakeDecoder)
    yield


@pytest.fixture
def start(permissions: PermissionChecker) -> Iterator[Any]:
    started: list[VoiceReceiver] = []

    def start(
        member_ids: list[int],
        dave_session: StubDaveSession | None = None,
        *,
        bot_ids: frozenset[int] = frozenset(),
        uncached_ids: frozenset[int] = frozenset(),
    ) -> Harness:
        connection = FakeConnection(dave_session)
        voice_client = cast(
            discord.VoiceClient, FakeVoiceClient(connection, member_ids, bot_ids, uncached_ids)
        )
        harness = Harness(connection)
        harness.receiver = VoiceReceiver(
            voice_client, permissions, lambda *frame: harness.frames.append(frame)
        )
        harness.receiver.start()
        started.append(harness.receiver)
        return harness

    yield start
    for receiver in started:
        receiver.stop()


async def test_mapped_ssrc_streams_each_frame_to_its_person(start: Any) -> None:
    harness: Harness = start([ROB_ID, ALEX_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    await harness.speaking(ALEX_SSRC, ALEX_ID)

    for seq in range(1, 4):
        harness.send(rtp(ROB_SSRC, b"rob-opus-%d" % seq, seq=seq))
    harness.send(rtp(ALEX_SSRC, b"alex-opus"))

    assert await harness.delivered() == [
        ("rob", bytes(FRAME_BYTES)),
        ("rob", bytes(FRAME_BYTES)),
        ("rob", bytes(FRAME_BYTES)),
        ("alex", bytes(FRAME_BYTES)),
    ]
    assert FakeDecoder.decoded == [b"rob-opus-1", b"rob-opus-2", b"rob-opus-3", b"alex-opus"]


async def test_strips_extension_body_and_padding_before_decoding(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.send(rtp(ROB_SSRC, b"opus", extension=b"\x10\xff\x00\x00", padding=3))

    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"opus"]


async def test_dave_frames_are_decrypted_for_the_mapped_user(start: Any) -> None:
    session = StubDaveSession(passthrough=True)
    harness: Harness = start([ROB_ID, ALEX_ID], session)
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.send(rtp(ROB_SSRC, dave(ROB_ID, b"rob-opus")))
    harness.send(rtp(ROB_SSRC, b"passthrough-opus", seq=2))

    assert [person for person, _ in await harness.delivered()] == ["rob", "rob"]
    assert session.calls == [ROB_ID, ROB_ID]
    assert FakeDecoder.decoded == [b"rob-opus", b"passthrough-opus"]


async def test_dave_frame_that_fails_to_decrypt_is_dropped(start: Any) -> None:
    harness: Harness = start([ROB_ID], StubDaveSession())
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.send(rtp(ROB_SSRC, dave(ALEX_ID, b"wrong-user")))

    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


async def test_unencrypted_frame_is_dropped_when_dave_passthrough_is_off(start: Any) -> None:
    harness: Harness = start([ROB_ID, ALEX_ID], StubDaveSession())
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.send(rtp(ROB_SSRC, b"unencrypted-opus"))

    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


@pytest.mark.parametrize("dave_on", [False, True])
async def test_stranger_on_unmapped_ssrc_is_dropped_not_credited_to_the_allowlisted_member(
    start: Any, dave_on: bool
) -> None:
    session = StubDaveSession() if dave_on else None
    harness: Harness = start([ROB_ID, STRANGER_ID], session)
    harness.send(rtp(ALEX_SSRC, b"stranger-opus"))
    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


@pytest.mark.parametrize("dave_on", [False, True])
async def test_unmapped_ssrc_is_inferred_when_the_channel_holds_only_the_bot_and_one_allowlisted_person(
    start: Any, dave_on: bool
) -> None:
    session = StubDaveSession() if dave_on else None
    harness: Harness = start([ROB_ID], session)
    payload = dave(ROB_ID, b"rob-opus") if dave_on else b"rob-opus"
    harness.send(rtp(ROB_SSRC, payload))
    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"rob-opus"]


async def test_another_bot_in_the_channel_prevents_inference(start: Any) -> None:
    # A music or text-to-speech bot's audio must not be credited to the only allowlisted person.
    harness: Harness = start([ROB_ID, STRANGER_ID], bot_ids=frozenset({STRANGER_ID}))
    harness.send(rtp(ALEX_SSRC, b"other-bot-opus"))
    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


async def test_a_member_missing_from_the_cache_still_prevents_inference(start: Any) -> None:
    harness: Harness = start([ROB_ID, STRANGER_ID], uncached_ids=frozenset({STRANGER_ID}))
    harness.send(rtp(ALEX_SSRC, b"stranger-opus"))
    assert await harness.delivered() == []


async def test_speaking_remaps_an_inferred_ssrc_to_the_stranger_who_is_then_dropped(start: Any) -> None:
    harness = start([ROB_ID])
    harness.send(rtp(ALEX_SSRC, b"inferred", seq=1))
    await harness.speaking(ALEX_SSRC, STRANGER_ID)
    harness.send(rtp(ALEX_SSRC, b"after-remap", seq=2))
    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"inferred"]


@pytest.mark.parametrize("dave_on", [False, True])
async def test_unmapped_ssrc_with_two_allowlisted_members_is_dropped(start: Any, dave_on: bool) -> None:
    session = StubDaveSession() if dave_on else None
    harness: Harness = start([ROB_ID, ALEX_ID], session)
    payload = dave(ROB_ID, b"rob-opus") if dave_on else b"rob-opus"

    harness.send(rtp(ROB_SSRC, payload))

    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []
    if session is not None:
        assert session.calls == []


async def test_speaking_after_drop_maps_the_ssrc(start: Any) -> None:
    harness: Harness = start([ROB_ID, ALEX_ID])
    harness.send(rtp(ALEX_SSRC, b"before"))
    await harness.speaking(ALEX_SSRC, ALEX_ID)
    harness.send(rtp(ALEX_SSRC, b"after", seq=2))

    assert [person for person, _ in await harness.delivered()] == ["alex"]
    assert FakeDecoder.decoded == [b"after"]


async def test_non_allowlisted_user_is_dropped(start: Any) -> None:
    harness: Harness = start([ROB_ID, STRANGER_ID])
    await harness.speaking(ALEX_SSRC, STRANGER_ID)

    harness.send(rtp(ALEX_SSRC, b"stranger-opus"))

    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


async def test_bot_own_ssrc_and_non_voice_packets_are_ignored(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.send(rtp(BOT_SSRC, b"echo"))
    not_voice = bytearray(rtp(ROB_SSRC, b"opus"))
    not_voice[1] = 0xC9  # an RTCP receiver report
    harness.send(bytes(not_voice))
    harness.send(b"\x80\x78short")

    assert await harness.delivered() == []


async def test_frame_decrypted_under_a_replaced_key_generation_is_dropped(start: Any) -> None:
    session = StubDaveSession()
    harness: Harness = start([ROB_ID], session)
    await harness.speaking(ROB_SSRC, ROB_ID)

    def rekey_mid_packet() -> None:
        # A SESSION_DESCRIPTION lands on the event loop while this packet is being decrypted.
        harness.connection.secret_key = list(NEW_KEY)
        harness.receiver.refresh_credentials("test")

    session.on_decrypt = rekey_mid_packet
    harness.send(rtp(ROB_SSRC, dave(ROB_ID, b"in-flight")))
    session.on_decrypt = lambda: None
    harness.send(rtp(ROB_SSRC, dave(ROB_ID, b"new-key"), key=NEW_KEY, seq=2))

    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"in-flight", b"new-key"]


async def test_voice_websocket_key_change_refreshes_credentials(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)

    harness.connection.secret_key = list(NEW_KEY)
    await harness.connection.hook(None, {"op": 4, "d": {}})
    harness.send(rtp(ROB_SSRC, b"new-key", key=NEW_KEY))

    assert [person for person, _ in await harness.delivered()] == ["rob"]


async def test_transport_decrypt_failure_streak_rereads_the_key(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    harness.connection.secret_key = list(NEW_KEY)  # rotated without the receiver seeing op 4

    for seq in range(1, REKEY_FAILURE_STREAK + 1):
        harness.send(rtp(ROB_SSRC, b"lost", key=NEW_KEY, seq=seq))
    harness.send(rtp(ROB_SSRC, b"recovered", key=NEW_KEY, seq=REKEY_FAILURE_STREAK + 1))

    assert FakeDecoder.decoded == [b"recovered"]


async def test_hook_wraps_the_original_on_state_and_live_websocket_and_stop_restores_it(
    permissions: PermissionChecker,
) -> None:
    seen: list[Any] = []

    async def original(ws: Any, msg: Any) -> None:
        seen.append(msg)

    connection = FakeConnection()
    connection.hook = original
    connection.ws = SimpleNamespace(_hook=original)
    receiver = VoiceReceiver(
        cast(discord.VoiceClient, FakeVoiceClient(connection, [ROB_ID])), permissions, lambda *_: None
    )

    receiver.start()
    assert connection.ws._hook is connection.hook is not original
    await connection.ws._hook(connection.ws, {"op": 5, "d": {"ssrc": ROB_SSRC, "user_id": ROB_ID}})
    assert len(seen) == 1

    receiver.stop()
    assert connection.hook is original
    assert connection.ws._hook is original
    assert connection.listeners == []


async def test_no_frames_after_stop(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    listener = harness.connection.listeners[0]

    listener(rtp(ROB_SSRC, b"in-flight"))
    harness.receiver.stop()
    listener(rtp(ROB_SSRC, b"late", seq=2))

    assert await harness.delivered() == []


async def test_frames_from_the_socket_thread_reach_the_callback_on_the_loop(start: Any) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    loop_thread = threading.get_ident()
    callback_threads: list[int] = []
    harness.receiver._on_frame = lambda *_: callback_threads.append(threading.get_ident())

    socket_thread = threading.Thread(target=harness.send, args=(rtp(ROB_SSRC, b"opus"),))
    socket_thread.start()
    socket_thread.join()
    await asyncio.sleep(0)

    assert callback_threads == [loop_thread]


async def test_unexpected_error_never_escapes_to_the_socket_reader(
    start: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    monkeypatch.setattr(harness.receiver, "_user_for_ssrc", lambda ssrc: 1 / 0)

    harness.send(rtp(ROB_SSRC, b"opus"))

    assert await harness.delivered() == []


def _real_opus_available() -> bool:
    try:
        opus.Decoder()
    except opus.OpusNotLoaded:
        return False
    return True


@pytest.mark.skipif(not _real_opus_available(), reason="libopus is not installed")
async def test_real_opus_frame_decodes_to_20_ms_of_48_khz_stereo(
    start: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    harness: Harness = start([ROB_ID])
    await harness.speaking(ROB_SSRC, ROB_ID)
    t = np.arange(960) / 48000
    tone = (3000 * np.sin(2 * np.pi * 440 * t)).astype("<i2")
    packet = opus.Encoder().encode(np.repeat(tone, 2).tobytes(), 960)

    harness.send(rtp(ROB_SSRC, packet))

    [(person, pcm)] = await harness.delivered()
    assert person == "rob"
    assert len(pcm) == FRAME_BYTES
