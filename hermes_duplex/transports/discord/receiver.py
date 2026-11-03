# Adapted from hermes-agent's Discord VoiceReceiver:
#   https://github.com/NousResearch/hermes-agent/blob/6112c2adb9e11f69734a29be9d2696f345ffa06a/plugins/platforms/discord/adapter.py
#   class VoiceReceiver, lines 703-1222, commit 6112c2adb9e11f69734a29be9d2696f345ffa06a
#   MIT License, Copyright (c) 2025 Nous Research (full notice below and in NOTICE)
#
# Changes from the original: decoded frames stream to a callback instead of being buffered into
# utterances on silence; no pause while the bot plays; Discord users map to configured people and
# everyone else is dropped; an unmapped SSRC gets no DAVE passthrough window and is credited only
# to the channel's sole person, when that person is allowlisted; DAVE frames davey refuses,
# unencrypted ones included, are dropped; frames decrypted under a key generation that was replaced
# mid-packet are dropped.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
# OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Receives the voices in a Discord voice channel as 20 ms PCM frames, one stream per person.

discord.py can send DAVE (end-to-end encrypted) audio but not receive it, so this hooks the voice
connection's private internals: its UDP socket listener, and its voice websocket hook for SPEAKING
events and key changes. ``tests/unit/test_discord_internals_guard.py`` fails if a discord.py upgrade
removes any of them; discord.py is pinned for that reason.

Each packet is transport-decrypted (NaCl ``aead_xchacha20_poly1305_rtpsize``), then DAVE-decrypted
through ``davey`` when the call is end-to-end encrypted, then Opus-decoded with a decoder per SSRC.
"""

import asyncio
import logging
import struct
import threading
from collections.abc import Callable
from typing import Any, NamedTuple

import davey
import discord
import nacl.exceptions
import nacl.secret
from discord import opus
from discord.utils import MISSING

from ...people import PermissionChecker, Person

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
"""Sample rate of the PCM frames passed to ``on_frame``, in Hz (Discord's native rate)."""

CHANNELS = 2
"""Channel count of the PCM frames passed to ``on_frame`` (Discord sends stereo)."""

REKEY_FAILURE_STREAK = 25
"""Consecutive transport-decrypt failures after which the credentials are re-read."""

_RTP_HEADER_BYTES = 12
_RTP_VERSION = 2
_VOICE_PAYLOAD_TYPE = 0x78
_NONCE_SUFFIX_BYTES = 4

# Voice websocket opcodes the hook reacts to. discord.py has already applied each op to the
# connection state when the hook runs, so a refresh reads the new values.
_OP_SESSION_DESCRIPTION = 4  # new transport key; may replace the DAVE session
_OP_SPEAKING = 5  # {ssrc, user_id}: who sends on an SSRC
_OP_DAVE_EXECUTE_TRANSITION = 22  # the DAVE protocol version may have changed
_OP_DAVE_PREPARE_EPOCH = 24  # epoch 1 replaces the DAVE session
_CREDENTIAL_OPS = frozenset({_OP_SESSION_DESCRIPTION, _OP_DAVE_EXECUTE_TRANSITION, _OP_DAVE_PREPARE_EPOCH})

# Warnings for repeated per-packet failures: the first few, then one every this many.
_LOG_FIRST = 10
_LOG_EVERY = 250


class _Credentials(NamedTuple):
    """The decryption state of one key generation.

    The packet thread reads it with one reference load while refreshes swap it from other threads, so
    a packet never pairs a new transport key with an old DAVE session.
    """

    secret_key: bytes
    dave_session: davey.DaveSession | None
    dave_protocol_version: int
    """0 when DAVE is off or downgraded; the session object can outlive a downgrade."""

    generation: int
    """Increases each time any of the fields above changes."""


class VoiceReceiver:
    """Streams each allowlisted person's voice from one ``discord.VoiceClient`` to ``on_frame``.

    ``on_frame(person_id, pcm)`` gets every decoded frame as it arrives: ``person_id`` is the
    person's name from config and ``pcm`` is 20 ms of PCM16 at 48 kHz stereo (3840 bytes).
    It runs on the event loop that called ``start``, never on discord.py's socket thread, and never
    after ``stop`` has returned on that loop.

    A packet is dropped, never passed on, when its SSRC belongs to nobody on the allowlist; when its
    SSRC is not yet mapped to a user and the channel does not have exactly one allowlisted member to
    infer it from; when it fails to decrypt or decode; or when the key generation changed while it
    was being decrypted. A dropped packet is lost audio, which beats feeding a model the noise that
    ciphertext makes once it passes through Opus.
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        permissions: PermissionChecker,
        on_frame: Callable[[str, bytes], None],
    ) -> None:
        """``voice_client`` must be connected before ``start``."""
        self._vc = voice_client
        self._permissions = permissions
        self._on_frame = on_frame
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

        self._creds = _Credentials(b"", None, 0, 0)
        self._bot_ssrc = 0
        self._original_hook: Callable[..., Any] | None = None

        # Guards the SSRC map (written by the websocket hook, read on the packet thread) and
        # credential swaps (refreshed from both).
        self._lock = threading.Lock()
        self._ssrc_to_user: dict[int, int] = {}
        # Packet thread only: each SSRC is a separate Opus stream with its own decoder state.
        self._decoders: dict[int, opus.Decoder] = {}

        # Health counters, logged by ``stop``; written on the packet thread only.
        self._frames_ok = 0
        self._decrypt_failed = 0
        self._unattributed_dropped = 0
        self._stale_dropped = 0
        self._nacl_fail_streak = 0

    # --- Lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        """Hook the voice connection and start streaming. Call it on the event loop thread."""
        self._loop = asyncio.get_running_loop()
        conn = self._vc._connection
        self._resolve_credentials()
        self._install_hook(conn)
        self._running = True
        conn.add_socket_listener(self._on_packet)
        logger.info("Discord voice receiver started (bot ssrc=%d)", self._bot_ssrc)

    def stop(self) -> None:
        """Unhook the voice connection and forget all per-SSRC state. Safe to call more than once."""
        if not self._running:
            return
        self._running = False
        conn = self._vc._connection
        conn.remove_socket_listener(self._on_packet)
        self._remove_hook(conn)
        with self._lock:
            self._ssrc_to_user.clear()
        self._decoders.clear()
        logger.info(
            "Discord voice receiver stopped (frames ok=%d, decrypt failed=%d, unattributed dropped=%d, "
            "stale key dropped=%d)",
            self._frames_ok,
            self._decrypt_failed,
            self._unattributed_dropped,
            self._stale_dropped,
        )

    def map_ssrc(self, ssrc: int, user_id: int) -> None:
        """Record that Discord user ``user_id`` sends on ``ssrc``, as a SPEAKING event says."""
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def refresh_credentials(self, reason: str) -> None:
        """Re-read the decryption state from the live connection; ``reason`` is logged.

        Cheap and idempotent, so call it on every event that may change the keys (the websocket hook
        already does for key and DAVE transitions) without coalescing. Does nothing once stopped.
        """
        if not self._running:
            return
        self._resolve_credentials()
        self._nacl_fail_streak = 0
        logger.info(
            "Discord voice credentials refreshed (%s; generation=%d, dave=%s)",
            reason,
            self._creds.generation,
            "on" if self._creds.dave_session else "off",
        )

    def _resolve_credentials(self) -> None:
        """Snapshot the connection's keys, starting a new generation if any of them changed.

        Discord rotates the transport key on every voice (re)connect, and discord.py replaces the
        DAVE session object when DAVE (re)negotiates, so a one-time snapshot would silently decrypt
        nothing after either event.
        """
        conn = self._vc._connection
        secret_key = bytes(conn.secret_key)
        dave_session = conn.dave_session
        dave_protocol_version = int(conn.dave_protocol_version or 0)
        with self._lock:
            old = self._creds
            if (
                secret_key != old.secret_key
                or dave_session is not old.dave_session
                or dave_protocol_version != old.dave_protocol_version
            ):
                self._creds = _Credentials(
                    secret_key, dave_session, dave_protocol_version, old.generation + 1
                )
            self._bot_ssrc = conn.ssrc

    # --- Voice websocket hook --------------------------------------------------------------------

    def _install_hook(self, conn: Any) -> None:
        """Wrap the voice websocket hook to read SPEAKING events and key changes.

        discord.py passes ``conn.hook`` to each websocket it creates on (re)connect, so the wrapper
        goes on the connection state and on the live websocket.
        """
        original_hook = conn.hook
        self._original_hook = original_hook

        async def hook(ws: Any, msg: Any) -> None:
            if isinstance(msg, dict):
                self._on_voice_ws_message(msg)
            if original_hook is not None:
                await original_hook(ws, msg)

        conn.hook = hook
        if conn.ws is not MISSING:
            conn.ws._hook = hook

    def _remove_hook(self, conn: Any) -> None:
        """Put back the hook ``_install_hook`` wrapped, on the state and the live websocket."""
        conn.hook = self._original_hook
        if conn.ws is MISSING:
            return
        if self._original_hook is not None:
            conn.ws._hook = self._original_hook
        else:
            # Without a hook, discord.py's websocket falls back to its class's no-op ``_hook``.
            vars(conn.ws).pop("_hook", None)

    def _on_voice_ws_message(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == _OP_SPEAKING:
            data = msg.get("d") or {}
            ssrc, user_id = data.get("ssrc"), data.get("user_id")
            if ssrc and user_id:
                logger.debug("SPEAKING: ssrc=%s -> user=%s", ssrc, user_id)
                self.map_ssrc(int(ssrc), int(user_id))
        elif op in _CREDENTIAL_OPS:
            self.refresh_credentials(f"voice websocket op {op}")

    # --- Packets (discord.py's socket thread) ----------------------------------------------------

    def _on_packet(self, data: bytes) -> None:
        # An exception here would stop discord.py's socket reader for good, so none may escape.
        try:
            self._handle_packet(data)
        except Exception:
            logger.exception("Dropped a Discord voice packet after an unexpected error")

    def _handle_packet(self, data: bytes) -> None:
        if not self._running:
            return
        # One consistent key generation for this whole packet.
        creds = self._creds

        if len(data) < _RTP_HEADER_BYTES + _NONCE_SUFFIX_BYTES:
            return
        if data[0] >> 6 != _RTP_VERSION or data[1] & 0x7F != _VOICE_PAYLOAD_TYPE:
            return
        ssrc = struct.unpack_from(">I", data, 8)[0]
        if ssrc == self._bot_ssrc:
            return

        # RTP header size in rtpsize mode: the CSRCs and the extension preamble are authenticated
        # header; the extension body is inside the encrypted payload.
        csrc_count = data[0] & 0x0F
        has_extension = bool(data[0] & 0x10)
        has_padding = bool(data[0] & 0x20)
        header_size = _RTP_HEADER_BYTES + 4 * csrc_count + (4 if has_extension else 0)
        if len(data) < header_size + _NONCE_SUFFIX_BYTES:
            return
        extension_bytes = 0
        if has_extension:
            extension_words = struct.unpack_from(">H", data, header_size - 2)[0]
            extension_bytes = 4 * extension_words

        payload = self._transport_decrypt(creds, data, header_size)
        if payload is None:
            return
        if extension_bytes and len(payload) > extension_bytes:
            payload = payload[extension_bytes:]
        if has_padding:
            # RFC 3550 §5.1: the last byte counts the padding; left in, it corrupts DAVE and Opus.
            pad_bytes = payload[-1] if payload else 0
            if pad_bytes == 0 or pad_bytes > len(payload):
                return
            payload = payload[:-pad_bytes]
        if not payload:
            return

        user_id = self._user_for_ssrc(ssrc)
        person = self._permissions.by_discord_id(user_id) if user_id else None
        if person is None:
            self._unattributed_dropped += 1
            if self._unattributed_dropped % _LOG_EVERY == 1:
                logger.debug(
                    "Dropping voice from ssrc=%d (user=%s, not an allowlisted person; dropped=%d)",
                    ssrc,
                    user_id or "unmapped",
                    self._unattributed_dropped,
                )
            return

        if creds.dave_session is not None:
            payload = self._dave_decrypt(creds.dave_session, user_id, ssrc, payload)
            if payload is None:
                return

        pcm = self._decode(ssrc, payload)
        if pcm is None:
            return
        if self._creds.generation != creds.generation:
            # The keys changed while this packet was in flight; its plaintext cannot be trusted.
            self._stale_dropped += 1
            return
        self._frames_ok += 1
        self._deliver(person, pcm)

    def _transport_decrypt(self, creds: _Credentials, data: bytes, header_size: int) -> bytes | None:
        """The packet's payload after NaCl decryption, or None when it fails to decrypt.

        A sustained failure streak means the transport key rotated under us (voice reconnect or
        re-key), so the streak re-reads the credentials; the refresh resets the streak, so this
        retries every ``REKEY_FAILURE_STREAK`` packets while the failure persists.
        """
        header = bytes(data[:header_size])
        nonce = bytes(data[-_NONCE_SUFFIX_BYTES:]).ljust(nacl.secret.Aead.NONCE_SIZE, b"\0")
        encrypted = bytes(data[header_size:-_NONCE_SUFFIX_BYTES])
        try:
            payload = nacl.secret.Aead(creds.secret_key).decrypt(encrypted, header, nonce)
        except nacl.exceptions.CryptoError as exc:
            self._decrypt_failed += 1
            self._nacl_fail_streak += 1
            if self._decrypt_failed <= _LOG_FIRST or self._nacl_fail_streak % _LOG_EVERY == 0:
                logger.warning(
                    "Discord voice transport decrypt failed: %s (streak=%d)", exc, self._nacl_fail_streak
                )
            if self._nacl_fail_streak >= REKEY_FAILURE_STREAK:
                self.refresh_credentials("transport decrypt failure streak")
            return None
        self._nacl_fail_streak = 0
        return payload

    def _dave_decrypt(
        self, session: davey.DaveSession, user_id: int, ssrc: int, payload: bytes
    ) -> bytes | None:
        """The DAVE plaintext of ``payload``, or None when davey refuses it.

        An unencrypted frame is accepted only when davey's passthrough mode lets it through (davey
        then returns it as it is); with passthrough off davey raises, and the frame is dropped like
        any other that fails, since it is not bound to the sender's DAVE key.
        """
        try:
            return session.decrypt(user_id, davey.MediaType.audio, payload)
        except ValueError as exc:
            self._decrypt_failed += 1
            if self._decrypt_failed <= _LOG_FIRST or self._decrypt_failed % _LOG_EVERY == 0:
                logger.warning("Discord voice DAVE decrypt failed for ssrc=%d: %s", ssrc, exc)
            return None

    def _decode(self, ssrc: int, payload: bytes) -> bytes | None:
        """Decode one Opus packet with ``ssrc``'s decoder; None (and a fresh decoder) on failure."""
        decoder = self._decoders.get(ssrc)
        if decoder is None:
            decoder = self._decoders[ssrc] = opus.Decoder()
        try:
            return decoder.decode(payload, fec=False)
        except opus.OpusError as exc:
            self._decoders.pop(ssrc, None)
            logger.debug("Opus decode failed for ssrc=%d; reset its decoder: %s", ssrc, exc)
            return None

    def _user_for_ssrc(self, ssrc: int) -> int:
        """The Discord user id sending on ``ssrc``, or 0 when nobody can be credited.

        After the bot (re)joins, Discord may never resend SPEAKING for someone already talking. If
        the channel holds exactly one member besides this bot and that member is allowlisted, the
        SSRC must be theirs, so it is mapped to them. With anyone else in the channel (another
        person or another bot, such as a music or text-to-speech bot, allowlisted or not) the sender
        cannot be known, nobody is credited, and the packet is dropped. Members are read from the
        channel's voice states, so one missing from the member cache still counts.
        """
        with self._lock:
            user_id = self._ssrc_to_user.get(ssrc, 0)
            if user_id:
                return user_id
            bot_id = self._vc.user.id if self._vc.user else 0
            channel = self._vc.channel
            people = [uid for uid in channel.voice_states if uid != bot_id]
            if len(people) != 1 or people[0] not in self._permissions.allowlisted_discord_ids:
                return 0
            self._ssrc_to_user[ssrc] = people[0]
        logger.info("Mapped ssrc=%d -> user=%d (the only person in the channel)", ssrc, people[0])
        return people[0]

    def _deliver(self, person: Person, pcm: bytes) -> None:
        assert self._loop is not None  # set by start(), before the socket listener is added
        try:
            self._loop.call_soon_threadsafe(self._emit, person.name, pcm)
        except RuntimeError:
            # The loop has closed: the service is shutting down and nobody is listening.
            pass

    def _emit(self, person_id: str, pcm: bytes) -> None:
        # Runs on the event loop: drops frames that were in flight when stop() ran.
        if self._running:
            self._on_frame(person_id, pcm)
