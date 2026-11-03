# Test Spec: voice-router-discord

> **Source:** `test-plan.md` (acceptance items A–J), `master-plan.md` (Requirements, Error Handling, Interface amendments), `task-spec.md`
> **Created:** 2026-11-03
> **Last Updated:** 2026-11-03

## Approach

The coder's 633 unit and contract tests already pin each module on its own (config, router policy with stub backends, adapters against the fake WS server, the Hermes client against the fake API, the session with a stub router). The gaps are the seams: what happens when the real modules are wired together the way `build_service` wires them, and what the real Hermes gateway does with the requests the plugin sends.

- **Component tests** build the whole service from a `duplex.yaml` with `build_service` (web transport, fake provider, real knn router with the deterministic embedder, real `HermesApiClient`, real `TaskTracker`) and drive it over a real WebSocket against `tests/fakes/fake_hermes_api.py`. Audio is sent in bursts, one request at a time, so the tests are fast and the order of requests is fixed. A Discord test joins a `DiscordConversation` (fake voice client, receiver stand-in) to a `VoiceSession` the way the service does, to check the mixer, attribution, and permissions together. A router test builds the backends from config (`router/factory.py`) and runs the real Decisions backend over HTTP next to knn.
- **Integration tests** run against a real Hermes gateway (hermes-agent @6112c2a) with the plugin installed, via `tests/e2e/real_hermes.py`'s `run_real_hermes(configure=...)`, with their own provider script: a topic switch to `general`, a status request that never reaches Hermes, and `doctor` against the live API server.
- Where the test plan predates the interface amendments, the code wins: the `delegate` receipt is `{"status": "started"}` and the task number arrives in the spoken start notice.

## Component Tests

- [x] **CT-1** — Topic switch and model table: rob's portfolio question goes to `/p/stock-picker` with the table's model and provider (a missing effort cell falls back to `default_model`) and `reasoning.effort`; "what's the weather tomorrow" then goes to `/p/general` on `duplex-rob-general` with no model; every decision logs speaker, profile, effort, model, confidence, top-3, source, latency_ms, sticky (C3, C7, C8).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: config, build_service, WebTransport, VoiceSession, FakeProvider, CompositeRouter + KnnBackend, HermesApiClient, TaskTracker
- [x] **CT-2** — Queue, status, and cancel by voice: a second stock-picker task is announced "queued behind task 1"; "what are you working on" is answered from the tracker with nothing sent to Hermes; "cancel that" stops the latest running run via `/stop` and is confirmed; `tasks.jsonl` holds every transition and the web page gets the task list as status frames (D6, D9, D10, D11, J3).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: as CT-1, plus the `_control` choice, TaskTracker.cancel, WebConversation.notify
- [x] **CT-3** — Hermes `session_busy` then accepted: the start is resent with the same `session_id` and `Idempotency-Key`, and the result is announced (D1).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: as CT-1
- [x] **CT-4** — Hermes refuses or stays unavailable (`permission_denied`, `idempotency_key_conflict`, `runtime_draining` past 3 retries): the task fails, the voice gets a short reason naming the code, the history ends `failed`, and no run is followed (D3, D4).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: as CT-1
- [x] **CT-5** — Hermes down, then back: the first task fails with "Hermes is unreachable"; the next task, once Hermes listens again, runs and is announced (D5).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: as CT-1 (fake Hermes API started on a fixed port after the first request)
- [x] **CT-6** — A very long Hermes result is framed as the stock-picker agent's report and cut to the cap with "…(more in Hermes)" (F1, F2).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: as CT-1, prompts.frame_result
- [x] **CT-7** — A request when nobody on the call was loud enough to be voiced: answered "not_started" with "who's asking?", no task and no run (E4).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: WebConversation voiced flag, AttributionRing, VoiceSession, FakeProvider
- [x] **CT-8** — Stopping the service while a task runs and a browser is connected: `stop` returns within 5 s, the browser's socket is closed by the server, the provider session is closed, and the web port stops accepting connections; an unknown token is refused with HTTP 401 before the upgrade (A6, E3).
  - File: `tests/e2e/test_web_service_flows.py`
  - Modules exercised: DuplexService.stop, WebTransport, VoiceSession, FakeProvider, HermesApiClient
- [x] **CT-9** — Discord channel with two allowlisted people and a stranger: the request is credited to the person voiced most in the window (alex), alex's permissions decide (general allowed, stock-picker refused), and the stranger's audio never reaches the provider (E2, E5).
  - File: `tests/e2e/test_discord_session_flows.py`
  - Modules exercised: DiscordConversation (mixer, resampler, pacer), VoiceSession, AttributionRing, CompositeRouter + KnnBackend, PermissionChecker, HermesApiClient, TaskTracker
- [x] **CT-10** — Decisions main path built from config, slower than the deadline: knn decides within 350 ms with `source=knn`; after 3 misses the main path is not called for 30 s, then is called again (C4, C5).
  - File: `tests/e2e/test_router_backends_flow.py`
  - Modules exercised: config, router.factory.build_backends, CompositeRouter, DecisionsBackend (HTTP), KnnBackend
- [x] **CT-11** — Every backend fails (Decisions HTTP 500, knn unsure): the hint profile when one is given (`source=hint`), else the active profile, else `general`, at effort `medium` with `source=fallback` (C6, C9).
  - File: `tests/e2e/test_router_backends_flow.py`
  - Modules exercised: as CT-10

## Integration Tests

- [x] **IT-1** — Topic switch on real Hermes: rob's portfolio question runs on `/p/stock-picker`, "what's the weather tomorrow" runs on `/p/general` with session `duplex-rob-general` and the general profile's default model, both completed with the fake LLM's answer and announced as reports; "what are you working on" is answered locally, with no task, no run, and no model call (C1, C3, D9, F1).
  - File: `tests/e2e/test_real_hermes_flows.py`
  - Infrastructure: real `hermes gateway run` (hermes-agent @6112c2a) with the plugin installed from the working tree, fake LLM, fake provider script, real model2vec knn
- [x] **IT-2** — `hermes-duplex doctor` against the running gateway: exit 0 with every row passing; with a wrong stock-picker key, exit 1 with that row failing on HTTP 401 and the key value never printed; with the API server unreachable, exit 1 naming the URL (B4).
  - File: `tests/e2e/test_real_hermes_flows.py`
  - Infrastructure: as IT-1
- [x] **IT-3** — Teardown: once the gateway fixture ends, its temporary `HERMES_HOME` is gone and its API server no longer answers.
  - File: `tests/e2e/test_real_hermes_flows.py` (fixture teardown)
  - Infrastructure: as IT-1

Not runnable here:

- Live tier (`-m live`, test-plan "Live e2e" items, C10 with decisions/jev/composite): not runnable here: needs `OPENAI_API_KEY` (and Decisions/Jev keys). The tier skips with a reason.
- Manual Discord checklist (`docs/live-checklist.md`: I3, H5, G4, async delegation, E5, E2/I2, I6/I7, H6/F5, 60-minute F3, J1–J3 web parity): not runnable here: needs a Discord bot token, a Discord server, and provider keys.

## How to Run

```bash
# Component tests (default tier)
uv run pytest tests/e2e/test_web_service_flows.py tests/e2e/test_discord_session_flows.py tests/e2e/test_router_backends_flow.py

# Integration tests (real Hermes; skips without `hermes` on PATH)
PATH=<hermes-agent venv>/bin:$PATH uv run pytest -m e2e -rs   # 4 passed (2 existing + 2 new), ~85 s

# All tests
uv run pytest -m "not live and not e2e" && PATH=<hermes-agent venv>/bin:$PATH uv run pytest -m e2e -rs
```

## Validation

- [x] All component tests pass
- [x] All integration tests pass (or N/A if not supported)
- [x] All existing unit tests still pass (no regressions)
- [x] Build passes

Results (2026-11-03): default tier `uv run pytest -m "not live and not e2e"` 646 passed (633 before + 13 new); real-Hermes tier `-m e2e` 4 passed (fixture teardown checks included); `-m live` 1 skipped (no `OPENAI_API_KEY`); `uv sync --frozen`, `ruff check .`, `ruff format --check .`, `pyright` clean. The new component tests passed 3 runs in a row.
