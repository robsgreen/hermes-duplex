"""HermesApiClient against the fake Hermes API server: request shapes, retries, errors, and events."""

import logging
import socket
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from hermes_duplex.dispatch.hermes_api import HermesApiClient, HermesApiError, RunEvent, RunStart, run_event
from hermes_duplex.providers.base import ModelChoice
from tests.fakes.fake_hermes_api import (
    FakeHermesApi,
    RunScript,
    Subscription,
    admission_queued,
    draining,
    idempotency_conflict,
    refusal,
    session_busy,
    storage_unavailable,
)

KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
TASK_UUID = "6f1c1f4e-8f0e-4c4b-9a35-3f8f3c1f2b10"
SESSION_ID = "duplex-rob-stock-picker"


class _Sleeps(list[float]):
    """A ``sleep`` stand-in that records each requested delay and returns at once."""

    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
async def fake() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as api:
        yield api


@pytest.fixture
def sleeps() -> _Sleeps:
    return _Sleeps()


@pytest.fixture
async def client(fake: FakeHermesApi, sleeps: _Sleeps) -> AsyncIterator[HermesApiClient]:
    async with HermesApiClient(fake.api_base, KEYS.get, sleep=sleeps) as api_client:
        yield api_client


async def _start(client: HermesApiClient, model: ModelChoice | None = None) -> RunStart:
    return await client.start_run("stock-picker", "how's my portfolio", SESSION_ID, model, "high", TASK_UUID)


async def _collect(client: HermesApiClient, run_id: str, profile: str = "stock-picker") -> list[RunEvent]:
    return [event async for event in client.events(profile, run_id)]


def _kinds(events: list[RunEvent]) -> list[tuple[str, str | None]]:
    return [(event.kind, event.text) for event in events]


async def test_start_run_sends_profile_key_idempotency_key_and_effort(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    started = await _start(client, ModelChoice(provider="anthropic", model="claude-x"))

    assert started == RunStart(run_id=next(iter(fake.runs)), queued=False)
    [request] = fake.requests
    assert (request.method, request.path) == ("POST", "/p/stock-picker/v1/runs")
    assert request.headers["Authorization"] == f"Bearer {KEYS['stock-picker']}"
    assert request.headers["Idempotency-Key"] == TASK_UUID
    assert request.body == {
        "input": "how's my portfolio",
        "session_id": SESSION_ID,
        "model": "claude-x",
        "provider": "anthropic",
        "model_options": {"reasoning": {"effort": "high"}},
    }


async def test_start_run_omits_model_and_provider_without_a_model_choice(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    await _start(client, None)

    assert fake.requests[0].body == {
        "input": "how's my portfolio",
        "session_id": SESSION_ID,
        "model_options": {"reasoning": {"effort": "high"}},
    }


async def test_profile_without_a_key_fails_as_not_configured_without_a_request(fake: FakeHermesApi) -> None:
    async with HermesApiClient(fake.api_base, {"general": KEYS["general"]}.get) as client:
        with pytest.raises(HermesApiError, match="not configured"):
            await _start(client)
        with pytest.raises(HermesApiError, match="not configured"):
            await _collect(client, "run_1")
        with pytest.raises(HermesApiError, match="not configured"):
            await client.stop("stock-picker", "run_1")

    assert fake.requests == []


async def test_session_busy_is_retried_with_the_same_session_and_key_and_the_run_proceeds(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.start_replies = [session_busy(), session_busy()]

    started = await _start(client)
    events = await _collect(client, str(started.run_id))

    posts = fake.requests_to("POST", "/v1/runs")
    assert len(posts) == 3
    assert {post.body["session_id"] for post in posts} == {SESSION_ID}
    assert {post.headers["Idempotency-Key"] for post in posts} == {TASK_UUID}
    assert all(post.body == posts[0].body for post in posts)
    assert sleeps == [1.0, 1.0]
    assert _kinds(events) == [("started", None), ("completed", "done")]


async def test_session_busy_fails_after_five_retries(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.start_replies = [session_busy() for _ in range(6)]

    with pytest.raises(HermesApiError, match="busy") as raised:
        await _start(client)

    assert raised.value.code == "session_busy"
    assert len(fake.requests_to("POST", "/v1/runs")) == 6
    assert sleeps == [1.0] * 5


async def test_admission_queued_without_run_id_is_accepted_and_never_resent(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    fake.start_replies = [admission_queued(with_run_id=False)]

    started = await _start(client)

    assert started == RunStart(run_id=None, queued=True)
    assert len(fake.requests) == 1


async def test_admission_queued_with_run_id_polls_until_running_then_streams(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.start_replies = [admission_queued(with_run_id=True)]
    fake.run_scripts = [
        RunScript(
            events=[{"event": "run.started"}, {"event": "run.completed", "output": "42"}],
            subscriptions=[Subscription(status=404)],
            polls=["queued", "running"],
        )
    ]

    started = await _start(client)
    assert started.queued and started.run_id is not None
    events = await _collect(client, started.run_id)

    assert _kinds(events) == [("started", None), ("completed", "42")]
    assert len(fake.requests_to("POST", "/v1/runs")) == 1
    assert len(fake.requests_to("GET", f"/runs/{started.run_id}")) == 2
    assert len(fake.requests_to("GET", "/events")) == 2
    assert sleeps == [5.0]


@pytest.mark.parametrize("unavailable", [draining, storage_unavailable])
async def test_unavailable_is_retried_with_the_same_request_after_retry_after(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps, unavailable: Any
) -> None:
    fake.start_replies = [unavailable(retry_after="2"), unavailable(retry_after="3")]

    started = await _start(client)

    assert started.run_id is not None
    posts = fake.requests_to("POST", "/v1/runs")
    assert len(posts) == 3
    assert all(post.body == posts[0].body and post.headers["Idempotency-Key"] == TASK_UUID for post in posts)
    assert sleeps == [2.0, 3.0]


async def test_unavailable_fails_after_three_retries(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.start_replies = [draining() for _ in range(4)]

    with pytest.raises(HermesApiError, match="temporarily unavailable") as raised:
        await _start(client)

    assert raised.value.code == "runtime_draining"
    assert len(fake.requests) == 4
    assert sleeps == [1.0] * 3


async def test_idempotency_key_conflict_fails_and_logs_a_bug(
    client: HermesApiClient, fake: FakeHermesApi, caplog: pytest.LogCaptureFixture
) -> None:
    fake.start_replies = [idempotency_conflict()]

    with (
        caplog.at_level(logging.ERROR, logger="hermes_duplex.dispatch.hermes_api"),
        pytest.raises(HermesApiError) as raised,
    ):
        await _start(client)

    assert raised.value.code == "idempotency_key_conflict"
    assert len(fake.requests) == 1
    assert any(record.levelno == logging.ERROR and "Bug" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("status", "code"), [(409, "profile_mismatch"), (409, "permission_denied"), (400, "invalid_params")]
)
async def test_other_refusals_fail_with_a_reason_naming_the_code(
    client: HermesApiClient, fake: FakeHermesApi, status: int, code: str
) -> None:
    fake.start_replies = [refusal(code, status)]

    with pytest.raises(HermesApiError, match=code) as raised:
        await _start(client)

    assert raised.value.code == code
    assert len(fake.requests) == 1


async def test_unknown_profile_prefix_fails(fake: FakeHermesApi) -> None:
    async with HermesApiClient(fake.api_base, lambda profile: "some-key-0123456789") as client:
        with pytest.raises(HermesApiError, match="not_found"):
            await client.start_run("nope", "hi", "duplex-rob-nope", None, "low", TASK_UUID)


async def test_connection_refused_fails_as_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async with HermesApiClient(f"http://127.0.0.1:{port}", KEYS.get) as client:
        with pytest.raises(HermesApiError, match="Hermes is unreachable"):
            await _start(client)


async def test_stream_maps_every_event_name(client: HermesApiClient, fake: FakeHermesApi) -> None:
    fake.run_scripts = [
        RunScript(
            events=[
                {"event": "run.queued"},
                {"event": "run.started"},
                {"event": "message.started"},
                {"event": "message.delta", "delta": "Look"},
                {"event": "tool.started", "tool": "terminal", "preview": "ls -la"},
                {"event": "tool.progress", "tool": "web_search"},
                {"event": "tool.completed", "tool": "terminal", "duration": 0.5, "error": False},
                {"event": "tool.failed", "tool": "terminal"},
                {"event": "approval.request", "command": "rm"},
                {"event": "message.interim", "text": "Checking prices"},
                {"event": "run.steered"},
                {"event": "run.stopping"},
                {"event": "something.new"},
                {"event": "run.completed", "output": "Up 3%", "usage": {}, "runtime": {}},
            ]
        )
    ]
    started = await _start(client)

    events = await _collect(client, str(started.run_id))

    assert _kinds(events) == [
        ("queued", None),
        ("started", None),
        ("progress", "terminal: ls -la"),
        ("progress", "web_search"),
        ("progress", "terminal"),
        ("message", "Checking prices"),
        ("completed", "Up 3%"),
    ]
    assert events[-1].raw["event"] == "run.completed"
    [subscribe] = fake.requests_to("GET", "/events")
    assert subscribe.headers["Authorization"] == f"Bearer {KEYS['stock-picker']}"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"event": "run.failed", "error": "provider down"}, ("failed", "provider down")),
        ({"event": "run.interrupted"}, ("failed", "interrupted")),
        ({"event": "run.interrupted", "error": "Gateway shutdown"}, ("failed", "Gateway shutdown")),
        ({"event": "run.cancelled"}, ("cancelled", None)),
    ],
)
def test_terminal_failures_and_cancel_map_to_their_kinds(
    payload: dict[str, Any], expected: tuple[str, str | None]
) -> None:
    event = run_event(payload)

    assert event is not None
    assert (event.kind, event.text) == expected


async def test_dropped_stream_resubscribes_from_the_last_event(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    fake.run_scripts = [
        RunScript(
            events=[
                {"event": "run.started"},
                {"event": "message.interim", "text": "halfway"},
                {"event": "run.completed", "output": "done"},
            ],
            subscriptions=[Subscription(drop_after=2)],
        )
    ]
    started = await _start(client)

    events = await _collect(client, str(started.run_id))

    assert _kinds(events) == [("started", None), ("message", "halfway"), ("completed", "done")]
    first, second = fake.requests_to("GET", "/events")
    assert "Last-Event-ID" not in first.headers
    assert second.headers["Last-Event-ID"] == "2"


async def test_second_drop_falls_back_to_polling_until_terminal(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.run_scripts = [
        RunScript(
            events=[{"event": "run.started"}, {"event": "run.completed", "output": "from status"}],
            subscriptions=[Subscription(drop_after=1), Subscription(drop_after=0)],
            polls=["running", "completed"],
        )
    ]
    started = await _start(client)

    events = await _collect(client, str(started.run_id))

    assert _kinds(events) == [("started", None), ("completed", "from status")]
    assert events[-1].raw["object"] == "hermes.run"
    assert len(fake.requests_to("GET", f"/runs/{started.run_id}")) == 2
    assert sleeps == [5.0]


async def test_failed_resubscribe_and_three_failed_polls_end_as_unknown(
    client: HermesApiClient, fake: FakeHermesApi, sleeps: _Sleeps
) -> None:
    fake.run_scripts = [
        RunScript(
            events=[{"event": "run.started"}, {"event": "run.completed", "output": "never seen"}],
            subscriptions=[Subscription(drop_after=1), Subscription(status=503)],
            polls=[503, 503, 503],
        )
    ]
    started = await _start(client)

    events = await _collect(client, str(started.run_id))

    assert _kinds(events) == [("started", None), ("unknown", None)]
    assert len(fake.requests_to("GET", f"/runs/{started.run_id}")) == 3
    assert sleeps == [5.0, 5.0]


async def test_run_status_gives_the_end_of_a_run_that_ended(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    fake.run_scripts = [
        RunScript(events=[{"event": "run.started"}, {"event": "run.failed", "error": "boom"}])
    ]
    started = await _start(client)

    ended = await client.run_status("stock-picker", str(started.run_id))

    assert ended is not None and (ended.kind, ended.text) == ("failed", "boom")
    assert len(fake.requests_to("GET", f"/runs/{started.run_id}")) == 1


@pytest.mark.parametrize("polls", [["running"], ["queued"]])
async def test_run_status_is_none_while_a_run_goes_on(
    client: HermesApiClient, fake: FakeHermesApi, polls: list[str | int]
) -> None:
    fake.run_scripts = [RunScript(events=[{"event": "run.started"}], polls=list(polls))]
    started = await _start(client)

    assert await client.run_status("stock-picker", str(started.run_id)) is None


async def test_run_status_that_cannot_be_read_raises(client: HermesApiClient, fake: FakeHermesApi) -> None:
    fake.run_scripts = [RunScript(events=[{"event": "run.started"}], polls=[503])]
    started = await _start(client)

    with pytest.raises(HermesApiError, match="did not say"):
        await client.run_status("stock-picker", str(started.run_id))


async def test_run_status_of_a_profile_without_a_key_raises(client: HermesApiClient) -> None:
    with pytest.raises(HermesApiError, match="not configured"):
        await client.run_status("notes", "run_1")


async def test_stop_posts_to_the_run_stop_path(client: HermesApiClient, fake: FakeHermesApi) -> None:
    started = await _start(client)

    await client.stop("stock-picker", str(started.run_id))

    [stop] = fake.requests_to("POST", "/stop")
    assert stop.path == f"/p/stock-picker/v1/runs/{started.run_id}/stop"
    assert stop.headers["Authorization"] == f"Bearer {KEYS['stock-picker']}"


async def test_stop_of_an_unknown_run_fails(client: HermesApiClient) -> None:
    with pytest.raises(HermesApiError, match="run_not_found"):
        await client.stop("stock-picker", "run_missing")


async def test_fake_lists_models_per_profile_and_replays_a_repeated_key(
    client: HermesApiClient, fake: FakeHermesApi
) -> None:
    first = await _start(client)
    replayed = await _start(client)

    assert replayed.run_id == first.run_id
    assert len(fake.runs) == 1
    session = aiohttp.ClientSession()
    async with (
        session,
        session.get(
            f"{fake.api_base}/p/general/v1/models", headers={"Authorization": f"Bearer {KEYS['general']}"}
        ) as resp,
    ):
        assert (await resp.json())["data"] == [{"id": "general", "object": "model"}]
