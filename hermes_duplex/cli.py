"""Command line: ``hermes-duplex <command>`` standalone, and ``hermes duplex <command>`` inside Hermes.

Both use the same parser (``add_commands``) and handler (``run``). Commands:

- ``serve [--config PATH]`` runs the service until SIGINT or SIGTERM, then stops it.
- ``eval-router PHRASES [--backend NAME] [--json] [--config PATH]`` scores the router against a
  file of labelled phrases and prints a table, or JSON.
- ``doctor [--config PATH]`` checks the config, its secrets, and that Hermes, the provider, and
  Discord accept them, and prints a pass/fail table.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .config import ROUTER_BACKENDS, Config, ConfigError, load_config

if TYPE_CHECKING:
    from .router.knn import Embedder
    from .service import DuplexService

COMMAND_DEST = "duplex_command"
"""The ``argparse.Namespace`` attribute that holds the chosen command."""

_EXIT_CONFIG_ERROR = 1
_EXIT_CHECK_FAILED = 1
_EXIT_USAGE = 2


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Add the commands to ``parser``; ``hermes duplex`` passes its own subparser here."""
    commands = parser.add_subparsers(dest=COMMAND_DEST, metavar="{serve,eval-router,doctor}")
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


def run(args: argparse.Namespace) -> int:
    """Run the command ``args`` names and return the process exit status.

    Prints errors to stderr: a config the service cannot run with or an unusable phrase file exits 1,
    a failed ``doctor`` check exits 1, and a missing command exits 2.
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
    print("hermes-duplex: choose a command: serve, eval-router, or doctor", file=sys.stderr)
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

    ``embed`` replaces the knn backend's embedding model (tests pass a deterministic one).
    """
    # Imported here, like the service: the router pulls in numpy, model2vec, and aiohttp.
    from .router.eval import evaluate_config, format_table, load_phrases, to_json

    try:
        phrase_file = load_phrases(args.phrases)
        config = load_config(args.config) if args.config is not None else None
        reports = asyncio.run(evaluate_config(phrase_file, config, only=args.backend, embed=embed))
    except (ConfigError, ValueError) as error:
        print(f"hermes-duplex: {error}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
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


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the ``hermes-duplex`` command; exits with ``run``'s status.

    Logs INFO and above to stderr; inside Hermes, Hermes configures logging instead.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="hermes-duplex")
    add_commands(parser)
    raise SystemExit(run(parser.parse_args(argv)))
