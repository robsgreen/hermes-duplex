"""Client for the Hermes API server's runs endpoints: start a run on a profile, follow it, stop it.

Every request goes to ``{api_base}/p/{profile}/v1/...`` with that profile's own bearer key; Hermes
rejects the default listener's key on a named profile. Errors are told apart by the response's
``error.code``, never by message text.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Self
from urllib.parse import quote

import aiohttp

from ..providers.base import ModelChoice
from ..router.base import Effort

logger = logging.getLogger(__name__)

RunEventKind = Literal[
    "queued", "started", "progress", "message", "completed", "failed", "cancelled", "unknown"
]

TERMINAL_KINDS: frozenset[RunEventKind] = frozenset({"completed", "failed", "cancelled", "unknown"})

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
"""Run statuses in ``GET .../v1/runs/{id}`` after which Hermes does no more work on the run."""

SESSION_BUSY_RETRIES = 5
SESSION_BUSY_RETRY_DELAY_S = 1.0
UNAVAILABLE_RETRIES = 3
STATUS_POLL_INTERVAL_S = 5.0
STATUS_POLL_FAILURES = 3

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Hermes writes a keepalive comment every 10 s while a run is quiet, so a stream that stays silent
# for 30 s has dropped even if the socket is still open.
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=30)

_EVENT_KINDS: dict[str, RunEventKind] = {
    "run.queued": "queued",
    "run.started": "started",
    "tool.started": "progress",
    "tool.progress": "progress",
    "tool.completed": "progress",
    "message.interim": "message",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.interrupted": "failed",
    "run.cancelled": "cancelled",
}


class HermesApiError(Exception):
    """Hermes did not accept a request, or could not be reached.

    ``reason`` is a short phrase the voice model can speak, such as "Hermes is unreachable"; it is the
    only wording of the failure, spoken in the task's failure or cancel notice and logged.
    ``code`` is Hermes's ``error.code`` when the response carried one.
    """

    def __init__(self, reason: str, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class RunStart:
    """Hermes accepted a run.

    ``queued`` is true when Hermes admitted the run but has not started it. ``run_id`` is ``None``
    only for a queued run Hermes gave no id: it still runs once, but cannot be followed or stopped.
    """

    run_id: str | None
    queued: bool


@dataclass(frozen=True)
class RunEvent:
    """One step of a run, as the session announces it.

    ``text`` depends on ``kind``: the tool name and preview for ``progress``, the interim message for
    ``message``, the final output for ``completed``, the error for ``failed`` ("interrupted" for an
    interrupted run without one), and ``None`` otherwise. ``unknown`` means Hermes stopped answering
    before the run ended, so its outcome is not known. ``raw`` is the Hermes event, or the run status
    object when the event was read from a status poll.
    """

    kind: RunEventKind
    text: str | None
    raw: Mapping[str, Any]


def run_event(payload: Mapping[str, Any]) -> RunEvent | None:
    """Map a Hermes run event (the JSON of one SSE frame) to a ``RunEvent``; ``None`` for an event
    the session does not announce, such as ``message.delta`` or a name this client does not know."""
    return _event(str(payload.get("event")), payload)


def _event(name: str, fields: Mapping[str, Any]) -> RunEvent | None:
    kind = _EVENT_KINDS.get(name)
    if kind is None:
        return None
    text: str | None = None
    if kind == "progress":
        tool, preview = fields.get("tool"), fields.get("preview")
        text = f"{tool}: {preview}" if tool and preview else tool or preview or None
    elif kind == "message":
        text = fields.get("text")
    elif kind == "completed":
        text = fields.get("output")
    elif kind == "failed":
        text = fields.get("error") or ("interrupted" if name == "run.interrupted" else None)
    return RunEvent(kind, text, fields)


def _status_event(status: Mapping[str, Any] | None) -> RunEvent:
    """The terminal ``RunEvent`` for a terminal run status, or ``unknown`` when there is none."""
    event = _event(f"run.{status.get('status')}", status) if status is not None else None
    return event or RunEvent("unknown", None, status or {})


class HermesApiClient:
    """Starts, follows, and stops runs on Hermes profiles through the Hermes API server.

    ``api_key(profile)`` returns the ``API_SERVER_KEY`` of that profile, or ``None`` when it is not
    configured; a call for such a profile fails with "not configured" before any request is sent.
    ``sleep`` is how the client waits between retries and polls (tests pass a recorder).

    The client owns one ``aiohttp.ClientSession``, opened on first use; call ``close()`` (or use the
    client as an async context manager) when done.
    """

    def __init__(
        self,
        api_base: str,
        api_key: Callable[[str], str | None],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._sleep = sleep
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session. The client opens a new one if it is used again."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def start_run(
        self, profile: str, text: str, session_id: str, model: ModelChoice | None, effort: Effort, idem: str
    ) -> RunStart:
        """Start a run of ``text`` on ``profile`` and return once Hermes has accepted it.

        ``session_id`` keys the Hermes conversation, so runs that share it see each other's context;
        it is sent unchanged on every retry. ``idem`` is the ``Idempotency-Key`` (the task's uuid),
        also unchanged on retries, so Hermes admits the run at most once. ``model`` ``None`` leaves
        the choice to the profile's default model.

        Retries ``session_busy`` up to 5 times, 1 s apart, and ``runtime_draining`` or
        ``storage_unavailable`` up to 3 times after the server's ``Retry-After``. An
        ``admission_queued`` reply means Hermes committed the input and runs it once when it can, so
        it is never resent and is returned as a queued ``RunStart``.

        Raises ``HermesApiError`` when the profile has no key ("not configured"), Hermes cannot be
        reached or does not answer ("Hermes is unreachable"), retries run out, or Hermes refuses the run; ``reason``
        names the refusal's code.
        """
        headers = {**self._auth_headers(profile), "Idempotency-Key": idem}
        body: dict[str, Any] = {"input": text, "session_id": session_id}
        if model is not None:
            body["model"] = model.model
            body["provider"] = model.provider
        body["model_options"] = {"reasoning": {"effort": effort}}

        busy_retries = unavailable_retries = 0
        while True:
            try:
                async with self._http().post(self._url(profile, "runs"), json=body, headers=headers) as resp:
                    reply = await _json_body(resp)
                    status, retry_after_s = resp.status, _retry_after_s(resp.headers)
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning("Hermes unreachable starting a run on %s: %r", profile, exc)
                raise HermesApiError("Hermes is unreachable") from exc

            if status == 202:
                return RunStart(run_id=str(reply["run_id"]), queued=reply.get("status") == "queued")

            code = _error_code(reply)
            if code == "session_busy" and busy_retries < SESSION_BUSY_RETRIES:
                busy_retries += 1
                logger.info("Hermes session %s busy; retry %d in 1 s", session_id, busy_retries)
                await self._sleep(SESSION_BUSY_RETRY_DELAY_S)
            elif (
                code in ("runtime_draining", "storage_unavailable")
                and unavailable_retries < UNAVAILABLE_RETRIES
            ):
                unavailable_retries += 1
                logger.info(
                    "Hermes %s on %s; retry %d in %s s", code, profile, unavailable_retries, retry_after_s
                )
                await self._sleep(retry_after_s)
            elif code == "admission_queued":
                run_id = reply.get("run_id")
                logger.info("Hermes queued run %s on %s for later admission", run_id, profile)
                return RunStart(run_id=str(run_id) if run_id else None, queued=True)
            else:
                raise _refusal(profile, session_id, status, code)

    async def events(self, profile: str, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield the run's events as ``RunEvent``s, ending after the first terminal one.

        Follows the run from wherever it is. A run Hermes has queued but is not streaming yet is
        polled every 5 s until it starts, then streamed. When the stream drops before a terminal
        event, the client re-subscribes once from the last event seen; if that fails too, it polls the
        run's status every 5 s until the run ends, and yields ``unknown`` after 3 failed polls in a
        row. Events the session does not announce are skipped.

        Raises ``HermesApiError`` when the profile has no key, before any request is sent.
        """
        headers = self._auth_headers(profile)
        last_seq: int | None = None
        subscriptions_left = 2  # the first subscription and one re-subscribe after a drop
        waited_for_start = False
        while subscriptions_left > 0:
            subscriptions_left -= 1
            try:
                async with aclosing(self._subscribe(profile, run_id, headers, last_seq)) as frames:
                    async for seq, payload in frames:
                        last_seq = seq if seq is not None else last_seq
                        event = run_event(payload)
                        if event is not None:
                            yield event
                            if event.kind in TERMINAL_KINDS:
                                return
                logger.warning("Hermes event stream for run %s ended before the run did", run_id)
            except aiohttp.ClientResponseError as exc:
                if exc.status != 404 or last_seq is not None or waited_for_start:
                    logger.warning("Hermes refused the event stream for run %s: %s", run_id, exc.status)
                    break
                # Hermes admitted the run but is not running it in this gateway yet.
                waited_for_start = True
                subscriptions_left += 1
                status = await self._poll_status(profile, run_id, headers, lambda s: s != "queued")
                if status is None or status.get("status") in TERMINAL_STATUSES:
                    yield _status_event(status)
                    return
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                # A malformed frame (ValueError) leaves the stream unreadable, so it counts as a drop.
                logger.warning("Hermes event stream for run %s dropped: %r", run_id, exc)

        yield _status_event(await self._poll_status(profile, run_id, headers, TERMINAL_STATUSES.__contains__))

    async def run_status(self, profile: str, run_id: str) -> RunEvent | None:
        """The terminal ``RunEvent`` of a run that has ended, from one ``GET .../runs/{id}``, or
        ``None`` while the run is queued or running.

        Raises ``HermesApiError`` when the profile has no key, before any request is sent, and when the
        status cannot be read; following the run with ``events`` may still find out how it goes.
        """
        headers = self._auth_headers(profile)
        status, failure = await self._read_status(profile, run_id, headers)
        if not status:
            logger.warning("Reading the status of Hermes run %s failed: %s", run_id, failure)
            raise HermesApiError("Hermes did not say how it is going")
        return _status_event(status) if status.get("status") in TERMINAL_STATUSES else None

    async def stop(self, profile: str, run_id: str) -> None:
        """Ask Hermes to stop the run; returns once Hermes has accepted the request.

        Raises ``HermesApiError`` when the profile has no key, Hermes cannot be reached or does not
        answer, or Hermes refuses the stop.
        """
        headers = self._auth_headers(profile)
        try:
            async with self._http().post(
                self._url(profile, f"runs/{quote(run_id, safe='')}/stop"), headers=headers
            ) as resp:
                if resp.status < 300:
                    return
                code = _error_code(await _json_body(resp))
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("Hermes unreachable stopping run %s on %s: %r", run_id, profile, exc)
            raise HermesApiError("Hermes is unreachable") from exc
        logger.warning("Hermes refused to stop run %s on %s: %s %s", run_id, profile, resp.status, code)
        raise HermesApiError(f"Hermes refused to stop it ({code or f'HTTP {resp.status}'})", code)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT)
        return self._session

    def _url(self, profile: str, path: str) -> str:
        return f"{self._api_base}/p/{quote(profile, safe='')}/v1/{path}"

    def _auth_headers(self, profile: str) -> dict[str, str]:
        key = self._api_key(profile)
        if not key:
            logger.warning("No Hermes API key configured for profile %s", profile)
            raise HermesApiError(f"the {profile} profile is not configured", "not_configured")
        return {"Authorization": f"Bearer {key}"}

    async def _subscribe(
        self, profile: str, run_id: str, headers: Mapping[str, str], last_seq: int | None
    ) -> AsyncGenerator[tuple[int | None, dict[str, Any]]]:
        """Yield ``(seq, event)`` for each SSE frame of the run's event stream, after ``last_seq``.

        Raises ``aiohttp.ClientResponseError`` when Hermes refuses the subscription, other
        ``aiohttp.ClientError``s or ``TimeoutError`` when the stream drops, and ``ValueError`` for a
        malformed frame.
        """
        if last_seq is not None:
            headers = {**headers, "Last-Event-ID": str(last_seq)}
        url = self._url(profile, f"runs/{quote(run_id, safe='')}/events")
        async with self._http().get(url, headers=headers, timeout=_STREAM_TIMEOUT) as resp:
            resp.raise_for_status()
            seq: int | None = None
            data: list[str] = []
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if line.startswith("id:"):
                    seq = int(line[3:].strip())
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    yield seq, json.loads("\n".join(data))
                    seq, data = None, []

    async def _poll_status(
        self, profile: str, run_id: str, headers: Mapping[str, str], until: Callable[[str], bool]
    ) -> dict[str, Any] | None:
        """Poll the run's status every 5 s until ``until(status)`` holds and return the status object;
        ``None`` after 3 failed polls in a row."""
        failures = 0
        while True:
            status, failure = await self._read_status(profile, run_id, headers)
            if status:
                failures = 0
                if until(str(status.get("status"))):
                    return status
            else:
                failures += 1
                logger.warning(
                    "Polling Hermes run %s failed (%d of %d): %s",
                    run_id,
                    failures,
                    STATUS_POLL_FAILURES,
                    failure,
                )
                if failures >= STATUS_POLL_FAILURES:
                    return None
            await self._sleep(STATUS_POLL_INTERVAL_S)

    async def _read_status(
        self, profile: str, run_id: str, headers: Mapping[str, str]
    ) -> tuple[dict[str, Any], str]:
        """One ``GET .../runs/{id}``: the run's status object and the HTTP status, or an empty object
        and a short description of why it could not be read."""
        url = self._url(profile, f"runs/{quote(run_id, safe='')}")
        # The failure is described by hand: an aiohttp exception's repr would log the bearer key.
        try:
            async with self._http().get(url, headers=headers) as resp:
                return (await _json_body(resp) if resp.status == 200 else {}), f"HTTP {resp.status}"
        except (aiohttp.ClientError, TimeoutError) as exc:
            return {}, type(exc).__name__


async def _json_body(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """The response's JSON object, or an empty one when the body is not a JSON object."""
    try:
        body = await resp.json(content_type=None)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(reply: Mapping[str, Any]) -> str | None:
    error = reply.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _retry_after_s(headers: Mapping[str, str]) -> float:
    """Seconds from a ``Retry-After`` header; 1 s when it is missing or not a number of seconds."""
    try:
        return max(0.0, float(headers.get("Retry-After", "")))
    except ValueError:
        return 1.0


def _refusal(profile: str, session_id: str, status: int, code: str | None) -> HermesApiError:
    """The error for a start request Hermes refused, logged at the level its cause deserves."""
    if code == "idempotency_key_conflict":
        # The key is the task's own uuid and the body never changes between attempts, so Hermes
        # seeing a different payload under it is a bug on this side.
        logger.error(
            "Bug: Hermes reports an Idempotency-Key conflict for session %s on %s", session_id, profile
        )
        return HermesApiError("Hermes rejected it as a duplicate task", code)
    if code == "session_busy":
        logger.warning("Hermes session %s still busy after %d retries", session_id, SESSION_BUSY_RETRIES)
        return HermesApiError(f"the {profile} agent is still busy", code)
    if code in ("runtime_draining", "storage_unavailable"):
        logger.warning("Hermes still %s after %d retries", code, UNAVAILABLE_RETRIES)
        return HermesApiError("Hermes is temporarily unavailable", code)
    logger.warning("Hermes refused a run on %s: HTTP %d, code %s", profile, status, code)
    return HermesApiError(f"Hermes refused it ({code or f'HTTP {status}'})", code)
