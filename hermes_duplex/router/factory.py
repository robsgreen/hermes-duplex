"""Builds the router backends a config names, for ``CompositeRouter`` and the router eval."""

from ..config import Config, ConfigError
from .base import RouterBackend
from .decisions import DecisionsBackend
from .jev import JevBackend
from .knn import Embedder, KnnBackend


def build_backends(config: Config, *, embed: Embedder | None = None) -> list[RouterBackend]:
    """The main-path backend ``config.router.backend`` names, then the fast-path backend
    ``config.router.fast_path`` names when it is set and different. Each backend's ``name`` is the
    config value that named it, which is how ``CompositeRouter`` finds it.

    ``embed`` replaces the ``knn`` backend's embedding model (tests pass a deterministic one). Nothing
    is loaded or connected here: ``knn`` loads its model on ``configure``, and the API backends open
    their HTTP session on first ``classify``; call each backend's ``close()`` when done.

    Raises ``ConfigError`` when ``decisions`` or ``jev`` is named but its settings are absent (a
    loaded config always has them for the main path), or the name is no backend.
    """
    router = config.router
    backends: list[RouterBackend] = []
    for name in dict.fromkeys((router.backend, router.fast_path)):
        match name:
            case None:
                pass
            case "knn":
                backends.append(KnnBackend(embed))
            case "decisions":
                if router.decisions is None:
                    raise ConfigError(
                        "router.decisions.api_key_env", "is required for router backend 'decisions'"
                    )
                backends.append(DecisionsBackend.from_config(router.decisions, config.secrets))
            case "jev":
                if router.jev is None:
                    raise ConfigError("router.jev.api_key_env", "is required for router backend 'jev'")
                backends.append(JevBackend.from_config(router.jev, config.secrets))
            case _:
                raise ConfigError("router.backend", f"'{name}' is not a router backend")
    return backends
