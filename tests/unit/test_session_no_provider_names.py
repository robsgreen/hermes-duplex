"""The session adapts to providers through their capabilities only, never by comparing provider names."""

import ast
from pathlib import Path

SESSION_PY = Path(__file__).resolve().parents[2] / "hermes_duplex" / "session.py"

PROVIDER_NAMES = frozenset({"gpt-live", "openai-realtime", "xai-realtime", "fake", "openai", "xai"})


def find_provider_name_comparisons(source: str) -> list[str]:
    """Return ``line: name`` for every comparison, ``match`` case, or membership test in ``source``
    whose operand is a provider-name string literal (alone or inside a tuple, list, or set)."""
    tree = ast.parse(source)
    hits: list[str] = []

    def check(node: ast.AST, line: int) -> None:
        literals = node.elts if isinstance(node, ast.Tuple | ast.List | ast.Set) else [node]
        for literal in literals:
            if isinstance(literal, ast.Constant) and literal.value in PROVIDER_NAMES:
                hits.append(f"{line}: {literal.value}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                check(operand, node.lineno)
        elif isinstance(node, ast.MatchValue):
            check(node.value, node.value.lineno)
    return hits


def test_session_never_compares_provider_names() -> None:
    assert find_provider_name_comparisons(SESSION_PY.read_text(encoding="utf-8")) == []


def test_detects_provider_name_comparisons() -> None:
    source = (
        "if provider.name == 'gpt-live':\n"
        "    pass\n"
        "if 'xai' != name:\n"
        "    pass\n"
        "if name in ('openai-realtime', 'other'):\n"
        "    pass\n"
        "match name:\n"
        "    case 'fake':\n"
        "        pass\n"
    )

    assert find_provider_name_comparisons(source) == [
        "1: gpt-live",
        "3: xai",
        "5: openai-realtime",
        "8: fake",
    ]


def test_ignores_capability_checks_and_other_strings() -> None:
    source = (
        "if capabilities['delegation_style'] == 'live':\n"
        "    pass\n"
        "label = 'openai'\n"
        "if kind in ('tool', 'live'):\n"
        "    pass\n"
    )

    assert find_provider_name_comparisons(source) == []
