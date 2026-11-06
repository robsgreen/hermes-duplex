"""Shared pytest setup."""

from pathlib import Path

import pytest


class _CollectRepoRootAsDirectory:
    """Collect the repo root as a plain directory, not as a test package.

    The repo root is also the Hermes plugin directory, so it holds an ``__init__.py`` whose relative
    import only works when Hermes loads it as a package. pytest would otherwise import that file to
    set up the root "package", and fail. A conftest hook cannot do this itself: pytest asks the
    parent directory's conftests how to collect a directory, so the hook is registered as a plugin.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path: Path, parent: pytest.Collector) -> pytest.Collector | None:
        if path == self._repo_root:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_CollectRepoRootAsDirectory(Path(__file__).parent.parent))
