"""A real Hermes gateway with this plugin installed, backed by a fake LLM, for end-to-end tests.

``run_real_hermes`` builds a temporary ``HERMES_HOME`` from ``tests/fixtures/hermes_home/``, starts
``tests/fakes/fake_llm.py``, validates and installs the plugin with the ``hermes`` command, and runs
``hermes gateway run`` until the block ends. Starting takes up to a minute, mostly for the install,
which builds an isolated Hermes runtime for the plugin.
"""

import json
import os
import re
import shutil
import signal
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
TEMPLATES = FIXTURES / "hermes_home"

KEYS = {
    "default": "default-key-0123456789",
    "general": "general-key-0123456789",
    "stock-picker": "stocks-key-0123456789",
}
"""Each profile's ``API_SERVER_KEY`` (Hermes wants at least 16 characters)."""

TOKENS = {"rob": "rob-e2e-token", "alex": "alex-e2e-token"}
"""Each person's web transport token."""

LLM_REPLIES = {
    "stock-picker-medium": "stock-picker says: portfolio up 2%",
    "stock-picker-high": "stock-picker says: portfolio up 2%, after a closer look",
}
"""What the fake LLM answers each model in the stock-picker model table with."""

STARTUP_TIMEOUT_S = 180.0
"""How long the gateway may take to serve the API and the web transport."""

STOP_TIMEOUT_S = 10.0
"""How long the gateway gets to exit after SIGTERM before it is killed."""

ROUTE_LINE = re.compile(
    r" route speaker=(?P<speaker>\S+) profile=(?P<profile>\S+) .* sticky=(?P<sticky>True|False)$"
)


class RealHermes:
    """A running Hermes gateway with hermes-duplex in it, and what it and its fakes recorded."""

    def __init__(self, home: Path, api_base: str, web_port: int, llm_record: Path) -> None:
        self.home = home
        """The temporary ``HERMES_HOME``."""
        self.api_base = api_base
        """The default profile's API server, e.g. ``http://127.0.0.1:8642``."""
        self.web_url = f"http://127.0.0.1:{web_port}"
        """The in-gateway web transport."""
        self.llm_record = llm_record
        """The fake LLM's JSONL record of request bodies."""
        self.model_table: dict[str, dict[str, str]] = yaml.safe_load(
            (home / "duplex.yaml").read_text(encoding="utf-8")
        )["profiles"]["stock-picker"]["models"]
        """The stock-picker profile's model table: effort to ``{provider, model}``."""

    def provider_calls(self, method: str) -> list[dict[str, Any]]:
        """The arguments of each fake provider call to ``method`` so far, across conversations."""
        calls = _read_jsonl(self.home / "provider-calls.jsonl")
        return [call["args"] for call in calls if call["method"] == method]

    def tasks(self) -> dict[str, dict[str, Any]]:
        """Every task in ``$HERMES_HOME/duplex/tasks.jsonl`` by uuid, as its latest record."""
        return {record["uuid"]: record for record in _read_jsonl(self.home / "duplex" / "tasks.jsonl")}

    def route_decisions(self) -> list[tuple[str, str, bool]]:
        """``(speaker, profile, sticky)`` for every route decision in the gateway log, oldest first."""
        log = self.home / "logs" / "gateway.log"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        matches = (ROUTE_LINE.search(line) for line in lines)
        return [(m["speaker"], m["profile"], m["sticky"] == "True") for m in matches if m]

    def llm_requests(self) -> list[dict[str, Any]]:
        """Every chat completion request body the fake LLM received, oldest first."""
        return _read_jsonl(self.llm_record)

    def run_status(self, profile: str, run_id: str) -> dict[str, Any]:
        """``GET /p/<profile>/v1/runs/<run_id>`` with the profile's key."""
        request = urllib.request.Request(
            f"{self.api_base}/p/{profile}/v1/runs/{run_id}", headers=_bearer(KEYS[profile])
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)


@contextmanager
def run_real_hermes(hermes: str, configure: Callable[[Path], None] | None = None) -> Iterator[RealHermes]:
    """Run a Hermes gateway (``hermes`` is the executable) that serves profiles ``general`` and
    ``stock-picker`` and has hermes-duplex installed.

    ``configure``, when given, is called with the temporary ``HERMES_HOME`` after the templates are
    rendered and before the plugin is installed, to change what the gateway and the plugin run with
    (for example ``duplex.yaml`` or a profile's ``config.yaml``). Without it the plugin uses the fake
    provider and the knn router, and every profile answers from the fake LLM.

    Asserts that ``hermes plugins validate`` accepts the repo. Installs a snapshot of the working
    tree, so uncommitted changes are what runs. On teardown stops the gateway (SIGTERM, then SIGKILL
    after ``STOP_TIMEOUT_S``) and the fake LLM, and removes the temporary home.
    """
    home = Path(tempfile.mkdtemp(prefix="duplex-e2e-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "API_SERVER_"))}
    env |= {
        "HERMES_HOME": str(home),
        "DUPLEX_E2E_KEY_GENERAL": KEYS["general"],
        "DUPLEX_E2E_KEY_STOCKS": KEYS["stock-picker"],
        "DUPLEX_E2E_TOKEN_ROB": TOKENS["rob"],
        "DUPLEX_E2E_TOKEN_ALEX": TOKENS["alex"],
    }
    processes: list[subprocess.Popen[str]] = []
    try:
        llm_record = home / "llm-requests.jsonl"
        llm, llm_url = _start_fake_llm(llm_record, home / "fake-llm.log")
        processes.append(llm)
        api_port, web_port = _free_port(), _free_port()
        api_base = f"http://127.0.0.1:{api_port}"
        _render_home(
            home,
            default_key=KEYS["default"],
            general_key=KEYS["general"],
            stock_picker_key=KEYS["stock-picker"],
            api_port=str(api_port),
            api_base=api_base,
            llm_url=llm_url,
            web_port=str(web_port),
            provider_script=str(FIXTURES / "provider_scripts" / "delegate_portfolio.jsonl"),
        )
        if configure is not None:
            configure(home)

        validated = _run([hermes, "plugins", "validate", str(REPO)], env)
        assert validated.returncode == 0, f"hermes plugins validate failed:\n{validated.stdout}"
        snapshot = _snapshot_repo(home / "plugin-source")
        installed = _run([hermes, "plugins", "install", f"file://{snapshot}", "--enable", "--yes-deps"], env)
        # The command exits 0 even when Hermes refuses to admit the plugin, so check that it enabled it.
        assert installed.returncode == 0 and "Plugin hermes-duplex enabled" in installed.stdout, (
            f"hermes plugins install failed:\n{installed.stdout}"
        )

        with (home / "gateway-output.log").open("w", encoding="utf-8") as output:
            gateway = subprocess.Popen(
                [hermes, "gateway", "run"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        processes.append(gateway)
        _wait_until_serving(gateway, home, api_base, web_port)
        yield RealHermes(home, api_base, web_port, llm_record)
    finally:
        for process in reversed(processes):
            _stop(process)
        shutil.rmtree(home, ignore_errors=True)


def hermes_command() -> str:
    """The ``hermes`` executable, or skip the test when it is missing or cannot import ``hermes_cli``."""
    hermes = shutil.which("hermes")
    if hermes is None:
        pytest.skip("needs hermes-agent: the `hermes` command is not on PATH")
    try:
        version = subprocess.run(
            [hermes, "--version"], check=False, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"needs hermes-agent: `hermes --version` did not run ({error})")
    if version.returncode != 0:
        pytest.skip(f"needs hermes-agent: `hermes` cannot import hermes_cli:\n{version.stderr[-2000:]}")
    return hermes


def _start_fake_llm(record: Path, log: Path) -> tuple[subprocess.Popen[str], str]:
    """Start ``tests.fakes.fake_llm`` on a free port; return it and its ``/v1`` base URL."""
    replies = [f"--reply={model}={text}" for model, text in LLM_REPLIES.items()]
    with log.open("w", encoding="utf-8") as errors:
        llm = subprocess.Popen(
            [sys.executable, "-m", "tests.fakes.fake_llm", "--port", "0", "--record", str(record), *replies],
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            start_new_session=True,
        )
    assert llm.stdout is not None
    line = llm.stdout.readline()
    if not line.startswith("listening on "):
        _stop(llm)
        pytest.fail(f"the fake LLM did not start:\n{log.read_text(encoding='utf-8')}")
    return llm, line.removeprefix("listening on ").strip()


def _render_home(home: Path, **values: str) -> None:
    """Write every template under ``TEMPLATES`` to ``home`` with ``values`` substituted, and give
    ``duplex.yaml`` the router phrase fixture's profile descriptions and examples."""
    for template in TEMPLATES.rglob("*"):
        if template.is_file():
            target = home / template.relative_to(TEMPLATES)
            target.parent.mkdir(parents=True, exist_ok=True)
            text = string.Template(template.read_text(encoding="utf-8")).substitute(values)
            target.write_text(text, encoding="utf-8")

    duplex = home / "duplex.yaml"
    config = yaml.safe_load(duplex.read_text(encoding="utf-8"))
    phrases = yaml.safe_load((FIXTURES / "router_eval" / "phrases.yaml").read_text(encoding="utf-8"))
    for name, profile in phrases["profiles"].items():
        config["profiles"][name] |= profile
    duplex.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _snapshot_repo(target: Path) -> Path:
    """Copy the working tree (tracked and untracked files, minus ignored ones) into a new one-commit
    git repo at ``target``, so ``hermes plugins install`` clones exactly what is checked out."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
        check=True,
    )
    for name in filter(None, listed.stdout.decode().split("\0")):
        source = REPO / name
        if source.is_file():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
    identity = ["-c", "user.name=e2e", "-c", "user.email=e2e@localhost"]
    for command in (["init", "-q"], ["add", "-A"], [*identity, "commit", "-q", "-m", "snapshot"]):
        subprocess.run(["git", *command], cwd=target, check=True, capture_output=True)
    return target


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a ``hermes`` CLI command without a terminal; stdout and stderr together in ``stdout``."""
    return subprocess.run(
        command,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )


def _wait_until_serving(gateway: subprocess.Popen[str], home: Path, api_base: str, web_port: int) -> None:
    """Wait until ``/p/general/v1/models`` answers 200 and the web transport serves its page."""
    checks = [
        urllib.request.Request(f"{api_base}/p/general/v1/models", headers=_bearer(KEYS["general"])),
        urllib.request.Request(f"http://127.0.0.1:{web_port}/"),
    ]
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while checks:
        if gateway.poll() is not None or time.monotonic() > deadline:
            output = (home / "gateway-output.log").read_text(encoding="utf-8")
            pytest.fail(
                f"the Hermes gateway is not serving (exit code {gateway.returncode}):\n{output[-4000:]}"
            )
        try:
            with urllib.request.urlopen(checks[0], timeout=5) as response:
                if response.status == 200:
                    checks.pop(0)
                    continue
        except URLError, OSError:
            pass
        time.sleep(0.5)


def _stop(process: subprocess.Popen[str]) -> None:
    """SIGTERM the process's group, then SIGKILL whatever is left of it after ``STOP_TIMEOUT_S``."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=STOP_TIMEOUT_S)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
