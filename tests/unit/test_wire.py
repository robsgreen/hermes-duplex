"""WireClient and retry_connect against the fake provider WebSocket server."""

import json
import logging
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from hermes_duplex.providers.wire import WireClient, WireError, retry_connect
from tests.fakes.fake_ws_server import FakeWsServer

API_KEY = "sk-test-0123456789abcdef"
LARGE_BASE64 = "A" * 2_000_000  # over websockets' default 1 MiB message limit


class _Sleeps(list[float]):
    """A ``sleep`` stand-in that records each requested delay and returns at once."""

    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


async def test_connect_sends_bearer_key_and_extra_headers() -> None:
    async with (
        FakeWsServer([]) as server,
        await WireClient.connect(
            f"{server.url}/v1/live/sessions", API_KEY, headers={"OpenAI-Beta": "live=v1"}
        ),
    ):
        pass

    (connection,) = server.connections
    assert connection.path == "/v1/live/sessions"
    assert connection.headers["authorization"] == f"Bearer {API_KEY}"
    assert connection.headers["openai-beta"] == "live=v1"


async def test_json_round_trip_after_waiting_for_a_client_event() -> None:
    script = [
        {"wait_for": "session.start"},
        {"type": "session.started", "session": {"id": "s1"}},
        {"wait_for": "session.input_audio.append"},
        {"type": "session.output_audio.delta", "delta": LARGE_BASE64},
        {"close": {}},
    ]
    async with FakeWsServer(script) as server, await WireClient.connect(server.url, API_KEY) as client:
        await client.send({"type": "session.start", "session": {"model": "gpt-live-1"}})
        events = client.events()
        assert await anext(events) == {"type": "session.started", "session": {"id": "s1"}}
        await client.send({"type": "session.input_audio.append", "audio": LARGE_BASE64})
        assert [event async for event in events] == [
            {"type": "session.output_audio.delta", "delta": LARGE_BASE64}
        ]
        assert client.close_reason == "remote_hangup"

    assert [event["type"] for event in server.client_events] == [
        "session.start",
        "session.input_audio.append",
    ]
    assert server.client_events[0]["session"] == {"model": "gpt-live-1"}


async def test_pings_and_unknown_events_pass_through() -> None:
    script = [{"type": "ping"}, {"type": "brand.new.event", "x": 1}, {"close": {}}]
    async with FakeWsServer(script) as server, await WireClient.connect(server.url, API_KEY) as client:
        events = [event async for event in client.events()]

    assert events == [{"type": "ping"}, {"type": "brand.new.event", "x": 1}]


async def test_non_object_messages_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    # The fake only sends JSON objects, so a bare websockets server sends the junk.
    async def handler(websocket: ServerConnection) -> None:
        for message in ("not json", "[1, 2]", b"\x00\x01", json.dumps({"type": "info"})):
            await websocket.send(message)
        await websocket.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        host, port = next(iter(server.sockets)).getsockname()[:2]
        with caplog.at_level(logging.WARNING):
            async with await WireClient.connect(f"ws://{host}:{port}", API_KEY) as client:
                events = [event async for event in client.events()]

    assert events == [{"type": "info"}]
    assert len([r for r in caplog.records if r.message.startswith("Skipped")]) == 3


async def test_client_close_is_clean_and_idempotent() -> None:
    async with FakeWsServer([]) as server:
        client = await WireClient.connect(server.url, API_KEY)
        await client.send({"type": "session.close"})
        await server.wait_for_client_event("session.close")
        await client.close()
        await client.close()
        assert client.close_reason == "close_requested"
        assert [event async for event in client.events()] == []
        await client.send({"type": "dropped"})  # after close: dropped, not raised


async def test_drop_ends_events_as_connection_lost_and_next_connection_resumes_script() -> None:
    script = [{"type": "session.started"}, {"drop": True}, {"type": "session.started", "n": 2}, {"close": {}}]
    async with FakeWsServer(script) as server:
        async with await WireClient.connect(server.url, API_KEY) as first:
            assert [event async for event in first.events()] == [{"type": "session.started"}]
            assert first.close_reason == "connection_lost"
            await first.send({"type": "late"})  # the end was already reported by events()

        async with await WireClient.connect(server.url, API_KEY) as second:
            assert [event async for event in second.events()] == [{"type": "session.started", "n": 2}]

    assert len(server.connections) == 2


async def test_abnormal_close_code_is_connection_lost() -> None:
    script = [{"close": {"code": 1011, "reason": "internal error"}}]
    async with FakeWsServer(script) as server, await WireClient.connect(server.url, API_KEY) as client:
        assert [event async for event in client.events()] == []
        assert client.close_reason == "connection_lost"


async def test_refused_upgrade_raises_wire_error_with_status_and_never_logs_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(connection: ServerConnection, request: Request) -> Response:
        return Response(401, "Unauthorized", Headers(), b"bad key")

    async def handler(websocket: ServerConnection) -> None:
        raise AssertionError("the upgrade should have been refused")

    async with serve(handler, "127.0.0.1", 0, process_request=refuse) as server:
        host, port = next(iter(server.sockets)).getsockname()[:2]
        with caplog.at_level(logging.DEBUG), pytest.raises(WireError) as raised:
            await WireClient.connect(f"ws://{host}:{port}", API_KEY)

    assert raised.value.status == 401
    assert raised.value.reason == "the voice provider refused the connection (HTTP 401)"
    client_logs = [r.getMessage() for r in caplog.records if not r.name.startswith("websockets.server")]
    assert client_logs and not any(API_KEY in message for message in client_logs)


async def test_unreachable_provider_raises_wire_error(caplog: pytest.LogCaptureFixture) -> None:
    async with FakeWsServer([]) as server:
        url = server.url
    with caplog.at_level(logging.DEBUG), pytest.raises(WireError) as raised:
        await WireClient.connect(url, API_KEY, open_timeout_s=2)

    assert raised.value.reason == "the voice provider is unreachable"
    assert raised.value.status is None
    assert API_KEY not in caplog.text


def test_fake_server_rejects_malformed_steps(tmp_path: Path) -> None:
    script = tmp_path / "bad.jsonl"
    script.write_text('{"type": "session.started"}\n\n{"wait_for": "a", "drop": true}\n')
    with pytest.raises(ValueError, match="step 2"):
        FakeWsServer(script)


async def test_retry_connect_backs_off_exponentially_until_success() -> None:
    sleeps = _Sleeps()
    outcomes: list[Exception | str] = [WireError("down"), WireError("down"), "connected"]

    async def attempt() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert await retry_connect(attempt, sleep=sleeps) == "connected"
    assert sleeps == [0.5, 1.0, 2.0]


async def test_retry_connect_raises_the_last_error_after_max_attempts() -> None:
    sleeps = _Sleeps()
    attempts: list[int] = []

    async def attempt() -> None:
        attempts.append(len(attempts) + 1)
        raise WireError(f"down {len(attempts)}")

    with pytest.raises(WireError, match="down 4"):
        await retry_connect(attempt, max_attempts=4, first_delay_s=0.25, sleep=sleeps)
    assert attempts == [1, 2, 3, 4]
    assert sleeps == [0.25, 0.5, 1.0, 2.0]


async def test_retry_connect_does_not_retry_other_errors() -> None:
    sleeps = _Sleeps()

    async def attempt() -> None:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await retry_connect(attempt, sleep=sleeps)
    assert sleeps == [0.5]


async def test_retry_connect_reconnects_to_a_live_server() -> None:
    async with FakeWsServer([{"type": "session.started"}, {"close": {}}]) as server:
        client = await retry_connect(lambda: WireClient.connect(server.url, API_KEY), sleep=_Sleeps())
        async with client:
            assert [event async for event in client.events()] == [{"type": "session.started"}]
