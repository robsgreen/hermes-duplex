# Task Spec: voice-router-discord (hermes-duplex)

> **Source:** `.sdlc/specs/voice-router-discord/master-plan.md`
> **Created:** 2026-11-03
> **Last Updated:** 2026-11-03
> **Progress:** 153 / 161 sub-tasks complete (4.3.6 MAY deferred)

**Read before every task:**

- The master plan's Below-the-Line section (Scope, Requirements, Interfaces, Error Handling, Test Strategy) is the contract. This file only orders the work.
- Phases follow the plan's six milestones. Milestone 1 is split into Phase 1 (foundations) and Phase 2 (end-to-end path), so each phase stays at six tasks or fewer.
- Layout rule (plan Scope): the repo root is the plugin directory. It holds `plugin.yaml` and a thin root `__init__.py`. All code lives in `hermes_duplex/` and uses relative imports. Tests live in `tests/` and run with `--import-mode=importlib`.
- Never import Hermes internals at runtime (`gateway`, `hermes_cli`, `agent`, `tools`, …). Never monkey-patch Hermes. Use only the documented plugin `ctx` methods.
- Reference sources are public. Fetch them when a task names one: hermes-agent `https://github.com/NousResearch/hermes-agent` at commit `6112c2adb9e11f69734a29be9d2696f345ffa06a` (short `6112c2a`); hermes-talk `https://github.com/TheSmokeDev/hermes-talk` at `e5ecfa9`; hermes-live-voice `https://github.com/bielcarpi/hermes-live-voice` at `f896178`; the `openai==3.42.1` wheel from PyPI (protocol types only, never a dependency).
- Standard validation commands (from plan Test Strategy; they exist after Task 1.1):
    - **Build gate:** `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pyright`
    - **Default tests:** `uv run pytest -m "not live and not e2e"`

---

## Phase 1: Foundations (Milestone 1, part A)

### Task 1.1: Toolchain, packaging, plugin manifest, and CI

Create the Python 3.14 project, the Hermes plugin entry points, and the CI workflow, so every later task has working build, lint, type-check, and test commands.

**Files:**

```
pyproject.toml                      (create)
uv.lock                             (create)
plugin.yaml                         (create)
__init__.py                         (create)
hermes_duplex/__init__.py           (create)
hermes_duplex/plugin.py             (create)
LICENSE                             (create)
.gitignore                          (create)
.github/workflows/ci.yml            (create)
tests/unit/test_packaging.py        (create)
tests/unit/test_no_hermes_internals.py (create)
```

**Sub-tasks:**

- [x] **1.1.1** — Confirm wheel availability before you pin anything. Run `uv pip compile` (or `uv add --dry-run`) on Python 3.14 for `soxr>=0.5,<1` and `model2vec>=0.6,<1`. Record the result in the Final Report: a `cp314` (or `abi3`) wheel exists for linux x86_64 and macOS arm64, and the transitive dependencies of `model2vec` do not conflict with the Hermes pins below. If `soxr` has no wheel, stop and report: the plan's fallback is `numpy`-based polyphase resampling, and Task 5.1 changes.
- [x] **1.1.2** — Write `pyproject.toml`: project `hermes-duplex`, version `0.1.0`, `requires-python = ">=3.14,<3.15"`, license MIT. Dependencies, each with an upper bound and each range containing the Hermes core pin from hermes-agent @6112c2a `pyproject.toml`:
    - [x] **1.1.2a** — `discord.py[voice]==2.7.1` (brings `davey`), `aiohttp>=3.14.5,<4`, `websockets>=15.0.1,<16`, `numpy>=2.5,<3`, `pyyaml>=6,<7`, `soxr>=0.5,<1`, `model2vec>=0.6,<1`. No `openai` dependency (Hermes pins `openai==2.54.0`; GPT-Live needs ≥3.42).
    - [x] **1.1.2b** — Copy Hermes's `[tool.uv] override-dependencies` entry `"pynacl>=1.6,<1.7"`: discord.py 2.7.1 pins `pynacl<1.6`, and Hermes overrides it. Without the override, the lock does not match the gateway's environment.
    - [x] **1.1.2c** — Dev dependency group: `pytest` (Hermes uses 9.1.1), `pytest-asyncio`, `ruff`, `pyright`. `[project.scripts] hermes-duplex = "hermes_duplex.cli:main"` (the module arrives in Task 2.4; add a stub `main()` that prints usage and exits 2 so the entry point resolves now).
    - [x] **1.1.2d** — Tool config: pytest `testpaths = ["tests"]`, `addopts = "--import-mode=importlib"`, `asyncio_mode = "auto"`, markers `e2e` and `live`; ruff (target py314, line length 110); pyright `include = ["hermes_duplex", "tests"]`, `pythonVersion = "3.14"`, standard mode.
- [x] **1.1.3** — Run `uv lock` and commit `uv.lock`.
- [x] **1.1.4** — Write `plugin.yaml` (Hermes manifest v2). Use only fields Hermes @6112c2a knows (`hermes_cli/plugins_manifest.py` `_KNOWN_MANIFEST_FIELDS`); unknown fields log a warning under v2. Content: `name: hermes-duplex`, `version: 0.1.0`, `manifest_version: 2`, `api_version: 1` (Hermes accepts any integer ≥ 1; `None` means current), `kind: standalone`, `description`, `author`, `license: MIT`, `provides_tools: []`, `provides_hooks: []`, `requires_env: []`. `hermes plugins validate` fails when `register()` registers a hook or tool that is not declared in `provides_hooks`/`provides_tools`, so keep the declared lists equal to what `register()` registers. Slash and CLI commands have no manifest field; do not add `provides_commands`.
- [x] **1.1.5** — Write root `__init__.py` with only `from .hermes_duplex.plugin import register`, and `hermes_duplex/plugin.py` with a `register(ctx)` that does nothing yet. `register()` must not raise under Hermes's validation probe, which passes a recording context whose unknown attributes raise `AttributeError`.
- [x] **1.1.6** — Write `tests/unit/test_no_hermes_internals.py`: a grep-based test that fails when any file under `hermes_duplex/` imports `gateway`, `hermes_cli`, `hermes_constants`, `agent`, `tools`, `plugins`, or `run_agent` (plan Requirement "register(ctx) only uses documented ctx methods"). Write `tests/unit/test_packaging.py`: assert `plugin.yaml` parses, holds the fields above, its `version` equals `pyproject.toml`'s version, and `openai` is absent from `pyproject.toml` dependencies and from `uv.lock` packages.
- [x] **1.1.7** — Write `.github/workflows/ci.yml` with these jobs on `ubuntu-latest`, using `astral-sh/setup-uv` and Python 3.14:
    - [x] **1.1.7a** — `check`: `uv sync --frozen`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run pyright`; `uv run pytest -m "not live and not e2e"`.
    - [x] **1.1.7b** — `deps-compat`: download hermes-agent `pyproject.toml` at `6112c2adb9e11f69734a29be9d2696f345ffa06a`, turn its exact `==` pins (base dependencies plus the `messaging` extra) into a constraints file, and run `uv pip compile pyproject.toml -c <constraints> --override <file with pynacl>=1.6,<1.7> --python-version 3.14`. The job fails on any conflict, and fails when `openai` appears in the resolved output.
    - [x] **1.1.7c** — Leave a commented placeholder for the `e2e` job; Task 2.5 fills it.
- [x] **1.1.8** — Write `LICENSE` (MIT, copyright holder from the repo owner `robsgreen`, 2026) and `.gitignore` (`.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `dist/`).

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest -m "not live and not e2e"` passes; new tests cover manifest fields, version match, `openai` absence, and the internal-import grep (prove the grep fails on a temporary file that contains `import gateway`, then delete it).
- [x] **Requirements:**
    - [x] "A CI job resolves the package's dependencies with hermes-agent @6112c2a's pins as constraints and fails on conflict; `openai` is absent from the dependency tree."
    - [x] "a test asserts no `import gateway`/`hermes_cli` internals in the package (grep-based)."
    - [x] "`uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest -m "not live"` all pass locally and in `.github/workflows/ci.yml`."
    - [x] Dependency ranges contain Hermes core's exact pins; `soxr`/`model2vec` cp314 wheel check recorded.
- [x] **Verify:** `grep -n openai uv.lock` prints nothing; run the `deps-compat` job's commands locally and confirm exit 0.

---

### Task 1.2: Config loading, people, and permissions

Load `duplex.yaml` into dataclasses with fail-closed validation, and map people to Discord ids, web tokens, and allowed profiles.

**Files:**

```
hermes_duplex/config.py               (create)
hermes_duplex/people.py               (create)
duplex.example.yaml                   (create)
tests/unit/test_config.py             (create)
tests/unit/test_people.py             (create)
tests/fixtures/config/*.yaml          (create)
```

**Sub-tasks:**

- [x] **1.2.1** — Define frozen dataclasses for every section in the plan's `duplex.example.yaml (shape)`: `hermes` (api_base, profile_keys_env), `provider` (name, model, voice, api_key_env, turn_detection, plus fake-only keys `script` and `record_path`), `router` (backend, fast_path, deadline_ms=300, default_profile="general", sticky_idle_s=600, control_examples), `profiles.<p>` (description, examples, models per effort as `ModelChoice(provider, model)`, default_model), `people.<name>` (discord_id, web_token_env, profiles list with `"*"` meaning all), `transports` (discord: enabled, bot_token_env; web: enabled, host=127.0.0.1, port=8765), and `audio` (pacer lead_ms=200, gate threshold, playback queue seconds=5). Define `ModelChoice` in `providers/base.py`'s interface shape; if Task 1.3 has not landed, put it in `config.py` and re-export later.
- [x] **1.2.2** — Resolve the config path in this order: explicit `--config` argument, env `HERMES_DUPLEX_CONFIG`, then `$HERMES_HOME/duplex.yaml` (HERMES_HOME defaults to `~/.hermes`).
- [x] **1.2.3** — Validate and fail closed with a `ConfigError` whose message names the offending key: unknown provider name (allowed: `gpt-live`, `openai-realtime`, `xai-realtime`, `fake`); unknown router backend (allowed: `decisions`, `jev`, `knn`); a person referencing an undefined profile; a missing env var named by any `*_env` key; xai provider with turn_detection other than `server_vad`; `ModelChoice` missing `provider` or `model`. Never infer a provider from which keys are present.
- [x] **1.2.4** — Secrets come only from env vars named in config. Reject a literal secret-looking value: any key named `api_key`, `token`, `bot_token`, `key`, `web_token`, or `secret` (without `_env`), and any `*_env` value that does not look like an env-var name (`^[A-Z][A-Z0-9_]*$`). Resolve secrets at load time into a separate `Secrets` object that never appears in `repr()` or logs.
- [x] **1.2.5** — In `people.py`, write `Person` and `PermissionChecker`: lookup by Discord id, lookup by web token (constant-time compare; one token maps to exactly one person; duplicate tokens are a `ConfigError`), `allowed(person, profile)`, and the set of allowlisted Discord ids.
- [x] **1.2.6** — Write `duplex.example.yaml` with every key documented in comments, the profile-by-effort table with `<fill>` placeholders and a comment that the user fills in their own models (plan Open Question), and `general` as the default profile.
- [x] **1.2.7** — Unit tests: one test per fail-closed case in 1.2.3 and 1.2.4 asserting the error names the key; a valid config round trip; path resolution order; `"*"` profile wildcard; token-to-person mapping; secrets absent from `repr`.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_config.py tests/unit/test_people.py` passes, covering every fail-closed case, wildcard permissions, token mapping, and path order.
- [x] **Requirements:**
    - [x] "Unknown provider, unknown router backend, profile referenced by a person but not defined, or missing secret env var → startup error naming the key; unit tests per case."
    - [x] "Secrets (bot token, provider keys, per-profile API_SERVER_KEY, web tokens) come only from env vars named in config, never literal values; a literal secret-looking value is rejected."
    - [x] "A `people` table maps each person to a Discord user id and a web token. Each profile lists the people allowed to use it." (ATL; implemented as the per-person `profiles` list in the plan's YAML shape)
- [x] **Verify:** `uv run python -c "from hermes_duplex.config import load_config; load_config('duplex.example.yaml')"` with the example's env vars unset prints a `ConfigError` naming the first missing env var.

---

### Task 1.3: Core contracts, audio frames, pacer, attribution buffer, and result framing

Write the provider and transport interfaces, the PCM frame helpers, the output pacer, the speaker attribution ring buffer, and the prompt templates that every later task builds on.

**Files:**

```
hermes_duplex/providers/__init__.py   (create)
hermes_duplex/providers/base.py       (create)
hermes_duplex/transports/__init__.py  (create)
hermes_duplex/transports/base.py      (create)
hermes_duplex/audio/__init__.py       (create)
hermes_duplex/audio/frames.py         (create)
hermes_duplex/audio/pacer.py          (create)
hermes_duplex/audio/mixer.py          (create)
hermes_duplex/prompts.py              (create)
tests/unit/test_frames.py             (create)
tests/unit/test_pacer.py              (create)
tests/unit/test_attribution.py        (create)
tests/unit/test_prompts.py            (create)
```

**Sub-tasks:**

- [x] **1.3.1** — `providers/base.py`: copy the plan's Interfaces exactly: `ProviderCapabilities` (TypedDict: input_rate, output_rate, supports_truncate, supports_out_of_band_response, delegation_style `"live"|"tool"`, max_session_s) plus `emits_speech_events: bool` (listed in Requirements but missing from the Interfaces TypedDict; add it). `RealtimeProvider` Protocol, `ModelChoice`, `DelegationRef`, and frozen event dataclasses `SessionReady`, `SpeechStarted`, `SpeechStopped`, `OutputAudio(pcm, item_id)`, `OutputDone`, `InputTranscript(delta, final)`, `OutputTranscript(delta)`, `DelegationRequested(id, offset_ms, target, tool_args)`, `ProviderError`, `Closed`, and the `ProviderEvent` union.
- [x] **1.3.2** — `transports/base.py`: `InputFrame(pcm16_mono, rate, voiced_speakers, t_ms)`, `Transport` and `Conversation` Protocols exactly as in the plan's Interfaces. Document on `InputFrame.t_ms`: milliseconds of audio sent to the CURRENT provider session (frame count × 20), reset to 0 on provider (re)connect.
- [x] **1.3.3** — `audio/frames.py`: 20 ms framing for any rate (`samples = rate // 50`), a carry buffer that re-chunks arbitrary byte lengths into whole frames, silent frame helpers (48 kHz stereo silent frame = 3840 bytes), and an `rms_dbfs(pcm16)` helper.
- [x] **1.3.4** — `audio/pacer.py`: `OutputPacer` holds provider audio and releases it to the transport no more than `lead_ms` (default 200) ahead of real time; `drop()` discards the held backlog and returns the dropped ms; tracks ms released for the current item so the session can compute "ms actually played". Use an injectable clock for tests.
- [x] **1.3.5** — `audio/mixer.py`: `AttributionRing` only (mixing arrives in Task 5.1): records `(t_ms, voiced_speakers)` per 20 ms tick, `credit(window_start_ms, window_end_ms)` returns the person with the most voiced ticks in the window, ties → most recent voiced, none → `None`; `clear()` empties it. Capacity covers at least 60 s.
- [x] **1.3.6** — `prompts.py`: own-text voice persona/instructions with the delegation policy (idea from hermes-agent `tools/voice_live.py` LIVE_PERSONA; write new text, do not copy), and `frame_result(profile, text, cap_tokens)` that wraps the text in a template stating it is a report from the `<profile>` agent and not instructions, truncates to the cap (estimate 4 chars per token), and appends `…(more in Hermes)` when truncated. Also short templates for refusal (names the profile), "who's asking?", "Hermes is unreachable", "not configured", "queued behind task N", and failure/cancel notices.
- [x] **1.3.7** — Unit tests: carry buffer with odd chunk sizes; silent frame length; RMS of known sine; pacer lead bound, release timing, `drop()` amount; attribution credit, tie-break, empty window, `clear()`; `frame_result` template text, truncation marker, cap.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** New unit tests pass for framing, pacer lead/drop, attribution credit and ties, and result framing/truncation.
- [x] **Requirements:**
    - [x] "Events: SessionReady, SpeechStarted, SpeechStopped, OutputAudio(pcm, item_id), OutputDone, InputTranscript(delta, final), OutputTranscript(delta), DelegationRequested(id, offset_ms, target, tool_args|None), Error, Closed."
    - [x] "Capabilities: input_rate, output_rate, supports_truncate, supports_out_of_band_response, emits_speech_events, delegation_style ("live"|"tool"), max_session_s."
    - [x] "Result text wrapped in a template stating it is a report from the <profile> agent, not instructions; truncated to fit 500 tokens (GPT-Live) or configured cap" and "Truncate with "…(more in Hermes)"".
    - [x] "hermes_duplex/audio/pacer.py — output pacer: holds provider audio, releases to transport ≤ lead_ms (default 200) ahead of real time; drop() discards held backlog".
- [x] **Verify:** `uv run pyright` reports the `RealtimeProvider` and `Conversation` Protocols with zero errors; read `prompts.py` and confirm the template says the text is a report and not instructions.

---

### Task 1.4: Scriptable fake provider and provider registry

Build the fake provider that tests and the CI end-to-end tiers use, and the registry that maps a provider name to a factory and fails closed.

**Files:**

```
hermes_duplex/providers/fake.py               (create)
hermes_duplex/providers/registry.py           (create)
tests/fixtures/provider_scripts/delegate_portfolio.jsonl  (create)
tests/unit/test_fake_provider.py              (create)
tests/unit/test_registry.py                   (create)
```

**Sub-tasks:**

- [x] **1.4.1** — Define the JSONL script format: one step per line, each with a trigger (`at_input_ms`: fire once the provider has received this many ms of input audio; or `after_ms`: wall-clock delay after the previous step; or `on_sent`: fire after the session sends a matching call such as `deliver_result`) and an `event` that maps to one normalized `ProviderEvent` (for example `{"type": "delegation", "id": "d1", "tool_args": {"task": "how is my portfolio doing"}}`, `{"type": "output_audio", "ms": 600}`, `{"type": "speech_started"}`, `{"type": "close"}`). Document the format in the module docstring.
- [x] **1.4.2** — `FakeProvider` implements `RealtimeProvider`. Capabilities come from config (default: tool-style, 24 kHz, emits_speech_events true, truncate true, out-of-band true) so tests can run the session as either family. It counts input ms from `send_audio`, fires script steps, and records every call the session makes (`connect` args, `truncate`, `report_progress`, `deliver_result`, `say`, `close`) in memory and, when `record_path` is set, appends them as JSONL to that file so an out-of-process test can read them.
- [x] **1.4.3** — `registry.py`: name → factory map (`gpt-live`, `openai-realtime`, `xai-realtime`, `fake`); unregistered real adapters raise `NotImplementedError` until their tasks land; an unknown name raises `ConfigError`. Never pick a provider from available keys.
- [x] **1.4.4** — Write `delegate_portfolio.jsonl` (used by Tasks 2.4 and 2.5): after ~1500 ms of input, delegate "how is my portfolio doing"; later, after more input, delegate "and what about Tesla".
- [x] **1.4.5** — Unit tests: script triggers fire in order and on the right input ms; recording to memory and to `record_path`; capability override; registry fail-closed on an unknown name.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** Fake provider and registry unit tests pass, covering every trigger kind, recording, and the unknown-name error.
- [x] **Requirements:**
    - [x] "hermes_duplex/providers/fake.py — scriptable fake provider (replays JSONL scripts; records what the session sent)".
    - [x] "hermes_duplex/providers/registry.py — name → factory; fail closed on unknown provider (never infer from available keys)".
- [x] **Verify:** Run a short script through the fake in a Python one-liner and read the `record_path` JSONL it writes.

---

### Task 1.5: Hermes API client (dispatcher)

Implement `HermesApiClient`, which creates runs on a chosen profile, streams their events, stops them, and maps every Hermes error code as the plan specifies.

**Files:**

```
hermes_duplex/dispatch/__init__.py            (create)
hermes_duplex/dispatch/hermes_api.py          (create)
tests/fakes/__init__.py                       (create)
tests/fakes/fake_hermes_api.py                (create)
tests/contract/test_hermes_api_client.py      (create)
```

**Protocol facts (hermes-agent @6112c2a, `gateway/platforms/api_server_runs.py`, `api_server_authority_runs.py`, `api_server.py`):**

- Start: `POST {api_base}/p/{profile}/v1/runs`, headers `Authorization: Bearer <that profile's API_SERVER_KEY>` and `Idempotency-Key: <task uuid>` (1–255 visible ASCII). Body `{"input": text, "session_id": "duplex-{person}-{profile}", "model"?: m, "provider"?: p, "model_options": {"reasoning": {"effort": "low|medium|high"}}}`. Effort goes in `model_options.reasoning.effort`, not `reasoning_effort`. Success: `202 {"run_id": "run_<hex>", "status": "started", "replayed": bool}`; a replay adds header `Idempotency-Replayed: true`.
- Errors: body `{"error": {"message", "type", "param", "code"}}`; branch on `error.code`. `session_busy` → 409. `idempotency_key_conflict` → 409. `runtime_draining` and `storage_unavailable` → 503 with `Retry-After: 1`. `admission_queued` → 503 with `x-should-retry: false` and no `Retry-After`; the input is committed and runs once when Hermes is back. At @6112c2a this 503 body carries no `run_id` (the run reservation is dropped, `api_server_runs.py:753-759`). Other refusals → 409 with their own code (`profile_mismatch`, `permission_denied`, …). Unknown profile prefix → 404.
- Status: `GET .../v1/runs/{run_id}` → `{"object": "hermes.run", "run_id", "status", "session_id", "model", "created_at", "updated_at", "last_event", ...}`. Statuses: `queued`, `running`, `waiting_for_approval`, `stopping`, `completed`, `failed`, `cancelled`, `interrupted` (terminal: completed, failed, cancelled, interrupted). On completion it also carries `output`, `usage`, and `runtime: {"provider", "model", "route_source", "requested": {"provider", "model"}}`; on failure `error`.
- Events: `GET .../v1/runs/{run_id}/events` (SSE). Each frame is `id: <seq>\ndata: <json>\n\n`; the JSON is `{"event": <name>, "run_id", "timestamp", "seq", ...fields}`. Resume with header `Last-Event-ID: <seq>` or `?last_seq=<seq>`. Names: `run.queued`, `run.started`, `message.started`, `message.delta` (`delta`), `message.interim` (`text`), `tool.started` (`tool`, `preview`), `tool.progress`, `tool.completed` (`tool`, `duration`, `error`), `tool.failed`, `approval.request`, `run.steered`, `run.stopping`, `run.completed` (`output`, `usage`, `runtime`), `run.failed` (`error`), `run.cancelled`, `run.interrupted`.
- Stop: `POST .../v1/runs/{run_id}/stop`.
- Profile keys: a named profile's `/p/<profile>/` requests need the `API_SERVER_KEY` from that profile's own `.env` (at least 16 characters); the default listener key is rejected there.

**Sub-tasks:**

- [x] **1.5.1** — Implement `start_run`, `events`, `stop` with the plan's signatures and the `RunEvent` dataclass. One `aiohttp.ClientSession` per client. Omit `model`/`provider` when `ModelChoice` is `None`. A missing per-profile key env var fails the task at once with reason "not configured" (no HTTP call).
- [x] **1.5.2** — Map SSE events to `RunEvent.kind`: `run.queued` → queued; `run.started` → started; `tool.started`/`tool.completed`/`tool.progress` → progress (text = tool name plus preview); `message.interim` → message; `run.completed` → completed (text = `output`); `run.failed` and `run.interrupted` → failed (text = `error` or "interrupted"); `run.cancelled` → cancelled. Ignore unknown events.
- [x] **1.5.3** — Retry and error policy keyed on `error.code`; `session_id` is never changed on any retry:
    - [x] **1.5.3a** — `session_busy` (409): retry the same body and same `Idempotency-Key` after 1 s, at most 5 times; then fail with a short spoken reason.
    - [x] **1.5.3b** — `runtime_draining` / `storage_unavailable` (503): retry the same request and key, honoring `Retry-After`, at most 3 times; then fail.
    - [x] **1.5.3c** — `admission_queued` (503): accepted; never resend. Return a result that tells the caller the task is `queued`. If the body carries a `run_id`, poll `GET .../v1/runs/{id}` until it is running and then stream events. If it carries none (the @6112c2a behavior), report the task as queued with no run id; the tracker resolves it per Task 1.6.
    - [x] **1.5.3d** — `idempotency_key_conflict` (409): fail the task and log at error level as a bug. Any other 4xx code: fail with a short spoken reason that names the code. Connection refused: fail with reason "Hermes is unreachable".
- [x] **1.5.4** — Event stream resilience: when the SSE stream drops before a terminal event, re-subscribe once with `Last-Event-ID`; if that fails, poll `GET .../v1/runs/{id}` every 5 s until terminal; after 3 failed polls emit a terminal `unknown` outcome.
- [x] **1.5.5** — `tests/fakes/fake_hermes_api.py`: an aiohttp fake of `/p/{profile}/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/runs/{id}/stop`, `/p/{profile}/v1/models`, with per-profile keys and scriptable responses (busy N times, draining with Retry-After, admission_queued with or without `run_id`, conflict, 4xx, mid-stream drop). It records every request (path, headers, body). Task 2.4 reuses it.
- [x] **1.5.6** — Contract tests: request path/headers/body (incl. `model_options.reasoning.effort`, omitted model when `None`); `session_busy` then 202 → same `session_id` and `Idempotency-Key` resent, task runs; `admission_queued` → no resend, queued → running via poll (body with `run_id`) and queued with no run id (body without); draining honors `Retry-After`; conflict and other 4xx fail; connection refused; SSE mapping for every event name; stream drop → resubscribe → poll → `unknown` after 3 failed polls; `stop` path.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_hermes_api_client.py` passes with every case in 1.5.6.
- [x] **Requirements:**
    - [x] "`POST {base}/p/{profile}/v1/runs` with `{input, session_id, model?, provider?, model_options: {reasoning: {effort}}}`, bearer key for that profile, `Idempotency-Key` = task uuid."
    - [x] "`session_id` = `duplex-{person}-{profile}` (stable → follow-up context)."
    - [x] "Stream `GET .../v1/runs/{id}/events`; map tool.started/completed and message.interim to progress, run.completed/failed/cancelled to terminal states; `POST .../stop` on cancel."
    - [x] Error mapping for `session_busy`, `admission_queued`, `runtime_draining`/`storage_unavailable`, `idempotency_key_conflict`, other 4xx, connection refused; "session_id is NEVER changed on retry."
    - [x] "Contract test with the fake API returning session_busy then success, and admission_queued."
    - [x] Edge case "SSE stream drops mid-run → Re-subscribe events once; else poll GET run status every 5 s until terminal → Mark `unknown` after 3 failed polls".
- [x] **Verify:** Read the recorded requests in the `session_busy` test and confirm all attempts carry the same `session_id` and `Idempotency-Key`.

---

### Task 1.6: TaskTracker

Track every task from routing to a terminal state, persist it before announcing, and answer status and cancel requests.

**Files:**

```
hermes_duplex/tasks.py            (create)
tests/unit/test_tasks.py          (create)
```

**Sub-tasks:**

- [x] **1.6.1** — `TaskTracker` with integer task numbers per service run, fields (task id, uuid, person, profile, effort, model, text, run_id, state, created/updated times, delegation ref), and states `routing → queued → running → completed | failed | cancelled | unknown` (`queued` may be skipped). Reject illegal transitions.
- [x] **1.6.2** — Append every transition to `$HERMES_HOME/duplex/tasks.jsonl` (create the directory). The `started` (or `queued`) record is flushed to disk before the session announces it. Writes never block the event loop (use `asyncio.to_thread` or a small writer task).
- [x] **1.6.3** — Queries: running/queued tasks for a person; the most recent running task for a person; a task by number; the task ahead in the same (person, profile) queue (for "queued behind task N").
- [x] **1.6.4** — Cancel: given a person and an optional number, choose the task, call a supplied async `stop(profile, run_id)` and mark `cancelled` when Hermes confirms; a task with no run id (admission_queued without `run_id`) is marked `cancelled` locally and logged that Hermes may still run it.
- [x] **1.6.5** — A queued task with no run id moves to `unknown` when the session ends or after a configurable timeout (default 10 min), and is logged.
- [x] **1.6.6** — Unit tests: state machine, JSONL written before announce (assert file content inside the announce callback), per-person queries, "queued behind task N", cancel most recent and by number, no-run-id cancel, timeout to `unknown`.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_tasks.py` passes with every case in 1.6.6.
- [x] **Requirements:**
    - [x] "TaskTracker states: routing → queued → running → completed|failed|cancelled|unknown (queued may be skipped); persisted append-only JSONL before announcing "started"."
    - [x] "cancel → stops the most recent running task of that speaker (or named by number)."
    - [x] "TaskTracker state `queued` when a run is admitted but not started … the voice model is told "queued behind task N"".
- [x] **Verify:** Run the tests with a temp `HERMES_HOME` and read `duplex/tasks.jsonl`: one JSON object per line, in transition order.

---

## Phase 2: End-to-end path (Milestone 1, part B)

### Task 2.1: Router core: knn backend and composite policy (fast path only)

Build the router interface, the local phrase matcher, and the composite policy that Milestone 1 needs: sticky rule, fallback, `_control`, the model table, and the decision log. The main path (Decisions, Jev, deadline, circuit breaker) arrives in Phase 3.

**Files:**

```
hermes_duplex/router/__init__.py      (create)
hermes_duplex/router/base.py          (create)
hermes_duplex/router/knn.py           (create)
hermes_duplex/router/composite.py     (create)
tests/unit/test_router_knn.py         (create)
tests/unit/test_router_composite.py   (create)
```

**Sub-tasks:**

- [x] **2.1.1** — `router/base.py`: `Effort`, `ProfileSpec`, `RouteRequest`, `RouteDecision`, `BackendResult`, `RouterBackend` exactly as in the plan's Interfaces.
- [x] **2.1.2** — `router/knn.py`: model2vec static embeddings (default model `minishlab/potion-base-8M`; configurable) over each profile's description plus examples, and `_control`'s examples. `configure()` embeds once; `classify()` returns softmax-normalized cosine scores as `profile_probs`, `effort_probs=None`, `followup_prob=None`. Run the embedding in a thread so the event loop is not blocked. Tests must not download a model: inject an embedding function, and use a tiny deterministic fake embedder in unit tests.
- [x] **2.1.3** — `_control`: built-in examples (status: "what are you working on", "any updates", "how's that task going"; cancel: "cancel that", "stop that task", "never mind") merged with optional config examples. Choices always include the default profile and `_control`. A `_control` decision carries which intent (status or cancel) and never dispatches to Hermes.
- [x] **2.1.4** — `CompositeRouter` (fast path only for now): accept the knn result; apply the sticky rule per speaker (active profile expires after `sticky_idle_s`, default 600; leave the active profile only if follow-up prob < 0.5 and the winner beats active by ≥ 0.2; when `followup_prob` is None, leave only if the winner beats active by ≥ 0.3); if knn top < 0.5, fall back to active profile, else the default profile; effort default `medium` when no effort probabilities exist.
- [x] **2.1.5** — Model lookup: `profiles.<p>.models.<effort>`; a missing cell uses `default_model`; else `model=None` (Hermes profile default). Leave room for the main path with a backend list and a `deadline_ms` parameter that Task 3.1 uses.
- [x] **2.1.6** — Decision log: one structured `logging` line per decision with speaker, profile, effort, model, confidence, top-3 probabilities, source backend, latency_ms, sticky flag.
- [x] **2.1.7** — Unit tests (stub backends and the fake embedder): route portfolio phrase → stock-picker; sticky follow-up "and what about Tesla" stays on stock-picker with `sticky=True`; topic switch "what's the weather tomorrow" → general; sticky expiry after idle; both sticky thresholds; `_control` status and cancel; model table cell, `default_model` fallback, `None`; one log line with all fields.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** Router unit tests pass with every case in 2.1.7, without network access.
- [x] **Requirements:**
    - [x] "Choices always include the default profile and `_control`; `_control` decisions never dispatch to Hermes."
    - [x] Sticky rule thresholds and 10 min idle expiry, including the `followup_prob is None` rule.
    - [x] "`_control` uses built-in examples (…) merged with optional config examples".
    - [x] "Model = `profiles.<p>.models.<effort>` (ModelChoice: provider + model, both required); missing cell falls back to profile `default_model`, else Hermes profile default (omit model in request)."
    - [x] "Every decision logs one structured line: speaker, profile, effort, model, confidence, top-3 probabilities, source backend, latency_ms, sticky flag."
    - [x] "Router sees all profiles; permission check happens after routing."
- [x] **Verify:** Run one real classification with the real model2vec model in a one-liner (network allowed) and confirm the latency in the log line is under 50 ms after warm-up.

---

### Task 2.2: VoiceSession core

Implement the session that pipes audio, consumes normalized provider events, turns delegations into routed and permitted tasks, dispatches them, and reports progress and results. This task covers the tool-style path and capability-driven branching; Phase 4 adds the live-style details and Phase 6 adds announcement gating and Realtime barge-in.

**Files:**

```
hermes_duplex/session.py              (create)
tests/unit/test_session.py            (create)
tests/unit/test_session_no_provider_names.py (create)
```

**Sub-tasks:**

- [x] **2.2.1** — Audio pump in: read `Conversation.frames()`, record each frame's `voiced_speakers` in the `AttributionRing` at its `t_ms`, and send PCM to the provider. Audio pump out: `OutputAudio` → `OutputPacer` → `Conversation.play()`.
- [x] **2.2.2** — Delegation flow for `DelegationRequested` with `tool_args`: create a `routing` task; credit the speaker with the most voiced ticks in the 4 s of `t_ms` before the call arrived (ties → most recent voiced); no voiced ticks → no dispatch and `provider.say("who's asking?")`. Route with `RouteRequest(text, speaker, history, active_profile, hint)`.
- [x] **2.2.3** — After routing: `_control` status → spoken summary of that speaker's running tasks; `_control` cancel → `TaskTracker` cancel and spoken confirmation. Routed profile not allowed → no dispatch, spoken refusal naming the profile, logged.
- [x] **2.2.4** — Dispatch: `start_run(profile, text, "duplex-{person}-{profile}", model, effort, task uuid)`; persist and then announce "started" (or "queued behind task N" when another task for the same person and profile is still open); stream events; `progress` → `provider.report_progress` (throttled to at most 1 per 5 s per delegation); terminal → `provider.deliver_result` with `frame_result(...)` for success, or a short failure/cancel notice. Tasks on different profiles run concurrently.
- [x] **2.2.5** — Branch only on `capabilities`, never on provider name. Write `test_session_no_provider_names.py`: a lint-style test that fails when `session.py` compares any value to a provider name string (`"gpt-live"`, `"openai-realtime"`, `"xai-realtime"`, `"fake"`, `"openai"`, `"xai"`).
- [x] **2.2.6** — Unit tests with the fake provider, a stub router, the fake Hermes API, and an in-memory `Conversation`: portfolio delegation dispatches to stock-picker with the right body; refusal for a disallowed person (no run, `say` contains the profile name); "who's asking?" with no voiced ticks; `_control` status and cancel (cancel hits `/stop`); second task same person+profile announces "queued behind task 1"; progress throttle; result text framed as data; Hermes unreachable → spoken notice.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_session.py tests/unit/test_session_no_provider_names.py` passes with every case in 2.2.6.
- [x] **Requirements:**
    - [x] "Session code branches only on capabilities, never on provider name (lint-style unit test: no provider-name string comparisons in session.py)."
    - [x] "Task credited to the person with the most voiced 20 ms ticks in … the 4 s of t_ms before the function call arrived (Realtime); ties → most recent voiced; no voiced ticks → no dispatch, spoken "who's asking?""
    - [x] "Routed profile not allowed for that person → no dispatch, spoken refusal naming the profile, logged."
    - [x] "`_control` status → spoken summary of running tasks for that speaker; cancel → stops the most recent running task of that speaker (or named by number)."
    - [x] "Same (person, profile) runs serialize …; the voice model is told "queued behind task N"; different profiles run concurrently."
- [x] **Verify:** Run the refusal test with `-s --log-level=INFO` and confirm one decision log line and one refusal log line.

---

### Task 2.3: Web transport

Serve the browser page and the authenticated audio WebSocket, and implement the `Transport` and `Conversation` protocols for it.

**Files:**

```
hermes_duplex/transports/web/__init__.py      (create)
hermes_duplex/transports/web/server.py        (create)
hermes_duplex/transports/web/static/index.html (create)
hermes_duplex/transports/web/static/app.js    (create)
hermes_duplex/transports/web/static/worklet.js (create)
tests/unit/test_web_transport.py              (create)
```

**Sub-tasks:**

- [x] **2.3.1** — aiohttp app on configurable host/port (default `127.0.0.1:8765`): `GET /` serves `index.html`; static routes serve `app.js` and `worklet.js`; `GET /ws` upgrades.
- [x] **2.3.2** — Auth before upgrade: token from query param `token` or from `Sec-WebSocket-Protocol` (`bearer, <token>`; echo the `bearer` subprotocol on success). Invalid or missing token → HTTP 401 before upgrade. A token maps to exactly one person.
- [x] **2.3.3** — `WebConversation`: binary frames in are PCM16 24 kHz mono; re-chunk to 20 ms `InputFrame`s with `t_ms` from the session's counter and `voiced_speakers = {person}` when the frame's RMS is above the configured voiced threshold. `play()` sends binary PCM16 24 kHz mono frames. `flush_playback()` sends `{"type":"interrupt"}` and returns ms actually played (from the pacer's released-ms count). `notify()` sends `{"type":"status", ...}` frames for transcript lines and the task list.
- [x] **2.3.4** — Browser page: token entry stored in `sessionStorage`; connect/disconnect buttons; AudioWorklet mic capture resampled to 24 kHz PCM16 mono in 20 ms chunks; playback queue; flush the playback queue on `{"type":"interrupt"}`; live transcript; task list. No external scripts or CDNs.
- [x] **2.3.5** — Unit tests with aiohttp's test client: 401 on missing/invalid token (both auth forms); successful upgrade with each form; binary in → `InputFrame`s with correct `t_ms` and voiced flag; `play()` → binary out; `flush_playback()` → interrupt frame; static files served.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_web_transport.py` passes with every case in 2.3.5.
- [x] **Requirements:**
    - [x] "aiohttp server on configurable host/port (default 127.0.0.1:8765); serves static page; `/ws` binary frames PCM16 24 kHz mono in and out; JSON control frames: `{"type":"interrupt"}`, `{"type":"status",...}`."
    - [x] "Page: token entry (stored in sessionStorage), connect/disconnect, live transcript, task list; playback flush on interrupt."
    - [x] "Web `/ws` upgrade without a valid token (query param `token` or `Sec-WebSocket-Protocol` bearer) → HTTP 401 before upgrade; token maps to exactly one person."
- [x] **Verify:** `curl -i http://127.0.0.1:8765/ws` against a running server returns `401`; open `/` in a browser (manual, optional) and confirm the token field and connect button render.

---

### Task 2.4: DuplexService, plugin lifecycle, CLI serve, and in-process end-to-end test

Wire everything into one `DuplexService`, start it inside the Hermes gateway and from `hermes duplex serve` through one factory, and prove the whole path in-process.

**Files:**

```
hermes_duplex/service.py                      (create)
hermes_duplex/plugin.py                       (modify)
hermes_duplex/cli.py                          (create)
plugin.yaml                                   (modify)
tests/unit/test_service_lifecycle.py          (create)
tests/unit/test_plugin_register.py            (create)
tests/e2e/__init__.py                         (create)
tests/e2e/test_web_fake_provider.py           (create)
tests/fixtures/router_eval/phrases.yaml       (create)
docs/system/architecture.md                   (create)
```

**Sub-tasks:**

- [x] **2.4.1** — Lifecycle seam spike. Install hermes-agent @`6112c2adb9e11f69734a29be9d2696f345ffa06a` into a scratch venv (`uv venv /tmp/h && uv pip install --python /tmp/h "hermes-agent @ git+https://github.com/NousResearch/hermes-agent@6112c2adb9e11f69734a29be9d2696f345ffa06a"`) and read `hermes_cli/plugins.py` (`register_platform` at ~:779, `spawn_task` at ~:425 which calls `asyncio.get_running_loop()` and so needs a running loop, `on_unload` at ~:416) and `hermes_cli/plugin_isolation.py:35`. Determine whether `register()` runs with a running event loop in the gateway. Choose: (a) `ctx.spawn_task(service.run())` when a loop is running, or (b) `ctx.register_platform(...)` with an adapter whose `connect()` starts the service. Do not import Hermes internals at runtime in either choice. Record the outcome and the evidence in `docs/system/architecture.md` under a "Gateway lifecycle seam" heading.
- [x] **2.4.2** — `service.py`: `build_service(config) -> DuplexService` is the one factory both modes use. It builds the provider factory (registry), router (CompositeRouter with configured backends), `HermesApiClient`, `TaskTracker`, `PermissionChecker`, and enabled transports. `start()` starts transports; each new `Conversation` gets one `VoiceSession`. `stop()` cancels all tasks, closes provider sockets, disconnects transports, and closes the web server, and finishes within 5 s (enforce with `asyncio.timeout(5)`; log anything it had to abandon).
- [x] **2.4.3** — `plugin.py` `register(ctx)`: register the Hermes `/duplex` slash command (status only: active conversations, running tasks, provider name; no join); register the `duplex` CLI command with subcommands `serve` (this task), `eval-router` (Task 3.4 fills it), `doctor` (Task 6.4 fills it); start the service through the seam from 2.4.1; `ctx.on_unload(...)` stops it. A missing `duplex.yaml` logs one warning and does not start the service (the gateway keeps working). Update `plugin.yaml` if the seam registers anything that must be declared.
- [x] **2.4.4** — `cli.py`: `hermes-duplex serve [--config PATH]` and the same handlers for the Hermes CLI subcommands; `serve` runs `build_service(...)` until SIGINT/SIGTERM, then `stop()`.
- [x] **2.4.5** — `tests/fixtures/router_eval/phrases.yaml`: labelled phrases for `general` and `stock-picker` (and `_control`), including the plan's scenario phrases, with expected profile and effort. Tasks 2.5 and 3.4 reuse it. The knn router in tests uses its examples.
- [x] **2.4.6** — In-process e2e `tests/e2e/test_web_fake_provider.py` (runs in the default tier, no marker): start `build_service` with the fake provider (`delegate_portfolio.jsonl`), knn router (fake embedder or real model2vec if cached; prefer a deterministic embedder), and `fake_hermes_api`; connect an aiohttp WS client with rob's token; stream voiced PCM (a −20 dBFS sine) at real-time pace; assert a run on `/p/stock-picker/v1/runs`, the second task reuses `session_id` `duplex-rob-stock-picker` with `sticky=True` in the decision log, and the fake recorded `deliver_result` text containing the report template. Repeat with alex's token: refusal recorded via `say`, no run requested.
- [x] **2.4.7** — Unit tests: standalone startup and shutdown through `cli` handlers; `stop()` within 5 s even when a provider `close()` hangs (assert elapsed < 5.5 s); `register(ctx)` with a recording fake ctx registers only documented methods and does not raise when `duplex.yaml` is missing.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest -m "not live and not e2e"` passes, including `tests/e2e/test_web_fake_provider.py`, the lifecycle tests, and the register test.
- [x] **Requirements:**
    - [x] "Both paths construct DuplexService through one factory; … a unit test exercises standalone startup/shutdown."
    - [x] "Service stop cancels all tasks, closes provider sockets, disconnects Discord, closes web server within 5 s (asserted)."
    - [x] "register(ctx): registers Hermes-side `/duplex` command (status only …), `hermes duplex` CLI subcommands (serve, eval-router, doctor), and starts DuplexService via the lifecycle seam chosen in task 1 … record outcome in docs/system/architecture.md".
    - [x] "In-process e2e: web WS → session (fake provider) → knn router → fake Hermes API; runs in default CI".
- [x] **Verify:** `uv run hermes-duplex serve --config <test config>` starts, `curl -i 127.0.0.1:8765/` returns 200, and Ctrl-C exits in under 5 s. `docs/system/architecture.md` names the chosen seam and the evidence.

---

### Task 2.5: Real-Hermes end-to-end test and CI e2e job

Prove the plugin against a real Hermes gateway: install, validate, run the API server with two profiles backed by a fake LLM, and route and dispatch through the web transport.

**Files:**

```
tests/fakes/fake_llm.py                       (create)
tests/fixtures/hermes_home/                   (create)
tests/e2e/conftest.py                         (create)
tests/e2e/test_real_hermes.py                 (create)
.github/workflows/ci.yml                      (modify)
```

**Hermes setup facts (hermes-agent @6112c2a):**

- Install: `uv pip install "hermes-agent[messaging] @ git+https://github.com/NousResearch/hermes-agent@6112c2adb9e11f69734a29be9d2696f345ffa06a"` (console script `hermes`). Hermes needs Python 3.14 and the same `pynacl>=1.6,<1.7` override.
- Profiles live at `$HERMES_HOME/profiles/<name>/` with their own `config.yaml` and `.env`. The default profile is `$HERMES_HOME` itself. The gateway serves every profile; `/p/<profile>/v1/...` is answered on the default profile's API server listener.
- API server: default profile `.env` sets `API_SERVER_ENABLED=true`, `API_SERVER_KEY=<≥16 chars>`, `API_SERVER_HOST=127.0.0.1`, `API_SERVER_PORT=<free port>` (default 8642). Each named profile's `.env` sets its own `API_SERVER_KEY` (≥16 chars); `/p/<profile>/` rejects the default key.
- Custom OpenAI-compatible provider in a profile's `config.yaml`: `providers: {fake: {api: "http://127.0.0.1:<port>/v1", transport: chat_completions}}` and `model: {provider: fake, default: <model name>}`. Hermes sends the configured reasoning effort unchanged to a custom `chat_completions` endpoint as `reasoning_effort`.
- Commands: `hermes plugins validate <path>` (catalog gate, exits 0/1; fails if `register()` registers an undeclared hook/tool); `hermes plugins install <git URL>` (accepts a Git URL; a local `file://` repo URL may work); `hermes plugins enable <name>`; `hermes gateway run` (foreground).
- Run status after completion carries `runtime.requested.{provider, model}`. At @6112c2a no run status field carries the requested reasoning effort.

**Sub-tasks:**

- [x] **2.5.1** — `tests/fakes/fake_llm.py`: an aiohttp OpenAI-compatible stub with `GET /v1/models` and `POST /v1/chat/completions` (non-streaming JSON and `stream: true` SSE chunks ending with `data: [DONE]`). It returns deterministic text per model name (for example `stock-picker says: portfolio up 2%`), answers tool-call-free, and records every request body (model, `reasoning_effort` if present) for assertions. Runnable as a module with `--port`.
- [x] **2.5.2** — `tests/fixtures/hermes_home/`: templates for the default profile `.env` (API server on), profile `general` and `stock-picker` (`config.yaml` with the `fake` provider; `.env` with per-profile keys), and a `duplex.yaml` that uses provider `fake` with `script` and `record_path`, router `knn` with the Task 2.4 phrases, profile keys env names, rob (all profiles) and alex (general only), web transport on a free port, and a stock-picker model table cell for `high` and `medium`.
- [x] **2.5.3** — `tests/e2e/conftest.py`: mark `e2e`; skip with a clear reason when the `hermes` command is not on PATH or `import hermes_cli` fails. Fixture: create a temp `HERMES_HOME` from the templates; start `fake_llm`; run `hermes plugins validate <repo>` and assert exit 0; install the plugin from the repo path (try `hermes plugins install file://<repo>`; if refused, copy the repo into `$HERMES_HOME/plugins/hermes-duplex/` and run `hermes plugins enable hermes-duplex`); start `hermes gateway run` as a subprocess; wait until `GET /p/general/v1/models` answers 200; on teardown stop the gateway (SIGTERM, then SIGKILL after 10 s) and remove the temp home.
- [x] **2.5.4** — `tests/e2e/test_real_hermes.py`: connect to the in-gateway web transport with rob's token and stream voiced PCM at real-time pace while the fake script delegates "how is my portfolio doing" then "and what about Tesla". Assert:
    - [x] **2.5.4a** — Both runs were created on `/p/stock-picker/...` (read run ids from `$HERMES_HOME/duplex/tasks.jsonl`) and both use `session_id` `duplex-rob-stock-picker`; the decision log (or tasks JSONL) shows the second route with `sticky=true`.
    - [x] **2.5.4b** — `GET /p/stock-picker/v1/runs/{id}` reports `status: completed` and `runtime.requested` equal to the model table's provider and model for the routed effort. For effort: assert it from the run status if any field carries it; otherwise assert `reasoning_effort` in the fake LLM's recorded request when present, and report in the Final Report which boundary carried it.
    - [x] **2.5.4c** — The fake LLM received the table's model name.
    - [x] **2.5.4d** — The fake provider's `record_path` contains a `deliver_result` whose text holds the report template and the fake LLM's text.
    - [x] **2.5.4e** — With alex's token, the portfolio delegation produces a `say` refusal and no new run (no new task with a run id in `tasks.jsonl`, no new stock-picker request at the fake LLM).
- [x] **2.5.5** — Fill the `e2e` CI job: Python 3.14, `uv sync --frozen`, install hermes-agent at the pinned SHA into the same environment, `uv run pytest -m e2e`. If Hermes cannot install on Python 3.14 in GitHub Actions, keep the job but mark it `continue-on-error: true`, record the failure output in the Final Report, and Task 7.2 records "local only" in the README (plan Risk).

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest -m e2e` passes locally where Hermes installs (or skips with the stated reason where it does not); `uv run pytest -m "not live and not e2e"` still passes.
- [x] **Requirements:**
    - [x] "`plugin.yaml` validates under hermes-agent @6112c2a's loader (`hermes plugins validate <path>` exits 0 in the real-Hermes e2e job)."
    - [x] "Installs hermes-agent at pinned SHA 6112c2a into the test env (CI job; locally skipped with reason if `hermes` not importable/installed)."
    - [x] "Creates temp HERMES_HOME with profiles `general` and `stock-picker`, each configured with an OpenAI-compatible custom provider pointing at `tests/fakes/fake_llm.py` …; enables API server with per-profile keys; installs this plugin from the repo path; starts `hermes gateway`."
    - [x] "Asserts: run created on `/p/stock-picker/...`, second task reuses session id (sticky), … requested model, provider and reasoning effort …, result announced through provider …, unauthorized person gets refusal and no run."
    - [x] "Gateway shut down and temp home removed after test."
    - [x] "the CI e2e exercises in-gateway".
- [x] **Verify:** After a local e2e run, confirm no `hermes gateway` process remains (`pgrep -f "hermes gateway"` prints nothing) and the temp home is gone.

---

## Phase 3: Router (Milestone 2)

### Task 3.1: Composite main path: deadline, fallback order, circuit breaker, and hint

Extend `CompositeRouter` to run the knn fast path and a decision-model main path in parallel, with the deadline, fallback order, circuit breaker, and the optional profile hint.

**Files:**

```
hermes_duplex/router/composite.py         (modify)
tests/unit/test_router_composite.py       (modify)
```

**Sub-tasks:**

- [x] **3.1.1** — Run fast path (knn, when enabled) and main path (configured backend) concurrently. Main-path deadline `deadline_ms` (default 300, configurable). Accept the fast path early when its top prob ≥ 0.85, margin ≥ 0.3, and it agrees with the sticky profile (cancel the main path then).
- [x] **3.1.2** — Fallback order when the main path misses the deadline or fails: knn if its top ≥ 0.5 → `hint` (source `hint`) when given and knn is not confident → active profile → default profile; effort `medium`. Use the main path's effort probabilities and follow-up probability when it answers.
- [x] **3.1.3** — Circuit breaker: after 3 consecutive main-path failures or timeouts, skip the main path for 30 s; log open and close.
- [x] **3.1.4** — Unit tests (stub backends with injectable delays): main path answers in time → source = backend; "Decisions timeout" (stub sleeps 1 s) → knn used, `source=knn`, latency < 350 ms; early fast-path accept; hint used only when main times out and knn is not confident; all backends fail → active profile else default, effort medium, `source=fallback`; breaker opens after 3 and closes after 30 s (injected clock); sticky rule with real `followup_prob`.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_router_composite.py` passes with every case in 3.1.4.
- [x] **Requirements:**
    - [x] "Composite: main-path deadline default 300 ms (configurable); fast path accepted early when top prob ≥ 0.85 and margin ≥ 0.3 and consistent with sticky profile; fallback order main → knn (if top ≥ 0.5) → active profile → `general` (configurable default profile), effort default medium."
    - [x] "Circuit breaker: after 3 consecutive main-path failures/timeouts, skip main path for 30 s."
    - [x] "Pass an optional `profile_hint` from the Realtime `delegate` tool into RouteRequest; router uses it only as fallback when main path times out and knn is not confident." (router side)
    - [x] Scenario "Decisions timeout: stub main backend sleeps 1 s → knn result used, source=knn, latency < 350 ms".
- [x] **Verify:** Run the timeout test 20 times in a bash loop (`for i in $(seq 20); do uv run pytest -q -k timeout tests/unit/test_router_composite.py || break; done`) and confirm no failure.

---

### Task 3.2: OpenAI Decisions backend

Implement the `decisions` router backend over `aiohttp`.

**Files:**

```
hermes_duplex/router/decisions.py             (create)
tests/contract/test_router_decisions.py       (create)
```

**Protocol facts (openai 3.42.1 `types/decision_create_params.py`, `types/decision.py`):**

- `POST https://api.openai.com/v1/decisions`, header `Authorization: Bearer $OPENAI_API_KEY`. Body: `{"model": "gpt-6-luna", "input": <string or message list>, "questions": [...]}`. Only `gpt-6-luna` is supported.
- Questions: `{"type": "choice", "name": "profile", "instructions": "...", "choices": [{"value": "<profile>", "description": "<profile description>"}, ...]}` (2–255 unique choices); `{"type": "choice", "name": "effort", "instructions": "...", "choices": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]}`; `{"type": "predicate", "name": "followup", "instructions": "The request continues the previous task's topic."}`.
- Response: `{"answers": [...], "model", "usage"}`, answers in question order. Choice answer: `{"type": "choice", "name", "choice", "confidence", "probabilities": [{"value", "probability"}]}`. Predicate answer: `{"type": "predicate", "name", "probability"}`. A `{"type": "refusal"}` answer can appear.

**Sub-tasks:**

- [x] **3.2.1** — `configure()` builds the three questions from `ProfileSpec`s, with `_control` as a choice using a fixed description ("status or cancel of running voice tasks"). `classify()` sends the request text plus the last few history lines (as a message list) and respects `deadline_ms` with a client timeout.
- [x] **3.2.2** — Map answers to `BackendResult`: `profile_probs` and `effort_probs` from the probabilities lists, `followup_prob` from the predicate; refusal or malformed answer → raise a backend error (counts toward the breaker). API key from the env var named in config.
- [x] **3.2.3** — Contract tests against an aiohttp fake: request body shape (model, three questions, choices with descriptions, `_control` present); response mapping; refusal; HTTP 5xx; slow response beyond the deadline.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_router_decisions.py` passes with every case in 3.2.3.
- [x] **Requirements:**
    - [x] "hermes_duplex/router/decisions.py — OpenAI Decisions backend (POST /v1/decisions; choice: profile, choice: effort, predicate: follow-up)".
    - [x] "decision backends get `_control` as a choice with a fixed description."
- [x] **Verify:** With `OPENAI_API_KEY` set (optional, manual), run one classification and confirm the log line shows `source=decisions`.

---

### Task 3.3: Jev backend

Implement the `jev` router backend (TypeSafe System One API) over `aiohttp`, with a configurable base URL so Cloudflare Clef works.

**Files:**

```
hermes_duplex/router/jev.py               (create)
tests/contract/test_router_jev.py         (create)
```

**Known facts (research-router.md):** endpoint `POST {base_url}/v1/systemone`, default `base_url` `https://api.typesafe.ai`, model `jev-latest`, bearer key, question types choice/score/predicate with probabilities and confidence. Cloudflare Clef offers a "Jev compatible" API at another base URL. The exact request and response field names are not in the plan; confirm them from `https://docs.typesafe.ai` before you write the mapping.

**Sub-tasks:**

- [x] **3.3.1** — Confirm the System One wire shape from the official docs and record the doc URL and the shape in the module docstring. Keep the wire mapping in two small functions (build request, parse response) so a shape change touches one place.
- [x] **3.3.2** — Ask the same three questions as Task 3.2 (profile choice incl. `_control`, effort choice, follow-up predicate) and map the answers to `BackendResult`. `base_url`, model, and key env var come from config. No `typesafe-sdk` dependency.
- [x] **3.3.3** — Contract tests against an aiohttp fake that serves the documented shape: request shape; mapping; non-default `base_url`; 5xx; deadline.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_router_jev.py` passes with every case in 3.3.3.
- [x] **Requirements:**
    - [x] "Backends: knn (local), decisions (OpenAI), jev (TypeSafe; base_url configurable). Selected by config; `knn` always runs as fast path when enabled."
    - [x] "Jev backend uses aiohttp directly (no typesafe-sdk)."
- [x] **Verify:** The module docstring names the docs URL used and the request/response shape; `grep -n typesafe pyproject.toml` prints nothing.

---

### Task 3.4: Router eval harness and `eval-router` command

Measure routing quality and latency per backend and for the composite router from a labelled phrase file.

**Files:**

```
hermes_duplex/router/eval.py              (create)
hermes_duplex/cli.py                      (modify)
hermes_duplex/plugin.py                   (modify)
tests/fixtures/router_eval/phrases.yaml   (modify)
tests/unit/test_router_eval.py            (create)
```

**Sub-tasks:**

- [x] **3.4.1** — Extend `phrases.yaml` toward 30–50 labelled utterances per profile, including ASR-style noise, follow-ups (with the preceding phrase), out-of-scope phrases, and `_control` phrases, each with expected profile and effort.
- [x] **3.4.2** — `eval.py`: run each backend alone and the composite over the file; compute accuracy, macro-F1, confusion matrix, effort exact and off-by-one rate, latency p50/p95/p99, and deadline-miss rate.
- [x] **3.4.3** — CLI: `hermes-duplex eval-router <phrases.yaml> [--backend knn|decisions|jev|composite] [--json] [--config PATH]` and the same under `hermes duplex eval-router`. Human table by default, JSON with `--json`; exit 0 when the run completes.
- [x] **3.4.4** — Unit test with a stub backend on a small fixture: exact metric values, confusion matrix, off-by-one, percentiles, miss rate, JSON output schema, exit code 0.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_router_eval.py` passes.
- [x] **Requirements:**
    - [x] "Prints accuracy, macro-F1, confusion matrix, effort exact + off-by-one rate, latency p50/p95/p99, deadline-miss rate, per backend and composite; exit 0; `--json` output option. Unit test on a fixture with a stub backend."
- [x] **Verify:** `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` prints the table and exits 0 (needs the model2vec model download once).

---

## Phase 4: GPT-Live adapter (Milestone 3)

### Task 4.1: Shared WebSocket client

Write the WebSocket layer both provider adapters use.

**Files:**

```
hermes_duplex/providers/wire.py       (create)
tests/fakes/fake_ws_server.py         (create)
tests/unit/test_wire.py               (create)
```

**Sub-tasks:**

- [x] **4.1.1** — `WireClient` over `websockets` 15 (`websockets.asyncio.client.connect`): URL plus `additional_headers` (`Authorization: Bearer <key>`), `max_size=None`, JSON send, an async iterator of decoded JSON events, tolerance for application-level `{"type":"ping"}` and unknown event types (pass them through; adapters ignore them), and clean close.
- [x] **4.1.2** — Reconnect helper with exponential backoff (for example 0.5 s, 1 s, 2 s) and a max attempts parameter; the session (Task 4.4) decides when to reconnect.
- [x] **4.1.3** — `tests/fakes/fake_ws_server.py`: a `websockets` server that replays a recorded JSONL event script, can wait for a client event type before replying, records every client event, and can drop the connection on cue. Contract tests in Tasks 4.2 and 6.1 use it.
- [x] **4.1.4** — Unit tests: headers sent, JSON round trip, ping/unknown tolerated, close, backoff timing (injected sleep).

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_wire.py` passes.
- [x] **Requirements:**
    - [x] "hermes_duplex/providers/wire.py — shared WebSocket client (websockets 15): connect with auth headers, JSON event send/recv, ping tolerance, reconnect backoff".
- [x] **Verify:** `grep -n "import openai" -r hermes_duplex` prints nothing.

---

### Task 4.2: GPT-Live adapter

Implement the GPT-Live provider over `wire.py` with client delegation.

**Files:**

```
hermes_duplex/providers/gpt_live.py                   (create)
hermes_duplex/providers/registry.py                   (modify)
tests/fixtures/provider_scripts/gpt_live_*.jsonl      (create)
tests/contract/test_gpt_live_adapter.py               (create)
```

**Protocol facts (openai 3.42.1 `resources/live/live.py`, `types/live/*`):**

- Connect: `wss://api.openai.com/v1/live/sessions`, header `Authorization: Bearer $OPENAI_API_KEY`. Do not put the model in the URL.
- First client event: `{"type": "session.start", "session": {"model": "gpt-live-1", "instructions": "<prompts.py text>", "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "<config voice, default marin>"}}, "delegation": {"type": "client"}, "input": [<optional text history items>]}}`. Wait for `session.started` before other commands. Voice, format, and instructions are immutable after start. `input` takes up to 128 text messages (developer/user/assistant, one text part each); use it for the re-seed summary (Task 4.4).
- Audio in: `{"type": "session.input_audio.append", "audio": "<base64 PCM16LE mono 24 kHz>"}` (no acknowledgment).
- Server events: `session.started`; `session.output_audio.delta` (`delta` base64; on the primary WebSocket no `start_ms`/`end_ms`); `session.input_transcript.delta` and `session.output_transcript.delta` (`delta`, `start_ms`, `end_ms` on the session timeline; no done event); `session.delegation.created` (`delegation: {id, target: "client"|"responses"|"decisions", type: "delegation"}`, `offset_ms`; no task text); `session.thinking.appended`, `session.commentary.appended`; `session.closed` (`reason`: `close_requested`|`expired`|`content`|`remote_hangup`|`connection_lost`); `error` (`error: {code, message, type, param?, client_event_id?}`); `info`. There are no speech-started/stopped or response-done events.
- Client returns: `{"type": "session.thinking.append", "content": "<≤500 tokens>", "delegation_id": "<id>"|null}` (silent); `{"type": "session.commentary.append", "content": "<≤500 tokens>", "delegation_id": "<id>"|null}` (spoken). Close: `{"type": "session.close"}`.

**Sub-tasks:**

- [x] **4.2.1** — Capabilities: input_rate 24000, output_rate 24000, supports_truncate false, supports_out_of_band_response false, emits_speech_events false, delegation_style `"live"`, max_session_s None.
- [x] **4.2.2** — `connect()` sends `session.start` and waits for `session.started` → `SessionReady`; `send_audio` base64-encodes into `session.input_audio.append`.
- [x] **4.2.3** — Map server events: output audio delta → `OutputAudio(pcm, item_id=None)`; input/output transcript deltas → `InputTranscript`/`OutputTranscript` (keep `start_ms`/`end_ms` available to the session); `session.delegation.created` → `DelegationRequested(id, offset_ms, target, tool_args=None)` and log `target` (expect `client`; warn otherwise); `session.closed` → `Closed(reason)`; `error` → `ProviderError`; ignore others.
- [x] **4.2.4** — `report_progress` → `session.thinking.append` with `delegation_id`; `deliver_result` → `session.commentary.append` with `delegation_id`; `say` → `session.commentary.append` with `delegation_id: null`; `truncate` → no-op. Trim content to the 500-token cap.
- [x] **4.2.5** — Register `gpt-live` in the registry.
- [x] **4.2.6** — Contract tests over `fake_ws_server` with recorded scripts: start handshake and payload; audio append encoding; event mapping including the `target` log; thinking/commentary payloads with `delegation_id`; `say` with `null`; closed and error mapping.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_gpt_live_adapter.py` passes.
- [x] **Requirements:**
    - [x] "session.start sends instructions from prompts.py, voice from config, `delegation: {type: "client"}`."
    - [x] "GPT-Live emits no SpeechStarted/SpeechStopped/OutputDone and no item ids …; its capabilities set emits_speech_events=false, supports_truncate=false."
    - [x] "Log `session.delegation.created.target` (Literal client|responses|decisions; expect "client")."
    - [x] "Progress → `session.thinking.append` …; final result → `session.commentary.append` with delegation_id; failures → commentary with a short apology and reason."
- [x] **Verify:** Read one recorded client-event log from the tests and confirm the first event is `session.start` with `delegation.type == "client"`.

---

### Task 4.3: Live-style session behavior: transcript windows, attribution, and loudness-gate barge-in

Add the session behavior GPT-Live needs: derive task text from transcripts, credit the speaker on the session timeline, throttle thinking, and flush playback locally when someone talks over the model.

**Files:**

```
hermes_duplex/session.py                  (modify)
hermes_duplex/audio/gate.py               (create)
tests/unit/test_gate.py                   (create)
tests/contract/test_gpt_live_session.py   (create)
```

**Sub-tasks:**

- [x] **4.3.1** — Transcript windows: keep input transcript deltas with their `start_ms`/`end_ms`. On `DelegationRequested` with `tool_args=None`, the task text is the input transcript from the previous delegation's offset (or the last 30 s) up to `offset_ms + 1500` ms grace, plus the last assistant output sentence as context. When two delegations arrive within 1 s, split the windows at the first delegation's offset so each gets its own text.
- [x] **4.3.2** — Attribution for live style: credit the person with the most voiced ticks in `[offset_ms − 4000, offset_ms + 500]` (same `t_ms` timeline); wait until `t_ms` passes `offset_ms + 500` before crediting.
- [x] **4.3.3** — `audio/gate.py`: RMS loudness gate per allowlisted speaker with a threshold (dBFS, from config) and a hangover; `trip()` reports a rising edge.
- [x] **4.3.4** — Barge-in when `emits_speech_events` is false: run the gate on allowlisted input while the pacer holds or plays output; on a trip, call `Conversation.flush_playback()` and `pacer.drop()` within one 20 ms tick; send no truncate; later output deltas play normally.
- [x] **4.3.5** — Thinking throttle: at most 1 `report_progress` per 5 s per delegation; failures go through `deliver_result` as a short apology with the reason.
- [ ] **4.3.6** (DEFERRED: MAY item, default off; needs side-effect-free router mode + config flag — follow-up) — MAY (config flag, default off): pre-warm routing from input transcript deltas before `session.delegation.created`; when the delegation arrives and the window text matches, reuse the pending decision.
- [x] **4.3.7** — Contract tests (GPT-Live adapter over `fake_ws_server` + real session + stub router + fake Hermes API): "GPT-Live delegation" (task text = utterance; thinking.append on progress; commentary.append on completion); back-to-back delegations and overlapping speech produce two tasks with split text; "GPT-Live barge-in" (output streaming, input RMS above gate → flush + pacer drop within 1 tick, no truncate sent). Unit tests for the gate threshold and hangover.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_gpt_live_session.py tests/unit/test_gate.py` passes.
- [x] **Requirements:**
    - [x] "On `session.delegation.created`, emit DelegationRequested; the session derives task text from input transcript since the previous delegation (or last 30 s) up to offset_ms + 1500 ms grace, plus the last assistant output sentence; contract test covers back-to-back and overlapping delegations."
    - [x] "Barge-in: when emits_speech_events=false, the session runs audio/gate.py on allowlisted input …; gate trip → transport.flush_playback() + pacer.drop() within one 20 ms tick; later output deltas play normally … Contract test: output streaming, input energy rises → flush within 1 tick, held backlog dropped."
    - [x] "Task credited to the person with the most voiced 20 ms ticks in [offset_ms − 4000, offset_ms + 500] (GPT-Live)".
    - [x] "Progress → `session.thinking.append` (≤500 tokens, throttled to ≤1 per 5 s per delegation)".
    - [x] MAY "Pre-warm routing from GPT-Live input transcript deltas … behind a config flag; default off."
- [x] **Verify:** In the barge-in test, assert and print the elapsed ticks between the gate trip and the flush (must be ≤ 1).

---

### Task 4.4: Provider reconnect, re-seed, and time-base reset

Keep a conversation alive when the provider socket closes or the session expires.

**Files:**

```
hermes_duplex/session.py                  (modify)
tests/unit/test_session_reconnect.py      (create)
```

**Sub-tasks:**

- [x] **4.4.1** — On `Closed` (any reason except a close the session asked for) or a socket error: reconnect with backoff, at most 3 times. Re-seed with the instructions plus a summary of the last 10 transcript lines and the running tasks (`history_summary` argument of `connect`). After 3 failures, end the conversation with a spoken message through the transport if possible, then `Conversation.close(reason)`.
- [x] **4.4.2** — On every (re)connect, reset `t_ms` to 0 and clear the `AttributionRing`.
- [x] **4.4.3** — Running Hermes tasks continue across the reconnect; their progress and results go to the new provider session. A delegation ref from the old session cannot be answered there: deliver the result through `say` (live style, `delegation_id: null`) or the announcement path (tool style).
- [x] **4.4.4** — Unit tests with the fake provider: "Provider reconnect" (fake closes the socket mid-task → reconnect, re-seed text contains the last transcript lines and the running task, result announced on the new session); "Attribution across reconnect" (delegation offset after reconnect credited using new-session `t_ms`; ring buffer reset); 3 failures → end message and close.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_session_reconnect.py` passes.
- [x] **Requirements:**
    - [x] "On provider Closed/expired: reconnect with backoff (max 3), re-seed instructions plus a summary of the last 10 transcript lines and running tasks; running Hermes tasks continue and announce on the new session."
    - [x] "Time base: `InputFrame.t_ms` … reset to 0 on provider (re)connect; GPT-Live `offset_ms` is on the same timeline. The attribution ring buffer is cleared on reconnect." and "Unit test crossing a reconnect."
- [x] **Verify:** Read the fake provider's recorded `connect` calls in the reconnect test: two calls, the second with a non-empty `history_summary`.

---

## Phase 5: Discord transport (Milestone 4)

### Task 5.1: Resampling and allowlist mixing

Add streaming resamplers and the per-tick mixer that keeps only allowlisted speakers.

**Files:**

```
hermes_duplex/audio/resample.py       (create)
hermes_duplex/audio/mixer.py          (modify)
tests/unit/test_resample.py           (create)
tests/unit/test_mixer.py              (create)
```

**Sub-tasks:**

- [x] **5.1.1** — `resample.py`: `soxr.ResampleStream`-based streaming resamplers that keep state across chunks: 48 kHz stereo → provider input rate mono (24 kHz or 16 kHz; downmix by averaging channels), and provider output rate mono → 48 kHz stereo (duplicate channels). If Task 1.1.1 found no `soxr` wheel, implement the same API with `numpy` polyphase resampling instead.
- [x] **5.1.2** — Mixer: per 20 ms tick, take each speaker's 48 kHz stereo frame, drop frames from people not on the allowlist before mixing, sum the rest with int32 accumulation and clip to int16, and return the mixed frame plus the set of person ids whose frames were voiced (RMS above the voiced threshold).
- [x] **5.1.3** — Unit tests: "Resample continuity" (1 s sine split in odd chunks matches a one-shot resample within tolerance, no discontinuity at chunk joins); stereo/mono conversion; "Unknown Discord user" (frames from a non-allowlisted speaker absent from the mix); clipping; voiced set.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_resample.py tests/unit/test_mixer.py` passes.
- [x] **Requirements:**
    - [x] "resample to provider input rate with soxr streaming (carry state, no clicks)."
    - [x] "Audio from unknown Discord users is dropped before mixing (unit test with mixed SSRCs)."
    - [x] "hermes_duplex/audio/mixer.py — per-tick mix of allowed speakers (sum + clip), per-tick voiced-speaker attribution ring buffer".
- [x] **Verify:** Print the maximum sample difference at chunk joins in the continuity test; it is within the stated tolerance.

---

### Task 5.2: Adapted DAVE voice receiver

Adapt Hermes core's `VoiceReceiver` into a streaming per-SSRC receiver with attribution.

**Files:**

```
hermes_duplex/transports/discord/__init__.py      (create)
hermes_duplex/transports/discord/receiver.py      (create)
NOTICE                                            (create)
tests/unit/test_receiver.py                       (create)
tests/unit/test_discord_internals_guard.py        (create)
```

**Source facts:** `https://github.com/NousResearch/hermes-agent/blob/6112c2adb9e11f69734a29be9d2696f345ffa06a/plugins/platforms/discord/adapter.py`, class `VoiceReceiver`, lines 703–1222 (MIT © 2025 Nous Research). It reads `voice_client._connection` (discord.py `VoiceConnectionState`) and uses: `add_socket_listener`, `remove_socket_listener`, `hook` (wrapped to read SPEAKING op 5 `{ssrc, user_id}` and ops 4/21/22 for key and DAVE transitions), `ws` and `ws._hook`, `secret_key`, `dave_session` (`.decrypt(user_id, davey.MediaType.audio, data)`), `dave_protocol_version`, `ssrc`; plus `voice_client.channel.members`, `voice_client.user`, and `discord.opus.Decoder`. Credentials are kept as one tuple `(secret_key, dave_session, dave_protocol_version, dave_downgraded)`. `REKEY_FAILURE_STREAK = 25` NaCl failures re-resolve credentials. Unmapped SSRC under active DAVE (protocol > 0 and no passthrough window) is dropped; sole-allowed-member inference is at lines 1050–1053 and 1106–1124.

**Sub-tasks:**

- [x] **5.2.1** — Copy the class into `receiver.py` with an attribution header naming the source URL, commit, line range, and MIT © 2025 Nous Research. Write `NOTICE` with the same attribution.
- [x] **5.2.2** — Replace utterance buffering (`SILENCE_THRESHOLD`, `check_silence`) with streaming: each decoded 20 ms PCM frame (48 kHz stereo) goes to a callback `on_frame(person_id, pcm)` on the event loop thread (use `loop.call_soon_threadsafe`). Remove the pause-while-playing flag (the providers handle turn-taking).
- [x] **5.2.3** — Keep: NaCl + DAVE decrypt, per-SSRC Opus decoders, SSRC→user from SPEAKING, sole-allowed-member inference for an unmapped SSRC (else drop), drop of frames under a stale key generation and of unmapped DAVE frames, rekey streak. Map Discord user ids to person ids through `PermissionChecker`; unknown users are dropped here too.
- [x] **5.2.4** — `test_discord_internals_guard.py`: on the pinned discord.py, assert that `discord.voice_state.VoiceConnectionState` (or the class `VoiceClient._connection` resolves to) has `add_socket_listener`, `remove_socket_listener`, `hook`, `secret_key`, `dave_session`, `dave_protocol_version`, `ssrc`, `ws`; that the voice websocket class has `_hook`; that `davey.MediaType.audio` and `discord.opus.Decoder` exist. Use `inspect`/class attributes, not a live connection.
- [x] **5.2.5** — Unit tests with synthetic packets (NaCl-encrypted test RTP built with `nacl.secret.Aead` and a test key; DAVE path with a stub session object): mapped SSRC → callback; unmapped SSRC with one allowlisted member → inferred; with two → dropped; stale generation → dropped; non-allowlisted user → dropped.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_receiver.py tests/unit/test_discord_internals_guard.py` passes.
- [x] **Requirements:**
    - [x] "Receiver adapted from Hermes VoiceReceiver: NaCl + DAVE decrypt via davey, per-SSRC Opus decode at 48 kHz stereo, SSRC→user from SPEAKING events; unmapped SSRC → keep Hermes's sole-allowed-member inference …, else drop; stale key-generation frames dropped; streaming frames (no silence segmentation)."
    - [x] "Unit test asserts the discord.py private attributes the receiver hooks exist on the pinned version."
    - [x] "LICENSE (MIT), NOTICE — attribution for adapted Hermes VoiceReceiver (MIT © 2025 Nous Research)".
- [x] **Verify:** `head -20 hermes_duplex/transports/discord/receiver.py` shows the attribution header; `grep -n check_silence hermes_duplex/transports/discord/receiver.py` prints nothing.

---

### Task 5.3: Playback AudioSource

Implement the Discord playback source that never starves discord.py's player thread.

**Files:**

```
hermes_duplex/transports/discord/playback.py      (create)
tests/unit/test_playback.py                       (create)
```

**Sub-tasks:**

- [x] **5.3.1** — `DuplexAudioSource(discord.AudioSource)`: `is_opus()` False; `read()` returns one 3840-byte 48 kHz stereo PCM frame; on underrun return a silent 3840-byte frame, never `b""` (an empty read ends discord.py's player thread).
- [x] **5.3.2** — Thread-safe bounded queue of 5 s (250 frames) that drops the oldest frames on overflow; `feed(pcm48_stereo)` re-chunks to 3840-byte frames with a carry buffer.
- [x] **5.3.3** — `flush()` empties the queue within one tick and returns the ms played of the current item (frames read since the last item boundary × 20).
- [x] **5.3.4** — Unit tests: "Underrun" (empty queue → 3840 zero bytes); never `b""` across many reads; overflow drops oldest; flush empties and returns played ms.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_playback.py` passes.
- [x] **Requirements:**
    - [x] "Playback AudioSource returns a silent 3840-byte frame on underrun, never b""; bounded queue (5 s) drop-oldest; flush on SpeechStarted within one tick."
- [x] **Verify:** In a one-liner, call `read()` 1000 times on an empty source and confirm every result is 3840 bytes.

---

### Task 5.4: Discord bot and conversation

Run the plugin's own Discord bot, join a voice channel on command, and implement the Discord `Conversation` with mixing, paced silence, pacing, and rejoin.

**Files:**

```
hermes_duplex/transports/discord/bot.py           (create)
hermes_duplex/service.py                          (modify)
tests/unit/test_discord_conversation.py           (create)
tests/unit/test_discord_bot.py                    (create)
```

**Sub-tasks:**

- [x] **5.4.1** — `bot.py`: `discord.Client` with intents for guilds, voice states, and message content; token from the env var named in config (never Hermes's bot token). App commands `/duplex join` (joins the invoker's current voice channel) and `/duplex leave`, plus a plain text message "join". Only people on the allowlist can invoke; others get an ephemeral refusal.
- [x] **5.4.2** — `DiscordConversation`: start the receiver from Task 5.2; every 20 ms tick, mix allowlisted frames (Task 5.1), resample to the provider input rate, and yield `InputFrame`s with `voiced_speakers` and `t_ms`. When no packets arrive, feed paced silence frames at real-time pace so the provider's server VAD still sees silence (reference: hermes-talk `talk_discord.py:1197-1250` @e5ecfa9).
- [x] **5.4.3** — Output: provider audio → `OutputPacer` (lead ≤ 200 ms by default, for every provider) → resample to 48 kHz stereo → `DuplexAudioSource.feed`. `flush_playback()` flushes the source and drops the pacer backlog and returns ms played. `notify()` is a no-op.
- [x] **5.4.4** — Rejoin: on voice disconnect or channel move, try to rejoin up to 3 times with backoff while keeping the provider session; after 3 failures, speak an end message if possible and close the conversation.
- [x] **5.4.5** — Service wiring: when `transports.discord.enabled`, `build_service` starts the bot as a `Transport`; `stop()` disconnects it within the 5 s budget.
- [x] **5.4.6** — Unit tests with fakes for the voice client and receiver: allowlist check on join/leave; tick produces `InputFrame`s at 20 ms with correct `t_ms`; paced silence when idle; pacer lead bound on output; flush drops backlog; rejoin up to 3 times then end message; `stop()` disconnects.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_discord_conversation.py tests/unit/test_discord_bot.py` passes; full default tier still passes.
- [x] **Requirements:**
    - [x] "Bot token from env; `/duplex join` (or text "join") joins the invoker's voice channel; `/duplex leave`; only people on the allowlist can invoke."
    - [x] "20 ms mix tick; paced silence frames when no packets; resample to provider input rate with soxr streaming".
    - [x] "Output audio passes through audio/pacer.py (lead ≤ 200 ms by default) for every provider, so a flush discards at most the lead plus held backlog."
    - [x] "Reconnect: on voice disconnect/move, attempt rejoin up to 3 times with backoff, keeping the provider session; then end with a spoken message."
- [x] **Verify:** Manual (optional, needs a bot token): `uv run hermes-duplex serve` with Discord enabled logs a successful login and registers `/duplex`. The full manual check is in Task 7.2's live checklist.

---

## Phase 6: Realtime adapter (Milestone 5)

### Task 6.1: Realtime adapter, OpenAI dialect

Implement the Realtime-family provider over `wire.py` for OpenAI Realtime GA.

**Files:**

```
hermes_duplex/providers/realtime.py                   (create)
hermes_duplex/providers/registry.py                   (modify)
tests/fixtures/provider_scripts/realtime_*.jsonl      (create)
tests/contract/test_realtime_adapter.py               (create)
```

**Protocol facts (openai 3.42.1 `types/realtime/*`, OpenAI realtime guides):**

- Connect: `wss://api.openai.com/v1/realtime?model=<model, e.g. gpt-realtime-2.1>`, header `Authorization: Bearer $OPENAI_API_KEY`. Server sends `session.created`.
- Configure: `{"type": "session.update", "session": {"type": "realtime", "instructions": "...", "output_modalities": ["audio"], "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": {"type": "server_vad"|"semantic_vad", ..., "interrupt_response": true}, "transcription": {"model": "<transcribe model>"}}, "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "<voice>"}}, "tools": [<delegate>, <task_status>, <cancel_task>], "tool_choice": "auto"}}`. Function tools are flat: `{"type": "function", "name", "description", "parameters": <JSON schema>}`.
- Audio in: `{"type": "input_audio_buffer.append", "audio": "<base64 PCM16>"}`.
- Server events: `input_audio_buffer.speech_started` (`audio_start_ms`, `item_id`), `input_audio_buffer.speech_stopped`, `response.created`, `response.output_audio.delta` (`delta`, `item_id`, `response_id`), `response.output_audio_transcript.delta` (`delta`, `item_id`), `conversation.item.input_audio_transcription.delta` / `.completed`, `response.function_call_arguments.done` (`name`, `call_id`, `arguments` JSON string, `item_id`, `response_id`), `response.done`, `error`.
- Function output: `{"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "...", "output": "<json string>"}}` then `{"type": "response.create"}`.
- Truncate: `{"type": "conversation.item.truncate", "item_id": "...", "content_index": 0, "audio_end_ms": <ms played>}`. Cancel: `{"type": "response.cancel"}`.
- Out-of-band announcement: `{"type": "response.create", "response": {"conversation": "none", "tools": [], "output_modalities": ["audio"], "instructions": "...", "input": [<message items with the framed result>]}}`. Fallback when out-of-band is unsupported: `conversation.item.create` a system message framed as data, `response.create` with `tool_choice: "none"`, then `conversation.item.delete` of that item after `response.done`. Sessions are capped at 60 minutes.

**Sub-tasks:**

- [x] **6.1.1** — Capabilities (openai dialect): 24 kHz in/out, supports_truncate true, supports_out_of_band_response true, emits_speech_events true, delegation_style `"tool"`, max_session_s 3600. `connect()` sends `session.update` with turn detection from config (`server_vad` or `semantic_vad`), `interrupt_response: true`, and the three tools: `delegate(task: string, profile_hint?: string)`, `task_status(task_id?: integer)`, `cancel_task(task_id?: integer)`.
- [x] **6.1.2** — Map events: speech started/stopped → `SpeechStarted`/`SpeechStopped`; output audio delta → `OutputAudio(pcm, item_id)`; `response.done` → `OutputDone`; transcripts → `InputTranscript`/`OutputTranscript`; `delegate` call → `DelegationRequested(id=call_id, offset_ms=<t_ms at arrival>, target="tool", tool_args)`; `task_status`/`cancel_task` calls → `DelegationRequested` with the tool name in `tool_args` so the session handles them locally; `error` → `ProviderError`; close → `Closed`.
- [x] **6.1.3** — The `delegate` receipt: the adapter exposes `send_function_output(call_id, payload)`; the session sends `{"status":"started","task_id":N}` within 50 ms of receiving the call, before routing completes.
- [x] **6.1.4** — `truncate(item_id, audio_end_ms)` sends `conversation.item.truncate`. `deliver_result` and `say` send an out-of-band `response.create` (conversation none, tools []) when supported, else the system-item fallback with `tool_choice: "none"` and the later `conversation.item.delete`.
- [x] **6.1.5** — Register `openai-realtime`.
- [x] **6.1.6** — Contract tests over `fake_ws_server`: `session.update` payload (pcm 24 kHz, turn detection, interrupt_response, three tools); event mapping; "Realtime delegate tool" (function output with task_id < 50 ms after the call); truncate payload; announcement in both modes (out-of-band and fallback incl. delete).

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_realtime_adapter.py` passes.
- [x] **Requirements:**
    - [x] "session.update: pcm 24 kHz, turn_detection from config (openai: server_vad|semantic_vad; …), interrupt_response true, tools [delegate, task_status, cancel_task]."
    - [x] "`delegate(task, profile_hint?)` returns `{"status":"started","task_id":N}` within 50 ms of receiving the call (before routing completes)."
    - [x] "Announcements: out-of-band `response.create` with `conversation:"none"`, tools [] when supported; else system item framed as data + response.create(tool_choice none) + conversation.item.delete. Contract test for both."
    - [x] "Realtime family sets emits_speech_events=true."
- [x] **Verify:** Print the measured receipt latency in the delegate test; it is under 50 ms.

---

### Task 6.2: Tool-style session behavior: announcement queue, Realtime barge-in, and local tools

Add the session behavior the Realtime family needs: speak results only when the floor is free, flush and truncate on interruption, suppress stale function calls, and answer `task_status`/`cancel_task` locally.

**Files:**

```
hermes_duplex/session.py                  (modify)
tests/contract/test_realtime_session.py   (create)
```

**Sub-tasks:**

- [ ] **6.2.1** — Announcement queue (one queue per session): a result announcement waits until the model is idle (`OutputDone` after the last response), playback has drained, and no `SpeechStarted` is open; wait at most 20 s, then speak anyway. Announcement turns have tools disabled. Reference: hermes-talk `talk_cli.py:742-815` (pump) and `:178-218` (data framing) @e5ecfa9.
- [ ] **6.2.2** — On `SpeechStarted`: `flush_playback()` within one tick, drop the pacer backlog; if `supports_truncate`, send `truncate(item_id, audio_end_ms = ms actually played)`; otherwise cancel only. Suppress function calls that belong to the interrupted response (reference: hermes-live-voice `openai-realtime.adapter.ts:403-418` @f896178).
- [ ] **6.2.3** — `delegate`: send the receipt at once (Task 6.1.3), then route with `hint = tool_args.profile_hint`. `task_status` and `cancel_task` calls answer from the `TaskTracker` as function outputs.
- [ ] **6.2.4** — Contract tests (Realtime adapter over `fake_ws_server` + session): "Barge-in" (`SpeechStarted` after 800 ms played → flush; `truncate(item, 800±20)`); function call from the interrupted response is not executed; announcement waits for `SpeechStopped` + idle + drain; announcement after the 20 s cap (injected clock); "Control intent" through `cancel_task` and through `_control` routing ("cancel that" → `/stop` + spoken confirmation); profile hint reaches `RouteRequest.hint`.

**Validation:**

- [ ] **Build:** Build gate exits 0.
- [ ] **Tests:** `uv run pytest tests/contract/test_realtime_session.py` passes.
- [ ] **Requirements:**
    - [ ] "On SpeechStarted: transport flushes playback; if supports_truncate, send conversation.item.truncate with audio_end_ms = ms actually played; suppress function calls from the interrupted response."
    - [ ] Edge case "Announcement while user speaking → Wait for SpeechStopped + model idle + playback drained, max 20 s, then speak anyway".
    - [ ] SHOULD "Pass an optional `profile_hint` from the Realtime `delegate` tool into RouteRequest".
    - [ ] "announcement turns have tools disabled. Unit test asserts template and tool-disable."
- [ ] **Verify:** In the barge-in test, print the `audio_end_ms` sent; it is within 800 ± 20.

---

### Task 6.3: xAI Grok dialect

Add the `xai` dialect to the Realtime adapter.

**Files:**

```
hermes_duplex/providers/realtime.py           (modify)
hermes_duplex/providers/registry.py           (modify)
tests/contract/test_realtime_adapter.py       (modify)
```

**Protocol facts (xAI docs `docs.x.ai/developers/model-capabilities/audio/speech-to-speech`, hermes-talk `talk_grok_realtime.py` @e5ecfa9):**

- Connect: `wss://api.x.ai/v1/realtime?model=grok-voice-latest` (alias of `grok-voice-3.0`), header `Authorization: Bearer $XAI_API_KEY`.
- `session.update` flat shape: `{"type": "session.update", "session": {"voice": "<voice>", "instructions": "...", "turn_detection": {"type": "server_vad"}, "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}, "output": {"format": {"type": "audio/pcm", "rate": 24000}}}, "tools": [...]}}`. Only `server_vad` (or null) is supported.
- Voice ids on the wire carry the `xai_` prefix (`xai_ara`); config uses the friendly name and the adapter adds the prefix unless it is already there.
- Server events use the OpenAI GA names. Application `{"type": "ping"}` events and unknown fields arrive and must be ignored; `response.status_details` can be the string `"unimplemented"`.
- `conversation.item.truncate` was not proven live; the dialect sets truncate capability false (cancel-only barge-in).

**Sub-tasks:**

- [x] **6.3.1** — Dialect switch in `realtime.py`: base URL `https://api.x.ai/v1` (as `wss://api.x.ai/v1/realtime`), flat session fields, `xai_` voice prefix, ignore ping and unknown events, `supports_truncate` false. Validate `server_vad` only (config already rejects others; keep a guard).
- [x] **6.3.2** — Register `xai-realtime` (dialect `xai`, key env from config).
- [x] **6.3.3** — Contract tests: "Grok dialect" (flat `session.update` shape, voice `xai_` prefix, no truncate sent on `SpeechStarted`, ping ignored, `"unimplemented"` status details tolerated).

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/contract/test_realtime_adapter.py` passes, including the Grok cases.
- [x] **Requirements:**
    - [x] "xai dialect: base_url https://api.x.ai/v1, flat session fields, `xai_` voice prefix, ignore ping/unknown events, truncate capability false."
    - [x] Edge case "Grok truncate unsupported → Capability false → cancel-only".
    - [x] Success criterion "provider swap to Grok via config" (research-decisions.md).
- [x] **Verify:** Change only `provider.name` to `xai-realtime` in a test config and confirm `build_service` constructs the xai dialect without other changes.

---

### Task 6.4: `doctor` command

Add a diagnostics command that checks config, secrets, Hermes reachability, provider auth, and Discord login.

**Files:**

```
hermes_duplex/cli.py                  (modify)
hermes_duplex/plugin.py               (modify)
tests/unit/test_doctor.py             (create)
```

**Sub-tasks:**

- [x] **6.4.1** — `hermes-duplex doctor [--config PATH]` and `hermes duplex doctor`: check config validity; every named env var present; each profile's API server reachable with its key (`GET {api_base}/p/<p>/v1/models` → 200); provider key auth with a cheap call (OpenAI: `GET https://api.openai.com/v1/models`; xAI: `GET https://api.x.ai/v1/models`); Discord token login (`discord.Client.login` then close) when Discord is enabled.
- [x] **6.4.2** — Print a pass/fail table; exit 0 when all pass, 1 otherwise. Never print secret values.
- [x] **6.4.3** — Unit tests with the fake Hermes API and stubbed HTTP/Discord: all pass; missing env var; wrong profile key (401); unreachable Hermes; secrets absent from output.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest tests/unit/test_doctor.py` passes.
- [x] **Requirements:**
    - [x] SHOULD "Checks config validity, env vars present, API server reachable per profile (GET /p/<p>/v1/models), provider key auth (cheap call), Discord token login, prints pass/fail table."
    - [x] Edge case "Profile key env missing at runtime → `doctor` reports it".
- [x] **Verify:** Run `uv run hermes-duplex doctor --config duplex.example.yaml` with no env vars set; it prints failed rows and exits 1.

---

## Phase 7: Docs and live checks (Milestone 6)

### Task 7.1: Live end-to-end test (opt-in)

Add the opt-in live tier that uses a real Hermes, real GPT-Live, and the Decisions router.

**Files:**

```
tests/live/__init__.py                    (create)
tests/live/conftest.py                    (create)
tests/live/test_live_gpt_live.py          (create)
tests/fixtures/audio/*.wav                (create)
```

**Sub-tasks:**

- [ ] **7.1.1** — Generate the WAV fixtures once with a TTS model (24 kHz mono PCM16): "How is my portfolio doing?", "And what about Tesla?", plus a short silence file. Commit them. If no TTS key is available in the coder session, stop and report it as blocked for the user.
- [ ] **7.1.2** — `conftest.py`: mark `live`; skip unless `OPENAI_API_KEY` is set and `HERMES_DUPLEX_LIVE=1`. Reuse the Task 2.5 Hermes fixture, but with the profiles' real model config taken from env (document the variables in the test docstring).
- [ ] **7.1.3** — Test: provider `gpt-live`, router `decisions` with knn fast path; stream the WAVs through the web WS at real-time pace with silence between; assert the routed profile is stock-picker and a non-empty spoken announcement (output transcript after the commentary) arrives within 60 s.

**Validation:**

- [ ] **Build:** Build gate exits 0.
- [ ] **Tests:** `uv run pytest -m live` skips with the stated reason when keys are absent; `uv run pytest -m "not live and not e2e"` still passes.
- [ ] **Requirements:**
    - [ ] SHOULD "Provide a live end-to-end test (marker `live`), skipped unless OPENAI_API_KEY and HERMES_DUPLEX_LIVE=1." and "Real Hermes …, real GPT-Live, decisions router; streams committed WAV fixtures … at real-time pace; asserts routing to stock-picker and a non-empty spoken announcement transcript within 60 s."
- [ ] **Verify:** `uv run pytest -m live -rs` prints the skip reason naming both variables.

---

### Task 7.2: README, architecture doc, contributor entry point, and live checklist

Document install, setup, configuration, usage, and testing, and write the manual checklist.

**Files:**

```
README.md                         (modify)
docs/system/architecture.md       (modify)
docs/live-checklist.md            (create)
AGENTS.md                         (create)
CLAUDE.md                         (create)
```

**Sub-tasks:**

- [x] **7.2.1** — `README.md`: what it is; install (`hermes plugins install <repo URL>`, standalone `uv sync` + `hermes-duplex serve`); Discord bot setup (create the second bot, intents, invite scopes `bot` + `applications.commands`, voice permissions); Hermes API server setup (`API_SERVER_ENABLED`, per-profile `API_SERVER_KEY`); `duplex.yaml` walkthrough incl. people and permissions and the model table; usage on Discord and web; provider switching (`provider.name`); testing tiers and their commands; the CI e2e status (record "local only" if Task 2.5.5 found Hermes not installable in CI).
- [x] **7.2.2** — `docs/system/architecture.md`: layers (transport, provider, router, dispatcher) around VoiceSession and TaskTracker; the GPT-Live and Realtime hand-off flows; barge-in for both families; router policy; time base and attribution; the lifecycle seam (keep Task 2.4's section).
- [x] **7.2.3** — `docs/live-checklist.md`: manual checks for Discord DAVE receive, barge-in (Realtime and GPT-Live), gate threshold tuning, multi-speaker attribution, allowlist removal, rejoin after channel move, Grok switch, and a 60-minute call (session expiry and reconnect). Each item has steps and an expected result.
- [x] **7.2.4** — `AGENTS.md`: layout, commands (the standard validation commands, `-m e2e`, `-m live`, eval-router), and rules (no Hermes internals, no `openai` dependency, deps must contain Hermes pins, session branches on capabilities only, secrets only via env). `CLAUDE.md`: a one-line pointer that imports `AGENTS.md`.

**Validation:**

- [x] **Build:** Build gate exits 0.
- [x] **Tests:** `uv run pytest -m "not live and not e2e"` passes (no code change expected).
- [x] **Requirements:**
    - [x] "README.md — install, Discord bot setup, config, usage, provider switching, testing tiers".
    - [x] "docs/system/architecture.md — how it works (layers, flows)"; "docs/live-checklist.md — manual Discord/GPT-Live/Grok checklist"; "AGENTS.md (+ CLAUDE.md pointer) — contributor entry point: layout, commands, rules".
    - [x] Manual tier items: "Discord DAVE receive, barge-in, multi-speaker, allowlist removal, Grok switch, 60-min call".
- [x] **Verify:** Every command in README and AGENTS.md runs as written (copy-paste each one that does not need secrets).

<!-- END OF SPEC -->
