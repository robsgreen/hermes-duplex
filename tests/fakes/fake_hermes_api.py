"""An aiohttp fake of the Hermes API server's per-profile runs endpoints.

It serves ``/p/{profile}/v1/runs``, ``.../runs/{id}``, ``.../runs/{id}/events``,
``.../runs/{id}/stop``, and ``.../models`` with the wire shapes of hermes-agent's API server, checks
each profile's own bearer key, and records every request. Tests script it:

- ``start_replies``: replies to ``POST .../runs``, used in order before the fake accepts a run
  (``session_busy()``, ``draining()``, ``admission_queued()``, ``refusal()``, ...).
- ``run_scripts``: what each new run does (its events, how each subscription to its stream behaves,
  and what each status poll returns), used in order; a run with no script completes with "done".

Usage::

    async with FakeHermesApi({"general": "k" * 16}) as fake:
        client = HermesApiClient(fake.api_base, keys.get)
"""

import asyncio
import itertools
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from aiohttp import web

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True)
class RecordedRequest:
    """One request the fake received; ``body`` is the parsed JSON body, or ``None`` without one."""

    method: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: Any


@dataclass(frozen=True)
class Reply:
    """A scripted HTTP reply. ``accepts_run`` makes the fake also create a run (``admission_queued``
    with a run id, whose ``run_id`` it fills into the body)."""

    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    accepts_run: bool = False


def error_reply(status: int, code: str, message: str = "", headers: Mapping[str, str] | None = None) -> Reply:
    """A Hermes error reply: ``{"error": {"message", "type", "param", "code"}}``."""
    error = {"message": message or code, "type": "invalid_request_error", "param": None, "code": code}
    return Reply(status, {"error": error}, headers or {})


def session_busy() -> Reply:
    return error_reply(409, "session_busy", "Session is busy with another run")


def draining(retry_after: str = "1") -> Reply:
    return error_reply(503, "runtime_draining", headers={"Retry-After": retry_after})


def storage_unavailable(retry_after: str = "1") -> Reply:
    return error_reply(503, "storage_unavailable", headers={"Retry-After": retry_after})


def admission_queued(with_run_id: bool = False) -> Reply:
    """``admission_queued`` as hermes-agent sends it (no ``run_id``), or with one when asked."""
    reply = error_reply(503, "admission_queued", "queued for the next owner", {"x-should-retry": "false"})
    return Reply(reply.status, reply.body, reply.headers, accepts_run=with_run_id)


def idempotency_conflict() -> Reply:
    return error_reply(409, "idempotency_key_conflict", "Idempotency-Key was already used")


def refusal(code: str, status: int = 409) -> Reply:
    return error_reply(status, code)


@dataclass(frozen=True)
class Subscription:
    """How one ``GET .../events`` request behaves.

    ``status`` other than 200 refuses the subscription with that HTTP status. Otherwise the fake
    sends the run's events after the requested ``Last-Event-ID``; with ``drop_after`` it aborts the
    connection after that many events instead of finishing.
    """

    status: int = 200
    drop_after: int | None = None


Poll = str | int
"""A scripted status poll: a run status (``"queued"``, ``"running"``, a terminal status), or an
HTTP error status the poll fails with."""


@dataclass
class RunScript:
    """What one run does. ``events`` are event payloads without ``run_id``/``timestamp``/``seq``
    (the fake adds them; seq starts at 1). ``subscriptions`` and ``polls`` are used in order; once
    used up, a subscription streams everything and a poll returns the status the events end in."""

    events: list[dict[str, Any]] = field(
        default_factory=lambda: [{"event": "run.started"}, {"event": "run.completed", "output": "done"}]
    )
    subscriptions: list[Subscription] = field(default_factory=list)
    polls: list[Poll] = field(default_factory=list)


@dataclass
class FakeRun:
    """A run the fake admitted, on the profile and Hermes session it was started with."""

    run_id: str
    profile: str
    session_id: str
    model: str | None
    script: RunScript
    state: str | None = None
    """The status a poll or stop set, or ``None`` for the status the events end in."""
    created_at: float = field(default_factory=time.time)

    def events(self) -> list[dict[str, Any]]:
        """The run's events as Hermes streams them, with ``run_id``, ``timestamp``, and ``seq``."""
        return [
            {"event": e["event"], "run_id": self.run_id, "timestamp": self.created_at, "seq": seq}
            | {k: v for k, v in e.items() if k != "event"}
            for seq, e in enumerate(self.script.events, start=1)
        ]

    def status(self) -> dict[str, Any]:
        """The ``GET .../v1/runs/{id}`` object."""
        events = self.events()
        terminal = next((e for e in events if e["event"].removeprefix("run.") in TERMINAL_STATUSES), None)
        status = self.state or (terminal["event"].removeprefix("run.") if terminal else "running")
        body: dict[str, Any] = {
            "object": "hermes.run",
            "run_id": self.run_id,
            "status": status,
            "session_id": self.session_id,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": time.time(),
            "last_event": events[-1]["event"] if events else None,
        }
        if terminal is not None and status == terminal["event"].removeprefix("run."):
            body |= {k: terminal[k] for k in ("output", "usage", "runtime", "error") if k in terminal}
        return body


class FakeHermesApi:
    """A running fake Hermes API server on 127.0.0.1. ``keys`` maps each profile to its bearer key;
    a profile not in it answers 404, as an unknown ``/p/<profile>/`` prefix does. ``port`` 0 picks a
    free port; a fixed one lets a test bring Hermes "back" where a client already points."""

    def __init__(
        self, keys: Mapping[str, str], models: Mapping[str, list[str]] | None = None, *, port: int = 0
    ) -> None:
        self.keys = dict(keys)
        self._port = port
        self.models = dict(models or {})
        self.start_replies: list[Reply] = []
        self.run_scripts: list[RunScript] = []
        self.requests: list[RecordedRequest] = []
        self.runs: dict[str, FakeRun] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._run_ids = (f"run_{n:032x}" for n in itertools.count(1))
        self._runner: web.AppRunner | None = None
        self.api_base = ""

    async def __aenter__(self) -> Self:
        app = web.Application(middlewares=[self._record_and_authorize])
        app.add_routes(
            [
                web.post("/p/{profile}/v1/runs", self._start),
                web.get("/p/{profile}/v1/runs/{run_id}", self._status),
                web.get("/p/{profile}/v1/runs/{run_id}/events", self._events),
                web.post("/p/{profile}/v1/runs/{run_id}/stop", self._stop),
                web.get("/p/{profile}/v1/models", self._models),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        port = self._runner.addresses[0][1]
        self.api_base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def requests_to(self, method: str, suffix: str) -> list[RecordedRequest]:
        """The recorded requests with this method whose path ends with ``suffix``."""
        return [r for r in self.requests if r.method == method and r.path.endswith(suffix)]

    @web.middleware
    async def _record_and_authorize(self, request: web.Request, handler: Any) -> web.StreamResponse:
        raw = await request.read()
        self.requests.append(
            RecordedRequest(
                request.method,
                request.path,
                dict(request.headers),
                dict(request.query),
                json.loads(raw) if raw else None,
            )
        )
        profile = request.match_info.get("profile", "")
        if profile not in self.keys:
            return web.json_response(error_reply(404, "not_found").body, status=404)
        if request.headers.get("Authorization") != f"Bearer {self.keys[profile]}":
            return web.json_response(error_reply(401, "invalid_api_key").body, status=401)
        return await handler(request)

    async def _start(self, request: web.Request) -> web.Response:
        body = await request.json()
        profile = request.match_info["profile"]
        key = (profile, request.headers.get("Idempotency-Key", ""))
        if key in self._idempotency:
            run_id = self._idempotency[key]
            reply = {"run_id": run_id, "status": self.runs[run_id].status()["status"], "replayed": True}
            return web.json_response(reply, status=202, headers={"Idempotency-Replayed": "true"})
        if self.start_replies:
            scripted = self.start_replies.pop(0)
            reply_body = dict(scripted.body)
            if scripted.accepts_run:
                run = self._new_run(profile, body, key)
                run.state = "queued"
                reply_body["run_id"] = run.run_id
            return web.json_response(reply_body, status=scripted.status, headers=dict(scripted.headers))
        run = self._new_run(profile, body, key)
        return web.json_response({"run_id": run.run_id, "status": "started", "replayed": False}, status=202)

    def _new_run(self, profile: str, body: Mapping[str, Any], key: tuple[str, str]) -> FakeRun:
        script = self.run_scripts.pop(0) if self.run_scripts else RunScript()
        run = FakeRun(next(self._run_ids), profile, body["session_id"], body.get("model"), script)
        self.runs[run.run_id] = run
        self._idempotency[key] = run.run_id
        return run

    def _run(self, request: web.Request) -> FakeRun | None:
        run = self.runs.get(request.match_info["run_id"])
        return run if run is not None and run.profile == request.match_info["profile"] else None

    async def _status(self, request: web.Request) -> web.Response:
        run = self._run(request)
        if run is None:
            return web.json_response(error_reply(404, "run_not_found").body, status=404)
        poll = run.script.polls.pop(0) if run.script.polls else None
        if isinstance(poll, int):
            return web.json_response(error_reply(poll, "storage_unavailable").body, status=poll)
        if poll is not None:
            run.state = None if poll in TERMINAL_STATUSES else poll
        return web.json_response(run.status())

    async def _events(self, request: web.Request) -> web.StreamResponse:
        run = self._run(request)
        subscription = run.script.subscriptions.pop(0) if run and run.script.subscriptions else Subscription()
        if run is None or subscription.status != 200:
            status = 404 if run is None else subscription.status
            return web.json_response(error_reply(status, "run_not_found").body, status=status)
        raw_last_seq = request.headers.get("Last-Event-ID") or request.query.get("last_seq") or "-1"
        pending = [e for e in run.events() if e["seq"] > int(raw_last_seq)]
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b": open\n\n")
        for sent, event in enumerate(pending):
            if subscription.drop_after is not None and sent >= subscription.drop_after:
                await asyncio.sleep(0)
                assert request.transport is not None
                request.transport.abort()
                return response
            await response.write(f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode())
        await response.write(b": stream closed\n\n")
        await response.write_eof()
        return response

    async def _stop(self, request: web.Request) -> web.Response:
        run = self._run(request)
        if run is None:
            return web.json_response(error_reply(404, "run_not_found").body, status=404)
        run.state = "stopping"
        return web.json_response(run.status())

    async def _models(self, request: web.Request) -> web.Response:
        profile = request.match_info["profile"]
        ids = self.models.get(profile, [profile])
        return web.json_response({"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})
