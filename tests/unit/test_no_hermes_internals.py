"""The package must reach Hermes only through the plugin ``ctx`` API, never by importing its internals,
and must import its own modules relatively, since Hermes loads it under a package name of its own."""

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "hermes_duplex"

# Top-level modules of hermes-agent; none of them is a public API for plugins.
HERMES_INTERNAL_MODULES = frozenset(
    {"gateway", "hermes_cli", "hermes_constants", "agent", "tools", "plugins", "run_agent"}
)


def find_internal_imports(
    package_dir: Path, top_level: frozenset[str] = HERMES_INTERNAL_MODULES
) -> list[str]:
    """Return ``path:line: module`` for every absolute import of a module under one of the
    ``top_level`` packages (default: Hermes internals) in a ``.py`` file under ``package_dir``.

    The scan is static (source is parsed, never imported). Relative imports such as
    ``from .tools import x`` name our own subpackages and are ignored.
    """
    hits: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = (node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom))
        for node in sorted(imports, key=lambda node: node.lineno):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".")[0] in top_level:
                    hits.append(f"{path}:{node.lineno}: {module}")
    return hits


def test_package_does_not_import_hermes_internals() -> None:
    assert find_internal_imports(PACKAGE_DIR) == []


def test_package_imports_its_own_modules_relatively() -> None:
    assert find_internal_imports(PACKAGE_DIR, frozenset({"hermes_duplex"})) == []


def test_detects_absolute_self_imports(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from hermes_duplex.providers.base import ModelChoice\nfrom ..router.base import Effort\n",
        encoding="utf-8",
    )

    hits = find_internal_imports(tmp_path, frozenset({"hermes_duplex"}))

    assert [hit.split(": ", 1)[1] for hit in hits] == ["hermes_duplex.providers.base"]


def test_detects_internal_imports(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import gateway\n"
        "from hermes_cli.plugins import PluginContext\n"
        "def f():\n"
        "    import tools.voice_live as voice\n"
        "import os, agent\n",
        encoding="utf-8",
    )

    hits = find_internal_imports(tmp_path)

    assert [hit.split(": ", 1)[1] for hit in hits] == [
        "gateway",
        "hermes_cli.plugins",
        "tools.voice_live",
        "agent",
    ]


def test_ignores_relative_and_similarly_named_imports(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "from .tools import helper\n"
        "from . import plugins\n"
        "import gateways\n"
        "from agentic import x\n"
        "text = 'import gateway'\n",
        encoding="utf-8",
    )

    assert find_internal_imports(tmp_path) == []
