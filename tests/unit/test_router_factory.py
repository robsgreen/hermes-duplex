"""build_backends: one backend per configured path, named as config names it, built without I/O."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_duplex.config import Config, ConfigError, load_config
from hermes_duplex.router.base import RouteRequest, choice_specs
from hermes_duplex.router.composite import CompositeRouter
from hermes_duplex.router.decisions import DecisionsBackend
from hermes_duplex.router.factory import build_backends
from hermes_duplex.router.jev import JevBackend
from hermes_duplex.router.knn import KnnBackend
from tests.fakes.fake_embedder import fake_embed

ENV = {"OPENAI_KEY": "openai-key-0123456789", "JEV_KEY": "jev-key-0123456789"}


def _config(tmp_path: Path, router: dict[str, Any]) -> Config:
    raw = {
        "hermes": {"api_base": "http://127.0.0.1:9"},
        "provider": {"name": "fake"},
        "router": router,
        "profiles": {
            "general": {"description": "Anything else", "examples": ["what's the weather"]},
            "stock-picker": {"description": "Portfolio and trades", "examples": ["how's my portfolio"]},
        },
    }
    path = tmp_path / "duplex.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path, env={**ENV, "HERMES_HOME": str(tmp_path)})


@pytest.mark.parametrize(
    ("router", "expected"),
    [
        ({"backend": "knn"}, [(KnnBackend, "knn")]),
        ({"backend": "knn", "fast_path": "knn"}, [(KnnBackend, "knn")]),
        (
            {"backend": "decisions", "fast_path": "knn", "decisions": {"api_key_env": "OPENAI_KEY"}},
            [(DecisionsBackend, "decisions"), (KnnBackend, "knn")],
        ),
        (
            {"backend": "jev", "jev": {"api_key_env": "JEV_KEY", "base_url": "http://127.0.0.1:9"}},
            [(JevBackend, "jev")],
        ),
    ],
    ids=["knn", "knn-fast-path-is-main", "decisions-with-knn-fast-path", "jev"],
)
async def test_builds_the_main_then_the_fast_path_named_as_configured(
    tmp_path: Path, router: dict[str, Any], expected: list[tuple[type, str]]
) -> None:
    config = _config(tmp_path, router)

    backends = build_backends(config, embed=fake_embed)

    assert [(type(backend), backend.name) for backend in backends] == expected
    # CompositeRouter finds every backend the config names among them.
    CompositeRouter(config.router, config.profiles, backends)
    for backend in backends:
        await backend.close()


async def test_knn_uses_the_given_embedder(tmp_path: Path) -> None:
    config = _config(tmp_path, {"backend": "knn"})
    [knn] = build_backends(config, embed=fake_embed)

    await knn.configure(choice_specs(config.profiles, config.router.control_examples))
    result = await knn.classify(
        RouteRequest(text="how is my portfolio", speaker="rob", history=(), active_profile=None, hint=None),
        deadline_ms=300,
    )

    assert max(result.profile_probs, key=lambda name: result.profile_probs[name]) == "stock-picker"


@pytest.mark.parametrize("backend", ["decisions", "jev"])
def test_an_api_backend_without_its_settings_is_a_config_error(tmp_path: Path, backend: str) -> None:
    config = _config(tmp_path, {"backend": "knn"})
    asks_for_api = dataclasses.replace(config, router=dataclasses.replace(config.router, backend=backend))

    with pytest.raises(ConfigError, match=f"router.{backend}.api_key_env"):
        build_backends(asks_for_api)
