"""A ``websockets`` fake of a voice provider's WebSocket endpoint that replays a JSONL script.

Script format: one JSON object per line (blank lines are skipped), run strictly in order. Each line is
one step:

- A server event, any object with a ``"type"`` key, such as ``{"type": "session.started"}``: sent to
  the client as is. Recorded provider sessions replay unchanged.
- ``{"wait_for": "<client event type>"}``: wait until the client has sent an event of that type on this
  connection, after the event the previous ``wait_for`` matched, then go on.
- ``{"drop": true}``: abort the connection without a close handshake, as a network failure would.
- ``{"close": {"code": 1000, "reason": "bye"}}``: close the connection cleanly; both fields are
  optional (default 1000, "").

A drop or close ends the connection; the remaining steps resume on the next connection. Once the
script runs out, the server keeps the connection open and goes on recording until the client closes;
``send`` then pushes further server events whenever the test chooses.

Usage::

    async with FakeWsServer([{"wait_for": "session.start"}, {"type": "session.started"}]) as server:
        client = await WireClient.connect(server.url, "key")
"""

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

_STEP_KEYS = ("type", "wait_for", "drop", "close")


@dataclass
class FakeConnection:
    """One client connection the server accepted: its request path and headers (header names
    lowercased), and every JSON event the client sent on it, in order."""

    path: str
    headers: Mapping[str, str]
    events: list[dict[str, Any]] = field(default_factory=list)


class FakeWsServer:
    """Serves the script on ``ws://127.0.0.1:<free port>`` while used as an async context manager.

    ``script`` is a JSONL file or the steps themselves. A malformed step raises ``ValueError`` here,
    before anything is served.
    """

    def __init__(self, script: Path | Iterable[Mapping[str, Any]]) -> None:
        steps = _load(script) if isinstance(script, Path) else [dict(step) for step in script]
        for number, step in enumerate(steps, 1):
            _check(number, step)
        self._steps = steps
        self._next_step = 0
        self.connections: list[FakeConnection] = []
        self._sockets: list[ServerConnection] = []
        self._received = asyncio.Condition()
        self._server: Server | None = None

    async def __aenter__(self) -> Self:
        self._server = await serve(self._handle, "127.0.0.1", 0, max_size=None)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        """The server's ``ws://`` base URL; any path below it is accepted."""
        if self._server is None:
            raise RuntimeError("the server is not running")
        host, port = next(iter(self._server.sockets)).getsockname()[:2]
        return f"ws://{host}:{port}"

    @property
    def client_events(self) -> list[dict[str, Any]]:
        """Every event clients sent, over all connections, in order."""
        return [event for connection in self.connections for event in connection.events]

    async def wait_for_client_event(self, event_type: str, timeout_s: float = 5.0) -> dict[str, Any]:
        """Return the first event of ``event_type`` any client sent, waiting up to ``timeout_s`` for
        one to arrive; raises ``TimeoutError`` when none does."""
        async with asyncio.timeout(timeout_s), self._received:
            while True:
                for event in self.client_events:
                    if event.get("type") == event_type:
                        return event
                await self._received.wait()

    async def send(self, *events: Mapping[str, Any]) -> None:
        """Send ``events`` to the latest connection as they are, after its script steps have run out."""
        if not self._sockets or self._next_step < len(self._steps):
            raise RuntimeError("send needs an open connection whose script has run out")
        for event in events:
            await self._sockets[-1].send(json.dumps(event))

    async def _handle(self, websocket: ServerConnection) -> None:
        request = websocket.request
        assert request is not None
        connection = FakeConnection(
            path=request.path, headers={name.lower(): value for name, value in request.headers.raw_items()}
        )
        self.connections.append(connection)
        self._sockets.append(websocket)
        reader = asyncio.create_task(self._record(websocket, connection))
        matched = 0  # client events of this connection already consumed by ``wait_for`` steps
        try:
            while self._next_step < len(self._steps):
                step = self._steps[self._next_step]
                self._next_step += 1
                if "wait_for" in step:
                    matched = await self._wait_for(connection, step["wait_for"], matched)
                elif "drop" in step:
                    websocket.transport.abort()
                    return
                elif "close" in step:
                    await websocket.close(step["close"].get("code", 1000), step["close"].get("reason", ""))
                    return
                else:
                    await websocket.send(json.dumps(step))
            await reader
        except ConnectionClosed:
            pass
        finally:
            reader.cancel()

    async def _record(self, websocket: ServerConnection, connection: FakeConnection) -> None:
        try:
            async for message in websocket:
                async with self._received:
                    connection.events.append(json.loads(message))
                    self._received.notify_all()
        except ConnectionClosed:
            pass

    async def _wait_for(self, connection: FakeConnection, event_type: str, matched: int) -> int:
        """Wait for an event of ``event_type`` at or after index ``matched`` of the connection's events;
        return the index just past it."""
        async with self._received:
            while True:
                for index in range(matched, len(connection.events)):
                    if connection.events[index].get("type") == event_type:
                        return index + 1
                await self._received.wait()


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _check(number: int, step: Mapping[str, Any]) -> None:
    keys = [key for key in _STEP_KEYS if key in step]
    if len(keys) != 1:
        raise ValueError(f"script step {number} needs exactly one of {', '.join(_STEP_KEYS)}: {step}")
    if "wait_for" in step and not isinstance(step["wait_for"], str):
        raise ValueError(f"script step {number}: wait_for must be a client event type")
    if "close" in step and not isinstance(step["close"], Mapping):
        raise ValueError(f"script step {number}: close must be an object with optional code and reason")
