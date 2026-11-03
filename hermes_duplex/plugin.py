"""Hermes plugin entry point: the ``/duplex`` command, the ``hermes duplex`` CLI, and the in-gateway service.

Hermes runs ``register()`` on a plugin-load worker thread with no event loop, so the service cannot
be started there (``ctx.spawn_task`` needs a running loop). Instead ``register()`` registers a gateway
platform named ``duplex`` whose adapter starts the service when the gateway connects it, on the
gateway's loop, and stops it when the gateway disconnects it or unloads the plugin. The adapter
carries no messages. docs/system/architecture.md ("Gateway lifecycle seam") has the evidence.

Only documented ``ctx`` methods are used, and no Hermes module is imported: the adapter duck-types
the gateway's platform adapter interface.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import cli
from .config import ConfigError, load_config, resolve_config_path

if TYPE_CHECKING:
    from .service import DuplexService

logger = logging.getLogger(__name__)

COMMAND_NAME = "duplex"
"""The Hermes slash command (``/duplex``) and CLI command (``hermes duplex``)."""

PLATFORM_NAME = "duplex"
"""The gateway platform whose adapter runs the service. Enable it in Hermes's ``config.yaml`` with
``platforms: {duplex: {enabled: true}}``."""


def register(ctx: Any) -> None:
    """Register hermes-duplex with Hermes.

    Registers the ``/duplex`` status command and the ``hermes duplex`` CLI command. When the config
    file (``resolve_config_path()``) exists, also registers the ``duplex`` gateway platform that runs
    the service and an unload callback that stops it; when it is missing, logs one warning and
    starts nothing, and the gateway keeps working. Never raises: ``hermes plugins validate`` calls
    it with a recording context, and a Hermes that refuses platform plugins (``plugins.isolation:
    host``) only loses the in-gateway service.
    """
    lifecycle = GatewayLifecycle(resolve_config_path())
    ctx.register_command(
        COMMAND_NAME,
        lifecycle.status_text,
        description="hermes-duplex status: active conversations, running tasks, provider",
    )
    ctx.register_cli_command(
        COMMAND_NAME,
        help="hermes-duplex voice service",
        setup_fn=cli.add_commands,
        handler_fn=cli.run,
        description="Run the hermes-duplex voice service, score its router, or check its setup (doctor).",
    )

    if not lifecycle.config_path.is_file():
        logger.warning(
            "hermes-duplex: no config at %s; the voice service will not start", lifecycle.config_path
        )
        return
    try:
        ctx.register_platform(
            name=PLATFORM_NAME,
            label="hermes-duplex voice",
            adapter_factory=lifecycle.adapter,
            check_fn=lambda: True,
            is_connected=lambda platform_config: lifecycle.config_path.is_file(),
            install_hint="Enable it with platforms: {duplex: {enabled: true}} in config.yaml",
        )
    except RuntimeError as error:
        # Hermes refuses platform adapters from plugins it runs out of process (``plugins.isolation:
        # host``); the standalone command still works.
        logger.warning(
            "hermes-duplex: cannot run the voice service in this gateway (%s); use `hermes duplex serve`",
            error,
        )
        return
    ctx.on_unload(lifecycle.unload)


class GatewayLifecycle:
    """Starts and stops the one service of this Hermes process, and reports its status.

    ``start`` and ``stop`` run on the gateway's event loop; ``unload`` and ``status_text`` may be
    called from any thread.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        """The config file the service loads on ``start``."""
        self._service: DuplexService | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unload_stop: asyncio.Task[None] | None = None
        """The stop ``unload`` scheduled on the service's loop; kept so the task is not collected."""
        self._starting = asyncio.Lock()
        """Held while starting, so adapters connecting at once (one per profile) start one service."""

    def adapter(self, platform_config: Any) -> GatewayAdapter:
        """The gateway's ``adapter_factory``: an adapter for this lifecycle."""
        return GatewayAdapter(self, platform_config)

    async def start(self) -> None:
        """Load the config and start the service; does nothing when it is running.

        Raises ``ConfigError`` for an invalid config, or one the service cannot start with, without
        starting anything.
        """
        # Imported here: Hermes imports this module in every ``hermes`` process, and the service
        # pulls in aiohttp, numpy, and the transports.
        from .service import build_service

        async with self._starting:
            if self._service is not None:
                return
            service = build_service(load_config(self.config_path))
            await service.start()
            self._service = service
            self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        """Stop the service (within ``service.STOP_TIMEOUT_S``); does nothing when it is not running."""
        service, self._service = self._service, None
        if service is not None:
            await service.stop()

    def unload(self) -> None:
        """Hermes's unload callback: stop the service on the loop it runs on.

        From another thread, waits for the stop to finish. On the service's own loop it cannot block,
        so it schedules the stop and returns.
        """
        loop = self._loop
        if self._service is None or loop is None or not loop.is_running():
            return
        try:
            on_service_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_service_loop = False
        if on_service_loop:
            self._unload_stop = loop.create_task(self.stop())
            return
        from .service import STOP_TIMEOUT_S

        stopping = asyncio.run_coroutine_threadsafe(self.stop(), loop)
        try:
            stopping.result(timeout=STOP_TIMEOUT_S + 1)
        except TimeoutError:
            logger.warning("hermes-duplex: the voice service did not stop in time on unload")

    def status_text(self, raw_args: str = "") -> str:
        """The ``/duplex`` command: active conversations, running tasks, and the provider."""
        service = self._service
        if service is None:
            return f"hermes-duplex is not running in this process (config: {self.config_path})."
        status = service.status()
        lines = [f"hermes-duplex is running with provider {status.provider}."]
        if status.conversations:
            lines.append(f"Conversations: {', '.join(status.conversations)}")
        else:
            lines.append("No active conversations.")
        if status.tasks:
            lines.append("Tasks:")
            lines.extend(
                f"- #{task.number} {task.profile} for {task.person} ({task.state}): {task.text}"
                for task in status.tasks
            )
        else:
            lines.append("No running tasks.")
        return "\n".join(lines)


@dataclass(frozen=True)
class PlatformId:
    """Stands in for the gateway's ``Platform`` member, which only Hermes's own modules can name."""

    value: str

    @property
    def name(self) -> str:
        return self.value.upper()


@dataclass(frozen=True)
class SendResult:
    """The result shape the gateway expects from ``send``."""

    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: Any = None
    retryable: bool = False
    retry_after: float | None = None


class GatewayAdapter:
    """The ``duplex`` gateway platform adapter: connecting it starts the service, disconnecting stops it.

    It implements the parts of the gateway's platform adapter interface the gateway calls on an
    adapter that receives and sends nothing: the gateway's handler setters are accepted and
    ignored, and ``send`` always fails. A config the service cannot start with is reported as a
    fatal, non-retryable error.
    """

    platform = PlatformId(PLATFORM_NAME)
    send_path_degraded = False
    DEGRADED_STATUS_MESSAGE = ""

    def __init__(self, lifecycle: GatewayLifecycle, config: Any) -> None:
        self.config = config
        """The gateway's ``PlatformConfig`` for this platform; unused."""
        self.gateway_runner: Any = None
        """Set by the gateway; unused."""
        self.has_fatal_error = False
        self.fatal_error_code: str | None = None
        self.fatal_error_message: str | None = None
        self.fatal_error_retryable = True
        self._lifecycle = lifecycle

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the service; False, with the fatal error set, when its config cannot start it."""
        try:
            await self._lifecycle.start()
        except ConfigError as error:
            logger.error("hermes-duplex: cannot start the voice service: %s", error)
            self.has_fatal_error = True
            self.fatal_error_code = "config_error"
            self.fatal_error_message = str(error)
            self.fatal_error_retryable = False
            return False
        self.has_fatal_error = False
        self.fatal_error_code = self.fatal_error_message = None
        return True

    async def disconnect(self) -> None:
        await self._lifecycle.stop()

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, **kwargs: Any
    ) -> SendResult:
        return SendResult(success=False, error="hermes-duplex does not deliver text messages")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    async def handle_message(self, event: Any) -> None:
        pass

    async def cancel_background_tasks(self) -> None:
        pass

    def get_pending_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    def _ignore(self, *args: Any, **kwargs: Any) -> None:
        pass

    set_message_handler = set_fatal_error_handler = set_session_store = set_busy_session_handler = _ignore
    set_topic_recovery_fn = set_authorization_check = set_platform_event_handler = _ignore
