"""WebTransport: token auth before the upgrade, audio in and out, control frames, and the static page."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import numpy as np
import pytest
import yaml

from hermes_duplex.config import AudioConfig, WebConfig, load_config
from hermes_duplex.people import PermissionChecker
from hermes_duplex.transports.base import Conversation, InputFrame
from hermes_duplex.transports.web.server import WebConversation, WebTransport

RATE = 24000
BYTES_PER_MS = RATE * 2 // 1000  # 48
ENV = {"TOKEN_ROB": "rob-web-token", "TOKEN_ALEX": "alex-web-token"}
RECEIVE_TIMEOUT_S = 2


def _sine(ms: int, amplitude: float = 3277.0) -> bytes:
    """A 440 Hz sine at about -20 dBFS by default."""
    t = np.arange(ms * RATE // 1000) / RATE
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes()


def _silence(ms: int) -> bytes:
    return bytes(ms * BYTES_PER_MS)


class Server:
    """A started transport on a free port, and the conversations it handed over."""

    def __init__(self, transport: WebTransport) -> None:
        self.transport = transport
        self.conversations: asyncio.Queue[WebConversation] = asyncio.Queue()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.transport.port}"

    async def on_conversation(self, conversation: Conversation) -> None:
        assert isinstance(conversation, WebConversation)
        await self.conversations.put(conversation)

    async def next_conversation(self) -> WebConversation:
        return await asyncio.wait_for(self.conversations.get(), RECEIVE_TIMEOUT_S)


def _people(tmp_path: Path) -> PermissionChecker:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {}},
        "people": {
            "rob": {"web_token_env": "TOKEN_ROB", "profiles": ["*"]},
            "alex": {"web_token_env": "TOKEN_ALEX", "profiles": ["general"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PermissionChecker(load_config(path, env=ENV))


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[Server]:
    transport = WebTransport(WebConfig(enabled=True, port=0), AudioConfig(), _people(tmp_path))
    server = Server(transport)
    await transport.start(server.on_conversation)
    yield server
    await transport.stop()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def _connect(server: Server, http: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    return await http.ws_connect(f"{server.base_url}/ws", protocols=("bearer", ENV["TOKEN_ROB"]))


async def _receive(socket: aiohttp.ClientWebSocketResponse) -> aiohttp.WSMessage:
    return await socket.receive(timeout=RECEIVE_TIMEOUT_S)


async def _receive_until_interrupt(socket: aiohttp.ClientWebSocketResponse) -> None:
    while (message := await _receive(socket)).type == aiohttp.WSMsgType.BINARY:
        pass
    assert json.loads(message.data) == {"type": "interrupt"}


async def _collect(conversation: WebConversation, count: int) -> list[InputFrame]:
    frames = []
    async for frame in conversation.frames():
        frames.append(frame)
        if len(frames) == count:
            break
    return frames


def test_default_address_is_localhost_8765() -> None:
    assert (WebConfig().host, WebConfig().port) == ("127.0.0.1", 8765)


@pytest.mark.parametrize(
    ("query", "protocols"),
    [
        ("", ()),
        ("?token=wrong-token", ()),
        ("?token=", ()),
        ("", ("bearer", "wrong-token")),
        ("", ("bearer",)),
        ("", ("other", ENV["TOKEN_ROB"])),
    ],
    ids=["missing", "invalid-query", "empty-query", "invalid-bearer", "bearer-without-token", "not-bearer"],
)
async def test_upgrade_without_a_valid_token_is_401(
    server: Server, http: aiohttp.ClientSession, query: str, protocols: tuple[str, ...]
) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
        await http.ws_connect(f"{server.base_url}/ws{query}", protocols=protocols)

    assert refused.value.status == 401
    assert server.conversations.empty()


async def test_query_tokens_never_reach_the_logs(
    server: Server, http: aiohttp.ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with http.ws_connect(f"{server.base_url}/ws?token={ENV['TOKEN_ALEX']}"):
        await server.next_conversation()
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await http.ws_connect(f"{server.base_url}/ws?token=wrong-secret-token")
    await asyncio.sleep(0.1)  # the access line of a WebSocket request is written when it ends

    assert caplog.records, "the requests were logged"
    assert ENV["TOKEN_ALEX"] not in caplog.text
    assert "wrong-secret-token" not in caplog.text


async def test_plain_get_on_ws_without_a_token_is_401(server: Server, http: aiohttp.ClientSession) -> None:
    async with http.get(f"{server.base_url}/ws") as response:
        assert response.status == 401


async def test_upgrade_with_query_token_signs_in_that_person(
    server: Server, http: aiohttp.ClientSession
) -> None:
    async with http.ws_connect(f"{server.base_url}/ws?token={ENV['TOKEN_ALEX']}") as socket:
        conversation = await server.next_conversation()

        assert conversation.person.name == "alex"
        assert socket.protocol is None


async def test_upgrade_with_bearer_protocol_echoes_bearer(
    server: Server, http: aiohttp.ClientSession
) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        assert conversation.person.name == "rob"
        assert conversation.id.startswith("web-rob-")
        assert socket.protocol == "bearer"


async def test_binary_in_becomes_20_ms_frames_with_t_ms_and_voiced_flag(
    server: Server, http: aiohttp.ClientSession
) -> None:
    audio = _sine(40) + _silence(40)
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()
        # Odd chunk sizes: frames must not depend on how the page splits its messages.
        for start in range(0, len(audio), 700):
            await socket.send_bytes(audio[start : start + 700])

        frames = await asyncio.wait_for(_collect(conversation, 4), RECEIVE_TIMEOUT_S)

    assert [frame.t_ms for frame in frames] == [0, 20, 40, 60]
    assert [frame.voiced_speakers for frame in frames] == [
        frozenset({"rob"}),
        frozenset({"rob"}),
        frozenset(),
        frozenset(),
    ]
    assert all(frame.rate == RATE and len(frame.pcm16_mono) == 20 * BYTES_PER_MS for frame in frames)
    assert b"".join(frame.pcm16_mono for frame in frames) == audio


async def test_quiet_input_below_the_gate_is_not_voiced(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()
        await socket.send_bytes(_sine(20, amplitude=100.0))  # about -50 dBFS, under the -35 dBFS gate

        (frame,) = await asyncio.wait_for(_collect(conversation, 1), RECEIVE_TIMEOUT_S)

    assert frame.voiced_speakers == frozenset()


async def test_play_sends_binary_pcm(server: Server, http: aiohttp.ClientSession) -> None:
    audio = _sine(100)
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        await conversation.play(audio, RATE)
        message = await _receive(socket)

    assert message.type == aiohttp.WSMsgType.BINARY
    assert message.data == audio


async def test_play_rejects_other_rates(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http):
        conversation = await server.next_conversation()

        with pytest.raises(ValueError, match="24000"):
            await conversation.play(_sine(20), 48000)


async def test_flush_playback_sends_interrupt_after_the_audio(
    server: Server, http: aiohttp.ClientSession
) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()
        await conversation.play(_sine(200), RATE)

        await conversation.flush_playback()
        await _receive_until_interrupt(socket)

        # The page discards what it queued; nothing more arrives.
        with pytest.raises(TimeoutError):
            await socket.receive(timeout=0.2)


async def test_flush_with_nothing_played_sends_interrupt(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        await conversation.flush_playback()
        assert json.loads((await _receive(socket)).data) == {"type": "interrupt"}


async def test_notify_sends_a_status_frame(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        await conversation.notify({"transcript": {"speaker": "rob", "text": "how is my portfolio doing"}})
        await conversation.notify(
            {"tasks": [{"number": 1, "profile": "general", "state": "running", "text": "x"}]}
        )

        transcript = json.loads((await _receive(socket)).data)
        task_list = json.loads((await _receive(socket)).data)

    assert transcript == {
        "type": "status",
        "transcript": {"speaker": "rob", "text": "how is my portfolio doing"},
    }
    assert task_list["type"] == "status"
    assert task_list["tasks"][0]["profile"] == "general"


async def test_browser_disconnect_ends_frames(server: Server, http: aiohttp.ClientSession) -> None:
    socket = await _connect(server, http)
    conversation = await server.next_conversation()

    await socket.close()

    assert await asyncio.wait_for(_collect(conversation, 1), RECEIVE_TIMEOUT_S) == []
    # Output after the page left is dropped silently.
    await conversation.play(_sine(20), RATE)
    await conversation.notify({"message": "late"})
    await conversation.flush_playback()


async def test_close_sends_reason_and_ends_frames(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        await conversation.close("provider unavailable")
        message = await _receive(socket)

    assert message.type == aiohttp.WSMsgType.CLOSE
    assert message.extra == "provider unavailable"
    assert await asyncio.wait_for(_collect(conversation, 1), RECEIVE_TIMEOUT_S) == []


async def test_stop_closes_open_conversations(server: Server, http: aiohttp.ClientSession) -> None:
    async with await _connect(server, http) as socket:
        conversation = await server.next_conversation()

        await server.transport.stop()
        message = await _receive(socket)

    assert message.type == aiohttp.WSMsgType.CLOSE
    assert await asyncio.wait_for(_collect(conversation, 1), RECEIVE_TIMEOUT_S) == []


async def test_failing_consumer_closes_the_socket(tmp_path: Path, http: aiohttp.ClientSession) -> None:
    async def fail(conversation: Conversation) -> None:
        raise RuntimeError("boom")

    transport = WebTransport(WebConfig(port=0), AudioConfig(), _people(tmp_path))
    await transport.start(fail)
    try:
        url = f"http://127.0.0.1:{transport.port}/ws"
        async with http.ws_connect(url, protocols=("bearer", ENV["TOKEN_ROB"])) as socket:
            message = await _receive(socket)
    finally:
        await transport.stop()

    assert message.type == aiohttp.WSMsgType.CLOSE
    assert message.extra == "internal error"


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/worklet.js"])
async def test_static_files_are_served(server: Server, http: aiohttp.ClientSession, path: str) -> None:
    async with http.get(f"{server.base_url}{path}") as response:
        body = await response.text()

    assert response.status == 200
    assert body.strip()


async def test_page_has_token_entry_and_loads_only_its_own_scripts(
    server: Server, http: aiohttp.ClientSession
) -> None:
    async with http.get(f"{server.base_url}/") as response:
        page = await response.text()

    assert response.content_type == "text/html"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert 'id="token"' in page and 'id="connect"' in page and 'id="disconnect"' in page
    assert "http://" not in page and "https://" not in page
