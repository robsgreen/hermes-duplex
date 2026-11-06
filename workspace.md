# Workspace: hermes-duplex

Default branch: main   Remote: robsgreen/hermes-duplex   GitHub: yes

Notes: `origin/HEAD` is not set locally; `main` comes from the GitHub REST API (`default_branch`) and matches `origin/main`. `main` (0fd20c6) still holds only `README.md`; the toolchain, code, and docs below were established by spec voice-router-discord on branch `sdlc/voice-router-discord` (worktree `/home/user/hermes-duplex-worktrees/voice-router-discord`) and apply once it merges. Python 3.14 Hermes Agent plugin; uv + hatchling, ruff, pyright, pytest + pytest-asyncio. The repo root is the plugin dir with an `__init__.py`; `tests/conftest.py` makes pytest collect it as a plain directory. Keep it.

## Capabilities

| Capability | Status |
|------------|--------|
| Install | ✅ `uv sync --frozen` (Python 3.14 via `uv python install 3.14`; `uv.lock` committed). Runtime needs libopus (`apt-get install libopus0`). |
| Build | ✅ hatchling (`uv build`); no separate build step needed for tests or `serve` |
| Type-check | ✅ `uv run pyright` |
| Lint/Format | ✅ `uv run ruff check .` ; `uv run ruff format --check .` (format only changed files: `uv run ruff format <files>`) |
| Unit Tests | ✅ `uv run pytest -m "not live and not e2e"` — 1285 tests: unit, contract, in-process end-to-end; no network or keys |
| Integration Tests | ✅ Real Hermes e2e: `PATH=<hermes venv>/bin:$PATH uv run pytest -m e2e -rs` — 6 tests; needs hermes-agent @ `6112c2adb9e11f69734a29be9d2696f345ffa06a` installed in its own venv (recipe in README "Test"); skips without `hermes` on PATH. CI job `e2e` (not yet run on GitHub Actions). |
| Live (opt-in) | ✅ `PATH=<hermes venv>/bin:$PATH HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs` — 1 test; needs `OPENAI_API_KEY`, hermes-agent, and the spoken WAV fixtures (`OPENAI_API_KEY=... uv run python -m tests.live.make_audio_fixtures`; not committed yet). Optional `HERMES_DUPLEX_LIVE_MODEL`, `HERMES_DUPLEX_LIVE_BASE_URL`, `HERMES_DUPLEX_LIVE_KEY_ENV`. Spends money. |
| Local Dev | ✅ `uv run hermes-duplex serve --config duplex.yaml` ; `uv run hermes-duplex doctor --config duplex.yaml` ; `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` |
| Logging | ✅ stdlib `logging` per module (`logging.getLogger(__name__)`); router decisions log one `route ...` line with a `route_decision` record attribute |
| Metrics | ❌ Not established — no metrics library or exporter |

## Modules
- `.` — hermes-duplex: Hermes Agent plugin, full-duplex voice front end (`hermes_duplex/`)  (build: `uv sync --frozen`, test: `uv run pytest -m "not live and not e2e"`)

## Module Dependencies

No intra-repo dependencies — single module.

## Docs Layout
`AGENTS.md` (entry point), `CLAUDE.md` (`@AGENTS.md`), `docs/system/architecture.md`, `docs/live-checklist.md` (manual checks), `README.md` (user guide), `duplex.example.yaml` (documented config). No `docs/system/modules/` yet. Retros go to `docs/specs/YYYYMM/` per `doc-standards`.

## Integration Tests
Real-Hermes e2e tier (marker `e2e`): `tests/e2e/test_real_hermes.py`, `tests/e2e/test_real_hermes_flows.py`, and `tests/e2e/test_real_hermes_context.py`, with the gateway harness in `tests/e2e/real_hermes.py`. They start a real Hermes gateway in a temporary `HERMES_HOME` with profiles backed by `tests/fakes/fake_llm.py`, install the plugin from the working tree, and drive the web transport with the fake provider. The other `tests/e2e/` files are default tier (in-process).

## Local Development
Copy `duplex.example.yaml` to `duplex.yaml`, export the env vars it names, run `doctor`, then `serve`. Hermes API server setup is in README "Set up the Hermes API server".

## Observability
### Logging
stdlib `logging`; the service logs lifecycle, routing, barge-in, reconnect, and Discord receive counters.

### Metrics
NOT ESTABLISHED.

## Commands

- Install: `uv sync --frozen`
- Lint/format: `uv run ruff check .` ; `uv run ruff format --check .`
- Type-check: `uv run pyright`
- Default tests: `uv run pytest -m "not live and not e2e"`
- Real-Hermes e2e: `PATH=<hermes venv>/bin:$PATH uv run pytest -m e2e -rs`
- Live (opt-in): `PATH=<hermes venv>/bin:$PATH HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs`
- CI (`.github/workflows/ci.yml`): `check` (lint, format, types, default tests), `deps-compat` (resolves against Hermes's pins; no `openai` SDK), `e2e`
