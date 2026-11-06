"""Fixtures for the end-to-end tests.

``real_hermes`` (one per module) is a running Hermes gateway with this plugin installed; see
``tests/e2e/real_hermes.py``. Tests that use it skip when the ``hermes`` command is not on PATH or
cannot import ``hermes_cli``.
"""

from collections.abc import Iterator

import pytest

from tests.e2e.real_hermes import RealHermes, hermes_command, run_real_hermes


@pytest.fixture(scope="module")
def real_hermes() -> Iterator[RealHermes]:
    with run_real_hermes(hermes_command()) as hermes:
        yield hermes
