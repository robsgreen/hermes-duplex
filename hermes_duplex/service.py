"""The duplex service: every part wired together, run the same way inside the Hermes gateway and alone.

``build_service`` is the one factory both modes use. The ``DuplexService`` it returns starts the
enabled transports and gives each conversation they accept its own provider session and
``VoiceSession``; ``stop`` tears all of it down within ``STOP_TIMEOUT_S``.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .dispatch.hermes_api import HermesApiClient
from .people import PermissionChecker
from .prompts.base import PromptContext
from .providers.registry import ProviderFactory, provider_factory
from .router.composite import CompositeRouter
from .router.factory import build_backends
from .router.knn import Embedder
from .session import VoiceSession
from .tasks import Task, TaskTracker
from .transports.base import Conversation, Transport
from .transports.discord.bot import DiscordTransport
from .transports.web.server import WebTransport

logger = logging.getLogger(__name__)

STOP_TIMEOUT_S = 5.0
"""``stop`` returns within this many seconds, abandoning whatever has not finished by then."""

SESSION_STOP_GRACE_S = 1.0
"""How long ``stop`` lets cancelled sessions finish (closing their provider) before abandoning them."""


@dataclass(frozen=True)
class ServiceStatus:
    """What the service is doing right now."""

    provider: str
    """The configured provider name."""

    conversations: tuple[str, ...]
    """Ids of the conversations that have a voice session, oldest first."""

    tasks: tuple[Task, ...]
    """Every task Hermes has admitted and not finished (queued or running), by task number."""


class DuplexService:
    """Runs one voice session per conversation that the transports accept.

    Every session shares the router (so a speaker's sticky profile follows them across
    conversations), the Hermes client, the task tracker, and the permission checker; each gets its
    own provider session from ``provider_factory``. Use it from one event loop.
    """

    def __init__(
        self,
        config: Config,
        *,
        provider_factory: ProviderFactory,
        router: CompositeRouter,
        hermes: HermesApiClient,
        tracker: TaskTracker,
        permissions: PermissionChecker,
        transports: Sequence[Transport],
    ) -> None:
        self._config = config
        self._prompt_context = prompt_context(config)
        self._provider_factory = provider_factory
        self._router = router
        self._hermes = hermes
        self._tracker = tracker
        self._permissions = permissions
        self.transports = tuple(transports)
        """The enabled transports, in start order."""
        self._sessions: dict[asyncio.Task[None], Conversation] = {}
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        """Restore the tasks a previous run left open, configure the router, then start every
        transport; returns once they accept conversations.

        The restored tasks stay unattended until a conversation hears their person
        (``TaskTracker.restore``).
        Raises whatever a transport raises when it cannot start (for example, the web port is taken),
        after stopping the transports that had started.
        """
        await self._tracker.restore()
        await self._router.configure()
        self._started = True
        try:
            for transport in self.transports:
                await transport.start(self._run_conversation)
        except BaseException:
            await self.stop()
            raise
        logger.info(
            "duplex service started: provider %s, transports %s",
            self._config.provider.name,
            ", ".join(transport.name for transport in self.transports) or "none",
        )

    async def stop(self) -> None:
        """Stop everything within ``STOP_TIMEOUT_S``, logging anything it had to abandon.

        Cancels every voice session (each closes its provider socket and leaves its tasks unattended;
        the Hermes runs themselves continue, and the next ``start`` restores them from the task
        history), then stops the transports, which closes their
        conversations and the web server, then closes the task tracker, the Hermes client, and the
        router's backends.
        Conversations that arrive meanwhile are closed at once. Idempotent; does nothing before
        ``start``.
        """
        if not self._started or self._stopping:
            return
        self._stopping = True
        started = time.monotonic()
        try:
            async with asyncio.timeout(STOP_TIMEOUT_S):
                await self._stop_sessions()
                for transport in self.transports:
                    await transport.stop()
                await self._tracker.close()
                await self._hermes.close()
                await self._router.close()
        except TimeoutError:
            logger.warning(
                "stopping the duplex service took over %g s; abandoned the rest of shutdown", STOP_TIMEOUT_S
            )
        logger.info("duplex service stopped in %.2f s", time.monotonic() - started)

    def status(self) -> ServiceStatus:
        """A snapshot of the active conversations and the tasks Hermes is working on."""
        tasks = sorted(
            (task for person in self._config.people for task in self._tracker.active_for(person)),
            key=lambda task: task.number,
        )
        return ServiceStatus(
            provider=self._config.provider.name,
            conversations=tuple(conversation.id for conversation in self._sessions.values()),
            tasks=tuple(tasks),
        )

    async def _run_conversation(self, conversation: Conversation) -> None:
        """Run one voice session for ``conversation`` until either ends. Transports call this."""
        if self._stopping:
            await conversation.close("server stopping")
            return
        task = asyncio.current_task()
        assert task is not None, "transports call this from a task"
        provider = self._provider_factory(self._config.provider, self._config.secrets, self._prompt_context)
        session = VoiceSession(
            conversation,
            provider,
            router=self._router,
            hermes=self._hermes,
            tracker=self._tracker,
            permissions=self._permissions,
            voice=self._config.provider.voice,
            pacer_lead_ms=self._config.audio.pacer_lead_ms,
            gate_threshold_dbfs=self._config.audio.gate_threshold_dbfs,
        )
        self._sessions[task] = conversation
        try:
            await session.run()
        finally:
            del self._sessions[task]

    async def _stop_sessions(self) -> None:
        """Cancel every session and wait ``SESSION_STOP_GRACE_S`` for them to close their providers.

        A session still running after that (its provider's ``close`` hangs) is cancelled again, which
        interrupts the hanging close, and abandoned.
        """
        sessions = dict(self._sessions)
        if not sessions:
            return
        for task in sessions:
            task.cancel()
        _, hung = await asyncio.wait(sessions, timeout=SESSION_STOP_GRACE_S)
        for task in hung:
            logger.warning(
                "voice session for conversation %s did not stop within %g s; abandoned it",
                sessions[task].id,
                SESSION_STOP_GRACE_S,
            )
            task.cancel()


def build_service(config: Config, *, embed: Embedder | None = None) -> DuplexService:
    """Build the service ``config`` describes, without starting anything.

    ``embed`` replaces the knn router backend's embedding model (tests pass a deterministic one);
    by default the backend loads its model2vec model on ``start``.

    Raises ``ConfigError`` for an unknown provider or router backend name, or a router backend whose
    settings are missing.
    """
    permissions = PermissionChecker(config)
    providers = provider_factory(config.provider.name)
    transports: list[Transport] = []
    if config.transports.discord.enabled:
        transports.append(_discord_transport(config, permissions, providers))
    if config.transports.web.enabled:
        transports.append(WebTransport(config.transports.web, config.audio, permissions))

    return DuplexService(
        config,
        provider_factory=providers,
        router=CompositeRouter(config.router, config.profiles, build_backends(config, embed=embed)),
        hermes=HermesApiClient(config.hermes.api_base, lambda profile: _profile_key(config, profile)),
        tracker=TaskTracker(config.hermes_home, restore_max_age_h=config.tasks.restore_max_age_h),
        permissions=permissions,
        transports=transports,
    )


def _discord_transport(
    config: Config, permissions: PermissionChecker, providers: ProviderFactory
) -> DiscordTransport:
    """The Discord bot, converting audio to and from the configured provider's sample rates."""
    token_env = config.transports.discord.bot_token_env
    assert token_env is not None, "config loading requires bot_token_env when Discord is enabled"
    # Building a provider opens nothing; it is the one place that knows the provider's rates.
    capabilities = providers(config.provider, config.secrets, prompt_context(config)).capabilities
    return DiscordTransport(
        config.secrets.get(token_env),
        config.audio,
        permissions,
        input_rate=capabilities["input_rate"],
        output_rate=capabilities["output_rate"],
    )


def prompt_context(config: Config) -> PromptContext:
    """What every provider's prompt is written from: the ``assistant`` section and the profiles."""
    assistant = config.assistant
    return PromptContext(
        assistant_name=assistant.name,
        persona=assistant.persona,
        backchannel=assistant.backchannel,
        profiles=tuple((name, profile.description) for name, profile in config.profiles.items()),
    )


def _profile_key(config: Config, profile: str) -> str | None:
    """The ``API_SERVER_KEY`` of ``profile``, or None when config names no key for it."""
    env_name = config.hermes.profile_keys_env.get(profile)
    return config.secrets.get(env_name) if env_name is not None else None
