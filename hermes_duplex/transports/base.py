"""The interface every transport (Discord, web) implements.

A transport accepts conversations; each ``Conversation`` streams mixed input audio in and plays the
provider's output audio back.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class InputFrame:
    """20 ms of input audio from one conversation, already mixed and filtered to allowed people."""

    pcm16_mono: bytes
    """Mono PCM16 audio at ``rate``."""

    rate: int
    """Sample rate of ``pcm16_mono`` in Hz."""

    voiced_speakers: frozenset[str]
    """Ids of the people voiced in this frame; empty when nobody spoke."""

    t_ms: int
    """Milliseconds of input this conversation produced before this frame (frame count × 20), from 0
    when the conversation starts; it never restarts. Informational only: the session keys speaker
    attribution and transcripts by its own count of input ms sent to each provider session (from 0 at
    each connect), which provider timestamps such as GPT-Live delegation offsets refer to."""


class Transport(Protocol):
    """A source of conversations, such as a Discord bot or the web server."""

    name: str

    async def start(self, on_conversation: Callable[[Conversation], Awaitable[None]]) -> None:
        """Start accepting conversations, calling ``on_conversation`` for each new one."""
        ...

    async def stop(self) -> None:
        """Stop accepting conversations and close the open ones."""
        ...


class Conversation(Protocol):
    """One live voice conversation (a Discord voice channel or a web socket)."""

    id: str

    @property
    def person(self) -> str | None:
        """The name of the person the conversation is for, whose active profile the voice prompt
        starts on: the one person signed in (web), or the person who brought the bot into the voice
        channel (Discord); None when there is none, and the prompt starts on the default profile."""
        ...

    def present(self, person: str) -> bool:
        """Whether ``person`` (a name from config's ``people``) is in the conversation now: signed in
        to it (web), or in its voice channel (Discord)."""
        ...

    def frames(self) -> AsyncIterator[InputFrame]:
        """Yield one ``InputFrame`` per 20 ms tick until the conversation ends."""
        ...

    async def play(self, pcm16_mono: bytes, rate: int) -> None:
        """Queue mono PCM16 audio at ``rate`` Hz for playback."""
        ...

    async def flush_playback(self) -> None:
        """Discard the playback queued at the conversation, so playback stops within one 20 ms tick.

        The session paces output, so at most its pacer's lead is queued here; it counts what was
        heard from its own pacer.
        """
        ...

    async def notify(self, event: dict[str, Any]) -> None:
        """Send a transcript or task update to the user interface; a no-op where there is none."""
        ...

    async def close(self, reason: str) -> None:
        """End the conversation; ``reason`` is for logs and, where shown, the user."""
        ...
