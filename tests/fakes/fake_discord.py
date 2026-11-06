"""Stand-ins for the discord.py voice objects the Discord transport touches, and its voice receiver."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import discord
import yaml

from hermes_duplex.config import load_config
from hermes_duplex.people import PermissionChecker
from hermes_duplex.transports.discord.bot import Receiver


class FakeVoiceClient:
    """A connected ``discord.VoiceClient``: records what is played and whether it disconnected."""

    def __init__(self, channel: FakeVoiceChannel) -> None:
        self.channel = channel
        self.source: discord.AudioSource | None = None
        self.stopped = False
        self.disconnected = False

    def play(self, source: discord.AudioSource) -> None:
        self.source = source

    def stop(self) -> None:
        self.stopped = True

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnected = True


class FakeGuild:
    """A guild whose member cache tests fill: ``members`` maps a member id to an object with ``bot``."""

    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.members: dict[int, Any] = {}

    def get_member(self, member_id: int) -> Any:
        return self.members.get(member_id)


class FakeVoiceChannel:
    """A voice channel whose ``connect`` attempts follow a script, and whose text chat is recorded.

    Each ``connect`` takes the next entry of ``connect_results``: an exception to raise, or None to
    succeed with a new ``FakeVoiceClient``. Once the script runs out, every attempt succeeds.
    """

    def __init__(self, channel_id: int = 500, guild_id: int = 900, name: str = "lounge") -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.guild = FakeGuild(guild_id)
        self.connect_results: list[BaseException | None] = []
        self.voice_clients: list[FakeVoiceClient] = []
        self.sent: list[str] = []
        self.voice_states: dict[int, Any] = {}
        """Member id → voice state of each member in the channel; tests add and remove members."""

    async def connect(self, **kwargs: Any) -> discord.VoiceClient:
        if self.connect_results and (error := self.connect_results.pop(0)) is not None:
            raise error
        self.voice_clients.append(FakeVoiceClient(self))
        return cast(discord.VoiceClient, self.voice_clients[-1])

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeReceiver:
    """A ``VoiceReceiver`` stand-in: tests call ``on_frame`` to deliver a person's 20 ms frame."""

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        permissions: PermissionChecker,
        on_frame: Callable[[str, bytes], None],
    ) -> None:
        self.voice_client = voice_client
        self.on_frame = on_frame
        self.started = False
        self.stopped = False
        self.refreshes: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def refresh_credentials(self, reason: str) -> None:
        self.refreshes.append(reason)


class ReceiverRecorder:
    """A receiver factory that keeps every ``FakeReceiver`` it builds, oldest first."""

    def __init__(self) -> None:
        self.built: list[FakeReceiver] = []

    def __call__(
        self,
        voice_client: discord.VoiceClient,
        permissions: PermissionChecker,
        on_frame: Callable[[str, bytes], None],
    ) -> Receiver:
        self.built.append(FakeReceiver(voice_client, permissions, on_frame))
        return self.built[-1]


ROB_ID = 123
ALEX_ID = 456
STRANGER_ID = 999


def allowlist(tmp_path: Path) -> PermissionChecker:
    """Permissions for two people on Discord: ``rob`` (``ROB_ID``) and ``alex`` (``ALEX_ID``)."""
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:8642"},
        "provider": {"name": "fake"},
        "router": {"backend": "knn"},
        "profiles": {"general": {}},
        "people": {
            "rob": {"discord_id": str(ROB_ID), "profiles": ["*"]},
            "alex": {"discord_id": str(ALEX_ID), "profiles": ["*"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PermissionChecker(load_config(path, env={}))
