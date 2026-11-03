"""Packaging invariants: the Hermes manifest, the project metadata, and the lock agree with each other."""

import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex import cli

REPO_ROOT = Path(__file__).resolve().parents[2]

# PEP 508: a requirement string starts with the distribution name.
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _load_toml(name: str) -> dict[str, Any]:
    with (REPO_ROOT / name).open("rb") as file:
        return tomllib.load(file)


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement.strip())
    assert match, requirement
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return _load_toml("pyproject.toml")


def test_manifest_declares_expected_fields(manifest: dict[str, Any]) -> None:
    assert manifest == {
        "name": "hermes-duplex",
        "version": "0.1.0",
        "manifest_version": 2,
        "api_version": 1,
        "kind": "standalone",
        "description": manifest["description"],
        "author": manifest["author"],
        "license": "MIT",
        "provides_tools": [],
        "provides_hooks": [],
        "requires_env": [],
    }
    assert manifest["description"].strip()
    assert manifest["author"].strip()


def test_manifest_version_matches_project_version(
    manifest: dict[str, Any], pyproject: dict[str, Any]
) -> None:
    assert manifest["version"] == pyproject["project"]["version"]


def test_openai_is_not_a_dependency(pyproject: dict[str, Any]) -> None:
    project = pyproject["project"]
    requirements = [
        *project["dependencies"],
        *(req for reqs in project.get("optional-dependencies", {}).values() for req in reqs),
    ]

    assert "openai" not in {_requirement_name(req) for req in requirements}


def test_openai_is_not_in_lock() -> None:
    locked = {package["name"] for package in _load_toml("uv.lock")["package"]}

    assert "hermes-duplex" in locked
    assert "openai" not in locked


def test_register_passes_the_validation_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the repo root the way Hermes's validation probe does and call register() with a context
    like the probe's: it records tools and hooks, accepts the other registration methods, and raises
    AttributeError for anything else. The probe's HERMES_HOME is an empty scratch directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DUPLEX_CONFIG", raising=False)
    recorded: dict[str, list[str]] = {"tools": [], "hooks": []}

    class ProbeContext:
        def register_tool(self, name: str, *args: Any, **kwargs: Any) -> None:
            recorded["tools"].append(name)

        def register_hook(self, name: str, *args: Any, **kwargs: Any) -> None:
            recorded["hooks"].append(name)

        def register_command(self, *args: Any, **kwargs: Any) -> None:
            pass

        def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __getattr__(self, name: str) -> Any:
            raise AttributeError(name)

    module_name = "hermes_duplex_root_probe"
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        module.register(ProbeContext())
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == module_name]:
            del sys.modules[name]

    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert recorded == {"tools": manifest["provides_tools"], "hooks": manifest["provides_hooks"]}


def test_cli_without_a_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code == 2
    assert capsys.readouterr().err.startswith("hermes-duplex: choose a command")
