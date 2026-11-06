"""Live end to end: a real Hermes gateway, real GPT-Live, and the OpenAI Decisions router.

rob speaks the committed WAV fixtures into the in-gateway web transport at real-time pace: "How is
my portfolio doing?", then, once the stock-picker report has been spoken, "And what about Tesla?".
The test passes when the first question is routed to stock-picker, its run completes, and the
spoken announcement of the result arrives within 60 s of the question.

Opt in, and configure it, with these variables (the ``live_hermes`` fixture in conftest.py reads
them):

- ``HERMES_DUPLEX_LIVE=1`` and ``OPENAI_API_KEY`` (both required): opt in, and the key for GPT-Live,
  Decisions, and by default the profiles' model.
- ``HERMES_DUPLEX_LIVE_MODEL`` (default ``gpt-5-mini``): the model the ``general`` and
  ``stock-picker`` profiles run on, for every effort. Hermes sends it the routed effort as
  ``reasoning_effort``, so pick a reasoning model.
- ``HERMES_DUPLEX_LIVE_BASE_URL`` (default ``https://api.openai.com/v1``): the OpenAI-compatible
  chat completions API that serves that model.
- ``HERMES_DUPLEX_LIVE_KEY_ENV`` (default ``OPENAI_API_KEY``): the variable holding that API's key.
  The gateway does not inherit ``HERMES_*`` variables, so it must not start with ``HERMES_``.

The test also needs hermes-agent installed (``hermes`` on PATH) and the WAV fixtures in
``tests/fixtures/audio/`` (``uv run python -m tests.live.make_audio_fixtures`` writes them).

Run it with ``HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs``.
"""

import asyncio
import itertools
import json
import time
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest

from hermes_duplex.tasks import TERMINAL_STATES
from hermes_duplex.transports.web.server import RATE
from tests.e2e.real_hermes import TOKENS, RealHermes
from tests.live.make_audio_fixtures import AUDIO_DIR, SILENCE

pytestmark = pytest.mark.live

CHUNK_BYTES = 2 * RATE * 20 // 1000
"""One 20 ms web transport message of PCM16 mono audio."""

ANNOUNCEMENT_TIMEOUT_S = 60.0
"""How long after the question the spoken result may take to arrive."""

ANNOUNCEMENT_END_S = 1.5
"""How long output audio must pause before the announcement counts as spoken to the end."""

FOLLOW_UP_TIMEOUT_S = 30.0
"""How long after the follow-up question the announcement's transcript line may take."""


def load_wav(name: str) -> bytes:
    """The PCM16 samples of ``tests/fixtures/audio/<name>``, which must be 24 kHz mono PCM16."""
    with wave.open(str(AUDIO_DIR / name), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2), (
            f"{name} must be {RATE} Hz mono PCM16"
        )
        return wav.readframes(wav.getnframes())


class Microphone:
    """Sends audio to a web transport socket as a microphone would: one 20 ms message every 20 ms,
    silence whenever nothing is being said. ``run`` sends until cancelled, or raises when a send
    fails (the socket closed)."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self._socket = socket
        self._pending = bytearray()
        self._drained = asyncio.Event()
        self._drained.set()
        self._stopped: BaseException | None = None

    async def say(self, pcm16: bytes) -> None:
        """Queue ``pcm16`` (24 kHz mono) and return once all of it has been sent.

        Raises ``AssertionError`` when the microphone stopped sending before then.
        """
        if self._stopped is None:
            self._pending += pcm16
            self._drained.clear()
            await self._drained.wait()
        assert self._stopped is None, f"the web transport socket stopped taking audio: {self._stopped!r}"

    async def run(self) -> None:
        silence = bytes(CHUNK_BYTES)
        started = time.monotonic()
        try:
            for number in itertools.count():
                await asyncio.sleep(max(0.0, started + number * 0.02 - time.monotonic()))
                chunk = bytes(self._pending[:CHUNK_BYTES])
                del self._pending[:CHUNK_BYTES]
                await self._socket.send_bytes(chunk.ljust(CHUNK_BYTES, b"\0") if chunk else silence)
                if not self._pending:
                    self._drained.set()
        except Exception as error:
            self._stopped = error
            self._drained.set()  # wake a waiting ``say`` so it reports the failure
            raise


class Listener:
    """Records what a web transport socket sends: its status messages in order, and when output
    audio arrived. ``run`` records until the socket closes or the task is cancelled."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self._socket = socket
        self.statuses: list[dict[str, Any]] = []
        """Every ``status`` message, oldest first."""
        self.audio_times: list[float] = []
        """``time.monotonic()`` of every output audio message, oldest first."""

    async def run(self) -> None:
        async for message in self._socket:
            if message.type == aiohttp.WSMsgType.BINARY:
                self.audio_times.append(time.monotonic())
            elif message.type == aiohttp.WSMsgType.TEXT:
                body = json.loads(message.data)
                if body.get("type") == "status":
                    self.statuses.append(body)

    def first_finished_task(self) -> tuple[int, dict[str, Any]] | None:
        """The index in ``statuses`` of the first task list in which a task is in a terminal state,
        and that task; None while no task has finished."""
        for index, status in enumerate(self.statuses):
            for task in status.get("tasks", []):
                if task["state"] in TERMINAL_STATES:
                    return index, task
        return None

    def assistant_lines(self, after: int) -> list[str]:
        """The text of every assistant transcript line in ``statuses`` after index ``after``."""
        transcripts = (status.get("transcript") for status in self.statuses[after + 1 :])
        return [line["text"] for line in transcripts if line and line["speaker"] == "assistant"]

    def finished_speaking_since(self, since: float) -> bool:
        """Whether output audio arrived after ``since`` and then paused for ``ANNOUNCEMENT_END_S``."""
        last = self.audio_times[-1] if self.audio_times else since
        return last > since and time.monotonic() - last > ANNOUNCEMENT_END_S


@asynccontextmanager
async def voice_call(hermes: RealHermes, person: str) -> AsyncIterator[tuple[Microphone, Listener]]:
    """A web transport connection signed in as ``person``, with its microphone sending and its
    listener recording until the block ends."""
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(f"{hermes.web_url}/ws", protocols=("bearer", TOKENS[person])) as socket,
    ):
        microphone, listener = Microphone(socket), Listener(socket)
        running = [asyncio.create_task(microphone.run()), asyncio.create_task(listener.run())]
        try:
            yield microphone, listener
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)


async def until[T](check: Callable[[], T | None]) -> T:
    """Poll ``check`` until it returns something truthy, and return that; the caller bounds the wait."""
    while not (result := check()):
        await asyncio.sleep(0.1)
    return result


async def test_rob_hears_the_stock_picker_report_on_his_portfolio(live_hermes: RealHermes) -> None:
    silence = load_wav(SILENCE)

    async with voice_call(live_hermes, "rob") as (microphone, listener):
        await microphone.say(silence)
        await microphone.say(load_wav("portfolio.wav"))
        asked = time.monotonic()

        async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT_S):
            finished_at, task = await until(listener.first_finished_task)
            assert (task["profile"], task["state"]) == ("stock-picker", "completed"), task
            finished = time.monotonic()
            await until(lambda: listener.finished_speaking_since(finished))
        print(f"spoken result ended {time.monotonic() - asked:.1f} s after the question")

        # GPT-Live marks no end of the model's turns, so the session ends the model's transcript line
        # when the user speaks next: ask the follow-up to get the announcement's line.
        await microphone.say(silence)
        await microphone.say(load_wav("tesla.wav"))
        async with asyncio.timeout(FOLLOW_UP_TIMEOUT_S):
            announcement = await until(lambda: " ".join(listener.assistant_lines(after=finished_at)).strip())
        print(f"announcement transcript: {announcement!r}")

    runs = [record for record in live_hermes.tasks().values() if record["run_id"] is not None]
    assert runs and runs[0]["profile"] == "stock-picker"
    assert live_hermes.run_status("stock-picker", runs[0]["run_id"])["status"] == "completed"
