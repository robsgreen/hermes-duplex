"""The duplex service: every part wired together, run the same way inside the Hermes gateway and alone.

``build_service`` is the one factory both modes use. The ``DuplexService`` it returns starts the
enabled transports and gives each conversation they accept its own provider session and
``VoiceSession``; ``stop`` tears all of it down within ``STOP_TIMEOUT_S``.
"""

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .config import Config
from .dispatch.hermes_api import HermesApiClient
from .hermes_context import HermesContextFiles, HermesWriter
from .people import PermissionChecker
from .profiles import ActiveProfiles
from .prompts.base import OPEN_TASKS_MAX, PromptContext
from .prompts.notices import topic
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

ACTIVE_PROFILES_FILE_NAME = "active_profiles.json"
"""The file under ``$HERMES_HOME/duplex/`` that keeps each person's active profile across restarts."""

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

    active_profiles: Mapping[str, str]
    """Each person in config's ``people``, in config order, to their active profile."""


class DuplexService:
    """Runs one voice session per conversation that the transports accept.

    Every session shares the router, the Hermes client, the task tracker, the permission checker, and
    the active-profile store (so a person's active profile follows them across conversations); each
    gets its own provider session from ``provider_factory``. Use it from one event loop.
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
        active_profiles: ActiveProfiles,
        context_files: HermesContextFiles,
        transports: Sequence[Transport],
    ) -> None:
        self._config = config
        self._prompt_context = prompt_context(config)
        self._provider_factory = provider_factory
        self._router = router
        self._hermes = hermes
        self._tracker = tracker
        self._permissions = permissions
        self._active_profiles = active_profiles
        self._context_files = context_files
        self.transports = tuple(transports)
        """The enabled transports, in start order."""
        self._sessions: dict[asyncio.Task[None], Conversation] = {}
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        """Restore the tasks a previous run left open, load each person's saved active profile,
        configure the router, start having Hermes write the context texts that are missing or stale
        (in the background: every profile's file rewrites, and the briefing on the owner's active
        profile), then start every transport; returns once they accept conversations.

        The restored tasks stay unattended until a conversation hears their person
        (``TaskTracker.restore``).
        Raises whatever a transport raises when it cannot start (for example, the web port is taken),
        after stopping the transports that had started.
        """
        await self._tracker.restore()
        self._active_profiles.load()
        await self._router.configure()
        self._context_files.refresh_stale(self._config.profiles, ("soul", "user"))
        if (owner := hermes_owner(self._config)) is not None:
            self._context_files.refresh_stale([self._active_profiles.get(owner)], ("briefing",))
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
        conversations and the web server, then closes the task tracker, cancels context-file
        rewrites still running, and closes the Hermes client and the router's backends.
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
                await self._context_files.close()
                await self._hermes.close()
                await self._router.close()
        except TimeoutError:
            logger.warning(
                "stopping the duplex service took over %g s; abandoned the rest of shutdown", STOP_TIMEOUT_S
            )
        logger.info("duplex service stopped in %.2f s", time.monotonic() - started)

    def status(self) -> ServiceStatus:
        """A snapshot of the active conversations, the tasks Hermes is working on, and each person's
        active profile."""
        tasks = sorted(
            (task for person in self._config.people for task in self._tracker.active_for(person)),
            key=lambda task: task.number,
        )
        return ServiceStatus(
            provider=self._config.provider.name,
            conversations=tuple(conversation.id for conversation in self._sessions.values()),
            tasks=tuple(tasks),
            active_profiles={person: self._active_profiles.get(person) for person in self._config.people},
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
            active_profiles=self._active_profiles,
            prompt_context_for=lambda profile, person, alone: prompt_context(
                self._config,
                profile,
                person,
                alone,
                context_files=self._context_files,
                open_tasks=self._tracker.active_for(person) if person is not None else (),
            ),
            default_profile=self._config.router.default_profile,
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
    active_profiles = ActiveProfiles(
        config.hermes_home / "duplex" / ACTIVE_PROFILES_FILE_NAME, permissions, config
    )
    providers = provider_factory(config.provider.name)
    hermes = HermesApiClient(config.hermes.api_base, lambda profile: _profile_key(config, profile))
    transports: list[Transport] = []
    if config.transports.discord.enabled:
        transports.append(_discord_transport(config, permissions, active_profiles, providers))
    if config.transports.web.enabled:
        transports.append(WebTransport(config.transports.web, config.audio, permissions, active_profiles))

    return DuplexService(
        config,
        provider_factory=providers,
        router=CompositeRouter(config.router, config.profiles, build_backends(config, embed=embed)),
        hermes=hermes,
        tracker=TaskTracker(config.hermes_home, restore_max_age_h=config.tasks.restore_max_age_h),
        permissions=permissions,
        active_profiles=active_profiles,
        context_files=build_context_files(config, hermes),
        transports=transports,
    )


def _discord_transport(
    config: Config,
    permissions: PermissionChecker,
    active_profiles: ActiveProfiles,
    providers: ProviderFactory,
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
        active_profiles=active_profiles,
        input_rate=capabilities["input_rate"],
        output_rate=capabilities["output_rate"],
    )


def build_context_files(config: Config, hermes: HermesApiClient | None = None) -> HermesContextFiles:
    """The Hermes context files ``config`` puts in the voice prompt, rewritten through ``hermes``
    (None: never rewritten, only read)."""
    return HermesContextFiles(
        config.hermes_home,
        config.hermes.voice_context_files,
        config.hermes.voice_context_max_chars,
        HermesWriter(hermes, hermes_owner(config)) if hermes is not None else None,
        briefing_refresh_h=config.hermes.briefing_refresh_h,
    )


def hermes_owner(config: Config) -> str | None:
    """The person with ``hermes_owner: true``, if any."""
    return next((name for name, person in config.people.items() if person.hermes_owner), None)


def prompt_context(
    config: Config,
    active_profile: str | None = None,
    person: str | None = None,
    person_alone: bool = False,
    *,
    context_files: HermesContextFiles | None = None,
    open_tasks: Sequence[Task] = (),
) -> PromptContext:
    """What every provider's prompt is written from: the ``assistant`` section and the profiles, and,
    when ``active_profile`` names a configured profile, that profile with its description and
    ``voice_context`` as the one the instructions speak for, and its Hermes ``SOUL.md`` from
    ``context_files``. ``person`` is who the conversation is with: their config ``voice_context`` and
    ``open_tasks`` (their queued and running tasks, of which the newest ``OPEN_TASKS_MAX`` go in) go
    in, and, when they are the Hermes owner and ``person_alone`` (nobody else in the conversation),
    the active profile's ``USER.md`` and briefing. Raises ``KeyError`` for an unknown profile."""
    assistant = config.assistant
    active = config.profiles[active_profile] if active_profile is not None else None
    who = config.people.get(person) if person is not None else None
    soul = user = briefing = None
    if context_files is not None and active_profile is not None:
        soul = context_files.read(active_profile, "soul")
        if who is not None and who.hermes_owner and person_alone:
            user = context_files.read(active_profile, "user")
            briefing = context_files.read(active_profile, "briefing")
    person_voice_context = who.voice_context if who is not None else None
    tasks = tuple((topic(task.text), task.profile) for task in open_tasks[-OPEN_TASKS_MAX:])
    says_anything = person_voice_context or user or briefing or tasks
    return PromptContext(
        assistant_name=assistant.name,
        persona=assistant.persona,
        backchannel=assistant.backchannel,
        profiles=tuple((name, profile.description) for name, profile in config.profiles.items()),
        active_profile=active_profile,
        active_profile_description=active.description if active is not None else None,
        voice_context=active.voice_context if active is not None else None,
        hermes_soul=soul.text if soul is not None else None,
        person=person if says_anything else None,
        person_voice_context=person_voice_context,
        hermes_user=user.text if user is not None else None,
        hermes_briefing=briefing.text if briefing is not None else None,
        open_tasks=tasks,
    )


def _profile_key(config: Config, profile: str) -> str | None:
    """The ``API_SERVER_KEY`` of ``profile``, or None when config names no key for it."""
    env_name = config.hermes.profile_keys_env.get(profile)
    return config.secrets.get(env_name) if env_name is not None else None
