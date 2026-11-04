"""A Discord voice channel joined to a voice session, as the service joins them: who is credited
with a request when several people talk, whose active profile it runs on, and that a stranger's
voice never reaches the provider.

The Discord conversation (20 ms mix tick, allowlist mixer, resampler, pacer), the voice session,
the knn router, the permission check, the Hermes client, and the task history are the real ones.
The voice client and the DAVE receiver are stand-ins (``tests/fakes/fake_discord.py``), so the test
feeds each person's decoded 48 kHz frames itself, timed against the audio the provider has heard.
The mix tick runs at real-time pace, so the test takes about as long as its audio, some 6 s.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from hermes_duplex.audio.frames import FRAME_MS
from hermes_duplex.config import AudioConfig, load_config
from hermes_duplex.dispatch.hermes_api import HermesApiClient
from hermes_duplex.people import PermissionChecker
from hermes_duplex.profiles import ActiveProfiles
from hermes_duplex.providers.fake.adapter import FakeProvider
from hermes_duplex.providers.gpt_live.prompt import frame_result
from hermes_duplex.router.composite import CompositeRouter
from hermes_duplex.router.factory import build_backends
from hermes_duplex.service import prompt_context
from hermes_duplex.session import RESULT_CAP_TOKENS, VoiceSession
from hermes_duplex.tasks import TaskTracker
from hermes_duplex.transports.discord.bot import DiscordConversation
from tests.fakes.fake_discord import ALEX_ID, ROB_ID, FakeVoiceChannel, ReceiverRecorder
from tests.fakes.fake_embedder import fake_embed
from tests.fakes.fake_hermes_api import FakeHermesApi, RunScript

PHRASES = Path(__file__).resolve().parents[1] / "fixtures" / "router_eval" / "phrases.yaml"
KEYS = {"general": "general-key-0123456789", "stock-picker": "stocks-key-0123456789"}
RATE = 24000

# Who talks when, on the provider's input timeline (ms). The first request comes at 3000 ms and the
# second at 5200 ms; each is credited from the 4 s before it.
SCHEDULE: tuple[tuple[int, int, str], ...] = (
    (0, 800, "rob"),
    (800, 3000, "alex"),
    (3000, 4000, "stranger"),  # not in config's people: never mixed
    (4000, 5200, "alex"),
)
END_MS = 5600
SCRIPT = [
    {
        "at_input_ms": 3000,
        "event": {"type": "delegation", "id": "c1", "tool_args": {"task": "what's the weather tomorrow"}},
    },
    {
        "at_input_ms": 5200,
        "event": {"type": "delegation", "id": "c2", "tool_args": {"task": "how is my portfolio doing"}},
    },
]


class _ListeningProvider(FakeProvider):
    """The scripted fake provider, also keeping every chunk of input audio it was sent."""

    def __init__(self, script: Iterable[Mapping[str, Any]]) -> None:
        super().__init__(script)
        self.heard: list[bytes] = []

    async def send_audio(self, pcm16: bytes) -> None:
        self.heard.append(pcm16)
        await super().send_audio(pcm16)

    def heard_between(self, start_ms: int, end_ms: int) -> np.ndarray:
        """The input samples received in [start_ms, end_ms) of the provider's input timeline."""
        samples = np.frombuffer(b"".join(self.heard), dtype="<i2")
        return samples[start_ms * RATE // 1000 : end_ms * RATE // 1000]

    def said(self, method: str) -> list[dict[str, Any]]:
        return [dict(call.args) for call in self.calls if call.method == method]


def _voice_frame() -> bytes:
    """20 ms of a loud 440 Hz tone as Discord delivers it: 48 kHz stereo PCM16."""
    wave = (8000 * np.sin(2 * np.pi * 440 * np.arange(960) / 48000)).astype("<i2")
    return np.repeat(wave, 2).tobytes()


def _speaker_at(t_ms: int) -> str | None:
    return next((person for start, end, person in SCHEDULE if start <= t_ms < end), None)


async def _feed(provider: _ListeningProvider, on_frame: Callable[[str, bytes], None]) -> None:
    """Deliver one frame per 20 ms tick from whoever the schedule says is talking at that point of
    the provider's input, a frame ahead so the mix tick finds it waiting."""
    frame = _voice_frame()
    while provider.input_ms < END_MS:
        if (person := _speaker_at(provider.input_ms + FRAME_MS)) is not None:
            on_frame(person, frame)
        await asyncio.sleep(FRAME_MS / 1000)


def _permissions(tmp_path: Path) -> tuple[Any, PermissionChecker]:
    profiles = yaml.safe_load(PHRASES.read_text(encoding="utf-8"))["profiles"]
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": profiles,
        "people": {
            "rob": {"discord_id": str(ROB_ID), "profiles": ["*"]},
            "alex": {"discord_id": str(ALEX_ID), "profiles": ["general"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path, env={})
    return config, PermissionChecker(config)


async def _wait_until(condition: Callable[[], bool], timeout_s: float) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.02)


@pytest.fixture
async def api() -> AsyncIterator[FakeHermesApi]:
    async with FakeHermesApi(KEYS) as fake:
        fake.run_scripts = [
            RunScript(events=[{"event": "run.started"}, {"event": "run.completed", "output": output}])
            for output in ("sunny", "up 2%")
        ]
        yield fake


async def test_request_is_credited_to_who_talked_most_and_a_stranger_is_never_heard(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    config, permissions = _permissions(tmp_path)
    router = CompositeRouter(config.router, config.profiles, build_backends(config, embed=fake_embed))
    await router.configure()
    hermes = HermesApiClient(api.api_base, KEYS.get)
    tracker = TaskTracker(tmp_path / "home")
    receivers = ReceiverRecorder()
    channel = FakeVoiceChannel()
    conversation = DiscordConversation(
        "discord-lounge",
        await channel.connect(),
        permissions=permissions,
        audio=AudioConfig(),
        input_rate=RATE,
        output_rate=RATE,
        receiver_factory=receivers,
    )
    provider = _ListeningProvider(SCRIPT)
    active_profiles = ActiveProfiles(
        tmp_path / "home" / "duplex" / "active_profiles.json", permissions, config
    )
    session = VoiceSession(
        conversation,
        provider,
        router=router,
        hermes=hermes,
        tracker=tracker,
        permissions=permissions,
        active_profiles=active_profiles,
        prompt_context_for=lambda profile: prompt_context(config, profile),
        default_profile=config.router.default_profile,
    )
    conversation.start()
    running = asyncio.create_task(session.run())
    try:
        await _feed(provider, receivers.built[-1].on_frame)
        await _wait_until(
            lambda: len(provider.said("deliver_result")) == 2 and len(provider.said("say")) >= 2, 5
        )
    finally:
        await conversation.close("test over")
        await asyncio.wait_for(running, timeout=5)
        await hermes.close()
        await tracker.close()
        await router.close()

    # Both requests are alex's, so both run on his active profile, the only one he may use.
    weather, portfolio = api.requests_to("POST", "/runs")
    assert [start.path for start in (weather, portfolio)] == ["/p/general/v1/runs"] * 2
    assert {weather.body["session_id"], portfolio.body["session_id"]} == {"duplex-alex-general"}
    assert (weather.body["input"], portfolio.body["input"]) == (
        "what's the weather tomorrow",
        "how is my portfolio doing",
    )
    for number in (1, 2):
        task = tracker.get(number)
        assert task is not None
        assert (task.person, task.profile, task.state) == ("alex", "general", "completed")

    assert [result["text"] for result in provider.said("deliver_result")] == [
        frame_result("general", "sunny", RESULT_CAP_TOKENS),
        frame_result("general", "up 2%", RESULT_CAP_TOKENS),
    ]
    assert [call["text"] for call in provider.said("say")] == [
        'Started "what\'s the weather tomorrow" on the general agent.',
        'Started "how is my portfolio doing" on the general agent.',
    ]

    # The mix carries rob and alex; while only the stranger talks it holds nothing but the
    # resampler's rounding noise (a sample or two), far below the 8000 peak of their voice.
    assert np.abs(provider.heard_between(100, 700)).max() > 1000
    assert np.abs(provider.heard_between(1000, 2900)).max() > 1000
    assert np.abs(provider.heard_between(3100, 3900)).max() <= 2
