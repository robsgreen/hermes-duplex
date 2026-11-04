"""The web transport: an aiohttp app serving the browser page and the audio WebSocket.

Routes:

- ``GET /`` serves the page; ``GET /static/<file>`` serves its scripts.
- ``GET /ws`` upgrades to the audio WebSocket after checking the person's web token. The token comes
  from the ``token`` query parameter or from ``Sec-WebSocket-Protocol: bearer, <token>``; a missing
  or unknown token gets HTTP 401 before the upgrade.

On the socket, binary messages carry PCM16 24 kHz mono audio both ways. The server also sends JSON
text messages: ``{"type": "interrupt"}`` tells the page to discard its queued playback, and
``{"type": "status", ...}`` carries transcript lines and the task list.
"""

import asyncio
import itertools
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, hdrs, web

from ...audio.frames import FRAME_MS, FrameCarry, rms_dbfs
from ...config import AudioConfig, WebConfig
from ...people import PermissionChecker, Person
from ..base import Conversation, InputFrame

logger = logging.getLogger(__name__)

RATE = 24000
"""Sample rate of the audio the socket carries in both directions, in Hz."""

BEARER_PROTOCOL = "bearer"
"""WebSocket subprotocol a browser offers, followed by its token, to sign in without a query string."""

STATIC_DIR = Path(__file__).parent / "static"

# The page loads only its own files (plus its inline stylesheet), so injected markup cannot load scripts.
_PAGE_HEADERS = {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'"}

# A WebSocket close reason must fit in a 125-byte control frame along with the 2-byte code.
_MAX_CLOSE_REASON_BYTES = 123


class WebConversation:
    """One browser connected to ``/ws``, signed in as one person.

    Input: binary messages are re-chunked into 20 ms ``InputFrame``s. ``t_ms`` counts the frames
    received on this socket (× 20, from 0); ``voiced_speakers`` is the person when the frame's RMS
    level is above ``audio.gate_threshold_dbfs``, and empty otherwise.

    Output: ``play`` sends the audio to the page at once; the session's pacer keeps what reaches it
    within its lead, and ``flush_playback`` tells the page to discard it.

    After the socket closes, ``play``, ``flush_playback``, and ``notify`` do nothing. Use it from one
    event loop.
    """

    def __init__(
        self, conversation_id: str, person: Person, socket: web.WebSocketResponse, audio: AudioConfig
    ) -> None:
        self.id = conversation_id
        self.signed_in = person
        """The signed-in person's entry in config's ``people``."""
        self.person = person.name
        self._socket = socket
        self._gate_threshold_dbfs = audio.gate_threshold_dbfs
        self._carry = FrameCarry(RATE)
        self._frames: asyncio.Queue[InputFrame | None] = asyncio.Queue()
        self._frames_received = 0

    async def frames(self) -> AsyncIterator[InputFrame]:
        """Yield the input frames in order until the socket closes. Iterate it from one consumer."""
        while (frame := await self._frames.get()) is not None:
            yield frame

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        """Queue ``pcm16_mono`` for the page; ``rate`` must be 24000 (raises ``ValueError`` otherwise)."""
        if rate != RATE:
            raise ValueError(f"the web transport plays {RATE} Hz audio, got {rate} Hz")
        await self._send(pcm16_mono)

    async def flush_playback(self) -> None:
        """Tell the page to discard its queued audio."""
        await self._send({"type": "interrupt"})

    async def notify(self, event: dict[str, Any]) -> None:
        """Send ``event`` to the page as ``{"type": "status", **event}``.

        The page renders ``{"transcript": {"speaker": str, "text": str}}`` as a transcript line,
        ``{"tasks": [{"number": int, "profile": str, "state": str, "text": str}, ...]}`` as the task
        list (replacing the previous one), and ``{"message": str}`` as its status line.
        """
        await self._send({**event, "type": "status"})

    async def close(self, reason: str) -> None:
        """Close the socket with ``reason`` as its close reason and end ``frames()``. Idempotent."""
        self._frames.put_nowait(None)
        if not self._socket.closed:
            # Cut on a character boundary: the page rejects a close reason that is not valid UTF-8.
            shortened = reason.encode()[:_MAX_CLOSE_REASON_BYTES].decode(errors="ignore")
            await self._socket.close(code=WSCloseCode.OK, message=shortened.encode())

    async def receive(self) -> None:
        """Read the socket until it closes, turning audio into input frames.

        Ends ``frames()`` on return.
        """
        try:
            async for message in self._socket:
                if message.type == WSMsgType.BINARY:
                    self._add_input(message.data)
                else:
                    logger.debug("ignoring %s message on web conversation %s", message.type.name, self.id)
        finally:
            self._frames.put_nowait(None)

    def _add_input(self, chunk: bytes) -> None:
        for pcm in self._carry.push(chunk):
            voiced = rms_dbfs(pcm) > self._gate_threshold_dbfs
            self._frames.put_nowait(
                InputFrame(
                    pcm16_mono=pcm,
                    rate=RATE,
                    voiced_speakers=frozenset({self.person}) if voiced else frozenset(),
                    t_ms=self._frames_received * FRAME_MS,
                )
            )
            self._frames_received += 1

    async def _send(self, message: bytes | dict[str, Any]) -> None:
        if self._socket.closed:
            return
        try:
            if isinstance(message, bytes):
                await self._socket.send_bytes(message)
            else:
                await self._socket.send_str(json.dumps(message))
        except ConnectionResetError:
            # The page went away mid-send; the receive loop sees the close and ends the conversation.
            pass


class WebTransport:
    """Serves the page and the audio WebSocket on ``config.host``:``config.port``.

    Each signed-in socket becomes a ``WebConversation`` passed to ``on_conversation``, which runs
    alongside the socket; the conversation ends when the page disconnects, when it is closed, or
    when ``on_conversation`` raises.
    """

    name = "web"

    def __init__(self, config: WebConfig, audio: AudioConfig, people: PermissionChecker) -> None:
        self._config = config
        self._audio = audio
        self._people = people
        self._on_conversation: Callable[[Conversation], Awaitable[None]] | None = None
        self._runner: web.AppRunner | None = None
        self._conversations: set[WebConversation] = set()
        self._conversation_numbers = itertools.count(1)

    @property
    def port(self) -> int:
        """The TCP port the server listens on; differs from ``config.port`` when that is 0.

        Raises ``RuntimeError`` before ``start``.
        """
        if self._runner is None:
            raise RuntimeError("the web transport is not started")
        return self._runner.addresses[0][1]

    async def start(self, on_conversation: Callable[[Conversation], Awaitable[None]]) -> None:
        """Start listening; returns once the port is bound."""
        self._on_conversation = on_conversation
        app = web.Application()
        app.router.add_get("/", self._page)
        app.router.add_get("/ws", self._websocket)
        app.router.add_static("/static/", STATIC_DIR)
        # No access log: its request line would hold a ``?token=`` query string.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._config.host, self._config.port).start()
        logger.info("web transport listening on %s:%d", self._config.host, self.port)

    async def stop(self) -> None:
        """Close every open conversation and stop the server. Does nothing if not started."""
        for conversation in list(self._conversations):
            await conversation.close("server stopping")
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _page(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers=_PAGE_HEADERS)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        person = self._authenticate(request)
        # PCM barely compresses, so per-message deflate would only cost CPU.
        socket = web.WebSocketResponse(protocols=(BEARER_PROTOCOL,), compress=False)
        await socket.prepare(request)

        conversation = WebConversation(
            f"web-{person.name}-{next(self._conversation_numbers)}", person, socket, self._audio
        )
        self._conversations.add(conversation)
        logger.info("web conversation %s started", conversation.id)
        consumer = asyncio.create_task(self._run_consumer(conversation))
        try:
            await conversation.receive()
        finally:
            await conversation.close("disconnected")
            self._conversations.discard(conversation)
            # The consumer sees frames() end; its own failure or cancellation was already handled.
            await asyncio.gather(consumer, return_exceptions=True)
            logger.info("web conversation %s ended", conversation.id)
        return socket

    def _authenticate(self, request: web.Request) -> Person:
        """Return the person the request's token belongs to, or raise HTTP 401."""
        token = request.query.get("token")
        if token is None:
            offered = [
                part.strip() for part in request.headers.get(hdrs.SEC_WEBSOCKET_PROTOCOL, "").split(",")
            ]
            if len(offered) == 2 and offered[0] == BEARER_PROTOCOL:
                token = offered[1]
        person = self._people.by_web_token(token) if token else None
        if person is None:
            raise web.HTTPUnauthorized(headers={hdrs.WWW_AUTHENTICATE: "Bearer"})
        return person

    async def _run_consumer(self, conversation: WebConversation) -> None:
        assert self._on_conversation is not None
        try:
            await self._on_conversation(conversation)
        except Exception:
            logger.exception("web conversation %s failed", conversation.id)
            await conversation.close("internal error")
