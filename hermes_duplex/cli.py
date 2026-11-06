"""Command line: ``hermes-duplex <command>`` standalone, and ``hermes duplex <command>`` inside Hermes.

Both use the same parser (``add_commands``) and handler (``run``). Commands:

- ``serve [--config PATH]`` runs the service until SIGINT or SIGTERM, then stops it.
- ``eval-router PHRASES [--backend NAME] [--json] [--config PATH]`` scores the router against a
  file of labelled phrases and prints a table, or JSON.
- ``doctor [--config PATH]`` checks the config, its secrets, and that Hermes, the provider, and
  Discord accept them, and prints a pass/fail table.
- ``voice-context show PROFILE [--config PATH]`` prints the voice instructions for ``PROFILE`` with
  the Hermes context (``SOUL.md``, ``USER.md``, the briefing) as it would go in now, assuming the
  Hermes owner is alone on the call; ``voice-context refresh [PROFILE] [--force] [--config PATH]``
  has Hermes write it now.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .config import (
    HERMES_CONTEXT_FILES,
    OWNER_CONTEXT_FILES,
    ROUTER_BACKENDS,
    Config,
    ConfigError,
    MissingEnvError,
    Secrets,
    load_config,
)

if TYPE_CHECKING:
    from .router.knn import Embedder
    from .service import DuplexService

COMMAND_DEST = "duplex_command"
"""The ``argparse.Namespace`` attribute that holds the chosen command."""

VOICE_CONTEXT_DEST = "voice_context_command"
"""The ``argparse.Namespace`` attribute that holds the chosen ``voice-context`` command."""

_EXIT_CONFIG_ERROR = 1
_EXIT_CHECK_FAILED = 1
_EXIT_USAGE = 2


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Add the commands to ``parser``; ``hermes duplex`` passes its own subparser here."""
    commands = parser.add_subparsers(dest=COMMAND_DEST, metavar="{serve,eval-router,doctor,voice-context}")
    serve_parser = commands.add_parser("serve", help="run the voice service until interrupted")
    serve_parser.add_argument(
        "--config",
        metavar="PATH",
        help="duplex.yaml to load (default: $HERMES_DUPLEX_CONFIG, else $HERMES_HOME/duplex.yaml)",
    )
    eval_parser = commands.add_parser("eval-router", help="score the router against labelled phrases")
    eval_parser.add_argument(
        "phrases",
        metavar="PHRASES",
        help="YAML file of labelled phrases (format: hermes_duplex/router/eval.py)",
    )
    eval_parser.add_argument(
        "--backend",
        choices=(*sorted(ROUTER_BACKENDS), "composite"),
        help="score only this backend alone, or only the composite router "
        "(default: each configured backend alone, then the composite)",
    )
    eval_parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    eval_parser.add_argument(
        "--config",
        metavar="PATH",
        help="duplex.yaml whose router settings and API keys to use (default: the knn backend alone)",
    )
    doctor_parser = commands.add_parser("doctor", help="check config, secrets, and connectivity")
    doctor_parser.add_argument(
        "--config",
        metavar="PATH",
        help="duplex.yaml to check (default: $HERMES_DUPLEX_CONFIG, else $HERMES_HOME/duplex.yaml)",
    )
    context_parser = commands.add_parser(
        "voice-context", help="show or refresh the Hermes context the voice prompt carries"
    )
    context_commands = context_parser.add_subparsers(dest=VOICE_CONTEXT_DEST, metavar="{show,refresh}")
    show_parser = context_commands.add_parser("show", help="print the voice instructions for a profile")
    show_parser.add_argument("profile", metavar="PROFILE", help="the profile active on the call")
    refresh_parser = context_commands.add_parser("refresh", help="have Hermes write the voice context now")
    refresh_parser.add_argument(
        "profile",
        metavar="PROFILE",
        nargs="?",
        help="the profile whose context to write (default: every one)",
    )
    refresh_parser.add_argument(
        "--force", action="store_true", help="write even a current or hand-edited text"
    )
    for context_command in (show_parser, refresh_parser):
        context_command.add_argument(
            "--config",
            metavar="PATH",
            help="duplex.yaml to use (default: $HERMES_DUPLEX_CONFIG, else $HERMES_HOME/duplex.yaml)",
        )


def run(args: argparse.Namespace) -> int:
    """Run the command ``args`` names and return the process exit status.

    Prints errors to stderr: a config the service cannot run with or an unusable phrase file exits 1,
    a failed ``doctor`` check or ``voice-context refresh`` exits 1, and a missing command exits 2.
    """
    command = getattr(args, COMMAND_DEST, None)
    if command == "serve":
        try:
            asyncio.run(serve(load_config(args.config)))
        except ConfigError as error:
            print(f"hermes-duplex: {error}", file=sys.stderr)
            return _EXIT_CONFIG_ERROR
        return 0
    if command == "eval-router":
        return eval_router(args)
    if command == "doctor":
        return doctor(args)
    if command == "voice-context":
        return voice_context(args)
    print("hermes-duplex: choose a command: serve, eval-router, doctor, or voice-context", file=sys.stderr)
    return _EXIT_USAGE


async def serve(config: Config, *, build: Callable[[Config], DuplexService] | None = None) -> None:
    """Build and start the service, run it until SIGINT or SIGTERM, then stop it.

    ``build`` defaults to ``build_service``. Signal handlers are installed only for the duration of
    the call, so it must run on the main thread's event loop.
    """
    if build is None:
        # Imported here: Hermes loads this module in every ``hermes`` process to list the commands,
        # and the service pulls in aiohttp, numpy, and the transports.
        from .service import build_service

        build = build_service
    service = build(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for number in signals:
        loop.add_signal_handler(number, stopping.set)
    try:
        await service.start()
        await stopping.wait()
    finally:
        for number in signals:
            loop.remove_signal_handler(number)
        await service.stop()


def eval_router(args: argparse.Namespace, *, embed: Embedder | None = None) -> int:
    """Run ``eval-router`` with the parsed ``args``: print the scores to stdout and return 0, or print
    the error to stderr and return 1 when the phrase file, config, or a backend's setup is unusable.

    ``embed`` replaces the knn backend's embedding model (tests pass a deterministic one). The
    router's per-request ``route ...`` INFO lines are silenced while it runs.
    """
    # Imported here, like the service: the router pulls in numpy, model2vec, and aiohttp.
    from .router import composite
    from .router.eval import evaluate_config, format_table, load_phrases, to_json

    route_level = composite.logger.level
    composite.logger.setLevel(max(logging.WARNING, route_level))
    try:
        phrase_file = load_phrases(args.phrases)
        config = load_config(args.config) if args.config is not None else None
        reports = asyncio.run(evaluate_config(phrase_file, config, only=args.backend, embed=embed))
    except (ConfigError, ValueError) as error:
        print(f"hermes-duplex: {error}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    finally:
        composite.logger.setLevel(route_level)
    print(json.dumps(to_json(reports), indent=2) if args.json else format_table(reports))
    return 0


def doctor(args: argparse.Namespace) -> int:
    """Run ``doctor`` with the parsed ``args``: print the pass/fail table to stdout and return 0 when
    every check passed, else 1."""
    # Imported here, like the service: the checks pull in aiohttp and discord.py.
    from .doctor import diagnose, format_table

    results = asyncio.run(diagnose(args.config))
    print(format_table(results))
    return 0 if all(result.passed for result in results) else _EXIT_CHECK_FAILED


def voice_context(args: argparse.Namespace) -> int:
    """Run ``voice-context show`` or ``voice-context refresh`` with the parsed ``args`` and return the
    exit status: 1 for an unusable config, an unknown profile, or a text Hermes could not write,
    2 without a subcommand.

    Env vars the config names may be unset: ``show`` needs none, and ``refresh`` needs only the keys
    of the profiles it rewrites (a profile without one fails as not configured).
    """
    subcommand = getattr(args, VOICE_CONTEXT_DEST, None)
    if subcommand not in ("show", "refresh"):
        print("hermes-duplex: choose show or refresh", file=sys.stderr)
        return _EXIT_USAGE
    try:
        try:
            config = load_config(args.config)
        except MissingEnvError as error:
            config = error.config
    except ConfigError as error:
        print(f"hermes-duplex: {error}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    if args.profile is not None and args.profile not in config.profiles:
        print(f"hermes-duplex: profile '{args.profile}' is not defined under profiles", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    if subcommand == "show":
        assert args.profile is not None, "show takes a required PROFILE"
        show_voice_context(config, args.profile)
        return 0
    return asyncio.run(refresh_voice_context(config, args.profile, force=args.force))


def show_voice_context(config: Config, profile: str) -> None:
    """Print the configured provider's voice instructions while ``profile`` is active to stdout, and
    to stderr where each Hermes file came from and who the call is assumed to be with: the Hermes owner,
    alone, so their ``USER.md`` is in. Starts no rewrite."""
    # Imported here, like the service: the providers pull in websockets and numpy.
    from .hermes_context import FILE_NAMES
    from .providers.registry import provider_factory
    from .service import build_context_files, hermes_owner, prompt_context

    files = build_context_files(config)
    owner = hermes_owner(config)
    context = prompt_context(config, profile, owner, True, context_files=files)
    # Building a provider opens nothing, and only its prompter is used, so no real key is needed.
    key_env = config.provider.api_key_env
    secrets = Secrets({key_env: "unused"} if key_env is not None else {})
    prompter = provider_factory(config.provider.name)(config.provider, secrets, context).prompter

    notes = [f"provider {config.provider.name}, active profile {profile}"]
    if owner is None:
        notes.append("no person has hermes_owner: true, so no USER.md or briefing")
    else:
        notes.append(
            f"talking with {owner}, the Hermes owner, alone; "
            "with anyone else present USER.md and the briefing are left out"
        )
    for kind in HERMES_CONTEXT_FILES:
        name = FILE_NAMES[kind]
        if kind not in files.kinds:
            notes.append(f"{name}: off (hermes.voice_context_files)")
            continue
        if kind in OWNER_CONTEXT_FILES and owner is None:
            continue
        found = files.read(profile, kind)
        if found is None:
            notes.append(f"{name}: none" if kind != "briefing" else f"{name}: none written yet")
        elif found.origin == "original":
            notes.append(f"{name}: the file itself, no rewrite of it yet ({found.path})")
        else:
            edited = "Hermes's rewrite, edited by hand" if found.origin == "edited" else "Hermes's rewrite"
            notes.append(f"{name}: {edited} ({found.path})")
    notes.append("open tasks: none shown here; on a call, the person's queued and running tasks are listed")
    for note in notes:
        print(f"# {note}", file=sys.stderr)
    print(prompter.instructions(context))


async def refresh_voice_context(config: Config, profile: str | None, *, force: bool = False) -> int:
    """Have Hermes write the enabled context texts of ``profile`` (None: every profile) now, print one
    line per text, and return 0, or 1 when one could not be written or none are enabled."""
    from .dispatch.hermes_api import HermesApiClient
    from .hermes_context import FILE_NAMES
    from .service import build_context_files

    def key(name: str) -> str | None:
        env_name = config.hermes.profile_keys_env.get(name)
        return config.secrets.get(env_name) if env_name in config.secrets.names() else None

    async with HermesApiClient(config.hermes.api_base, key) as hermes:
        files = build_context_files(config, hermes)
        if not files.kinds:
            print(
                "hermes-duplex: hermes.voice_context_files lists nothing; nothing to write",
                file=sys.stderr,
            )
            return _EXIT_CHECK_FAILED
        failed = False
        for name in [profile] if profile is not None else list(config.profiles):
            for kind in sorted(files.kinds):
                outcome = await files.refresh(name, kind, force=force)
                failed = failed or outcome == "failed"
                cache = files.cache_path(name, kind)
                message = {
                    "rewritten": f"written for voice, cached at {cache}",
                    "current": f"the cached rewrite is current ({cache})",
                    "kept_edit": f"kept your edit of {cache}; --force replaces it",
                    "missing": "no file to rewrite",
                    "failed": "Hermes could not write it (see the log); the voice prompt keeps what it had",
                }[outcome]
                print(f"{name} {FILE_NAMES[kind]}: {message}")
    return _EXIT_CHECK_FAILED if failed else 0


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the ``hermes-duplex`` command; exits with ``run``'s status.

    Logs INFO and above to stderr; inside Hermes, Hermes configures logging instead.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    add_commands(parser)
    raise SystemExit(run(parser.parse_args(argv)))
