"""The WebSocket connection the provider adapters speak their JSON event protocols over.

``WireClient`` sends and receives JSON objects, one per text message, with the provider's API key as a
bearer token. It knows no event types: application-level ``{"type": "ping"}`` messages and events no
adapter understands are passed through like any other, and adapters ignore what they do not map.
``retry_connect`` reconnects with exponential backoff when the session decides to.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Literal, Self

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.protocol import State

logger = logging.getLogger(__name__)

# websockets logs every handshake header at DEBUG, the Authorization header included, so its
# connections log through a logger that never emits DEBUG, whatever the root level.
_websockets_logger = logging.getLogger(f"{__name__}.websockets")
_websockets_logger.setLevel(logging.INFO)

CloseReason = Literal["close_requested", "remote_hangup", "connection_lost"]
"""Why a connection ended: this side closed it, the provider closed it cleanly (with a close frame
and a normal close code), or it dropped. The names match GPT-Live's ``session.closed`` reasons."""

CONNECT_TIMEOUT_S = 10.0
RECONNECT_ATTEMPTS = 3
RECONNECT_FIRST_DELAY_S = 0.5


class WireError(Exception):
    """A connection to the provider could not be opened.

    ``reason`` is a short phrase the voice model can speak, such as "the voice provider is
    unreachable". ``status`` is the HTTP status the provider refused the upgrade with, if it did.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class WireClient:
    """One open JSON-over-WebSocket connection to a voice provider.

    Open it with ``WireClient.connect``; close it with ``close()`` or by using it as an async context
    manager. A closed client cannot be reopened: connect a new one.
    """

    def __init__(self, connection: ClientConnection) -> None:
        """Wrap an open connection; use ``WireClient.connect`` instead of calling this directly."""
        self._connection = connection
        self._close_requested = False
        self._close_reason: CloseReason | None = None

    @classmethod
    async def connect(
        cls,
        url: str,
        api_key: str,
        *,
        headers: Mapping[str, str] | None = None,
        open_timeout_s: float = CONNECT_TIMEOUT_S,
    ) -> Self:
        """Open a WebSocket to ``url`` (``wss://`` or ``ws://``) and return once the upgrade succeeds.

        Sends ``Authorization: Bearer <api_key>`` plus any extra ``headers``. Messages of any size are
        accepted. Proxies set in the environment are honored.

        Raises ``WireError`` when the provider refuses the upgrade ("the voice provider refused the
        connection (HTTP 401)", with ``status`` set), or cannot be reached or does not finish the
        handshake within ``open_timeout_s`` ("the voice provider is unreachable"). The key is never
        logged.
        """
        all_headers = {**(headers or {}), "Authorization": f"Bearer {api_key}"}
        try:
            connection = await ws_connect(
                url,
                additional_headers=all_headers,
                max_size=None,
                open_timeout=open_timeout_s,
                logger=_websockets_logger,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            logger.warning("Voice provider at %s refused the WebSocket upgrade: HTTP %d", url, status)
            raise WireError(f"the voice provider refused the connection (HTTP {status})", status) from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            # Described by type only: some handshake errors carry the request, and with it the key.
            logger.warning("Voice provider at %s is unreachable: %s", url, type(exc).__name__)
            raise WireError("the voice provider is unreachable") from exc
        logger.info("Connected to voice provider at %s", url)
        return cls(connection)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def close_reason(self) -> CloseReason | None:
        """Why the connection ended, once ``events()`` has seen it end or ``close()`` has run; None
        while it is open."""
        return self._close_reason

    async def send(self, event: Mapping[str, Any]) -> None:
        """Send ``event`` as one JSON text message.

        Once the connection has ended, the event is dropped instead: the end is reported once, by
        ``events()`` finishing, rather than by every send that races it.
        """
        try:
            await self._connection.send(json.dumps(event))
        except ConnectionClosed:
            logger.debug("Dropped a %s event sent after the connection ended", event.get("type"))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each JSON object the provider sends, in order, until the connection ends.

        Every object is passed through, including pings and unknown types. Binary messages and text
        that is not a JSON object are logged and skipped. When the iterator finishes, ``close_reason``
        says why. Only one ``events()`` iterator may run at a time.
        """
        try:
            async for message in self._connection:
                if isinstance(message, bytes):
                    logger.warning("Skipped a %d-byte binary message from the voice provider", len(message))
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    logger.warning("Skipped a voice provider message that is not JSON: %.80r", message)
                    continue
                if not isinstance(event, dict):
                    logger.warning(
                        "Skipped a voice provider message that is not a JSON object: %.80r", message
                    )
                    continue
                yield event
        except ConnectionClosed:
            pass  # recorded below from the close frame, like a clean end of iteration
        self._record_close()

    async def close(self) -> None:
        """Close the connection with a normal close handshake; safe to call more than once."""
        # A connection that already ended keeps the reason it ended with.
        self._close_requested = self._close_requested or self._connection.state is State.OPEN
        await self._connection.close()
        self._record_close()

    def _record_close(self) -> None:
        if self._close_reason is not None:
            return
        code = self._connection.close_code  # 1006 when the connection dropped without a close frame
        if self._close_requested:
            self._close_reason = "close_requested"
        elif code in (1000, 1001):
            self._close_reason = "remote_hangup"
        else:
            self._close_reason = "connection_lost"
        logger.info(
            "Voice provider connection ended (%s, close code %s %s)",
            self._close_reason,
            code,
            self._connection.close_reason or "",
        )


async def retry_connect[T](
    connect: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = RECONNECT_ATTEMPTS,
    first_delay_s: float = RECONNECT_FIRST_DELAY_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``connect`` until it succeeds, up to ``max_attempts`` times, and return its result.

    Waits before every attempt, starting at ``first_delay_s`` and doubling each time (0.5 s, 1 s, 2 s
    by default), so a provider that just dropped the connection gets a moment to recover. Only
    ``WireError`` is retried; any other exception propagates at once. ``sleep`` is how the waits
    happen (tests pass a recorder).

    Raises the last ``WireError`` when every attempt fails.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
    delay_s = first_delay_s
    for attempt in range(1, max_attempts):
        await sleep(delay_s)
        try:
            return await connect()
        except WireError as exc:
            logger.warning("Reconnect attempt %d of %d failed: %s", attempt, max_attempts, exc.reason)
        delay_s *= 2
    await sleep(delay_s)
    return await connect()
