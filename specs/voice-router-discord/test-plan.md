# Test Plan: voice-router-discord (hermes-duplex)

> **Source:** `.sdlc/specs/voice-router-discord/master-plan.md`
> **Created:** 2026-11-03
> **Scope:** Acceptance-level, black-box behaviors. Each item says what a person or an integration test observes from outside the plugin. The detailed `test-spec.md` comes later.

## Tiers

| Tier | Command | Runs where | What it needs |
|---|---|---|---|
| Default (unit, contract, in-process e2e) | `uv run pytest -m "not live and not e2e"` | Every CI run and locally | Nothing external |
| Real-Hermes e2e | `uv run pytest -m e2e` | CI `e2e` job; locally when `hermes` is installed (else skipped with a reason) | hermes-agent @`6112c2adb9e11f69734a29be9d2696f345ffa06a`, fake LLM, fake provider |
| Live e2e (opt-in) | `HERMES_DUPLEX_LIVE=1 uv run pytest -m live` | User machine only | `OPENAI_API_KEY`, real models for Hermes profiles |
| Router eval | `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` | Locally; knn offline, decisions/jev with keys | model2vec model; API keys for main-path backends |
| Static checks | `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pyright` | Every CI run | — |
| Dependency compatibility | CI `deps-compat` job | Every CI run | hermes-agent @6112c2a `pyproject.toml` |
| Manual | `docs/live-checklist.md` | User, with a Discord server | Discord bot token, provider keys |

## Acceptance Items

Each ID is referenced from the tier tables below. "Req" names the master-plan Requirements heading it proves.

### A. Plugin packaging and lifecycle

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| A1 | Validate the plugin with real Hermes | `hermes plugins validate <repo>` exits 0 | Ship as a Hermes plugin |
| A2 | Install and enable the plugin in a fresh `HERMES_HOME`, start `hermes gateway run` | Gateway starts; plugin loads; the duplex web page answers on its port; Hermes's own platforms still work | Ship as a Hermes plugin; Run the same DuplexService in-gateway and standalone |
| A3 | Resolve dependencies against Hermes @6112c2a pins | Resolves without conflict; `openai` absent from the tree | Ship as a Hermes plugin |
| A4 | The package imports no Hermes internals | Grep test passes; no `gateway`/`hermes_cli` imports under `hermes_duplex/` | Ship as a Hermes plugin |
| A5 | Run `hermes-duplex serve` standalone, then stop it with Ctrl-C | Service starts with the same behavior as in-gateway; exits within 5 s | Run the same DuplexService … |
| A6 | Stop the service while a task runs, a provider socket is open, and Discord is connected | All tasks cancelled, sockets closed, Discord disconnected, web server closed within 5 s | Run the same DuplexService … |
| A7 | Hermes `/duplex` command | Shows active conversations, running tasks, and provider; offers no join | Ship as a Hermes plugin (Scope: plugin.py) |
| A8 | Gateway with no `duplex.yaml` | One warning; gateway keeps running; duplex service not started | Load config … fails closed |

### B. Configuration and secrets

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| B1 | Start with an unknown provider, unknown router backend, a person naming an undefined profile, or a missing secret env var | Startup error that names the offending key; nothing starts | Load config from `duplex.yaml` with validation that fails closed |
| B2 | Put a literal token or key in config instead of an `*_env` name | Rejected at startup | Load config … |
| B3 | Config path precedence | `--config` beats `HERMES_DUPLEX_CONFIG`, which beats `$HERMES_HOME/duplex.yaml` | Load config … |
| B4 | `doctor` with a valid setup / with a missing env var, wrong profile key, unreachable Hermes | Pass/fail table; exit 0 only when all pass; no secret values printed | Provide `hermes duplex doctor` (SHOULD) |

### C. Routing

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| C1 | "how's my portfolio doing" | Routed to `stock-picker`; run created on `/p/stock-picker/v1/runs` | Implement the router … |
| C2 | Then "and what about Tesla" within 10 min | Same profile; same `session_id` `duplex-<person>-stock-picker`; decision logged `sticky=true` | Implement the router …; Dispatch tasks … |
| C3 | Then "what's the weather tomorrow" | Routed to `general` | Implement the router … |
| C4 | Main backend slower than the deadline (stubbed 1 s) | knn result used; `source=knn`; routing latency < 350 ms | Implement the router … |
| C5 | Main backend fails 3 times in a row | Main path skipped for 30 s; decisions logged with fallback source | Implement the router … |
| C6 | All backends fail | Active profile, else `general`; effort `medium`; `source=fallback` | Implement the router … |
| C7 | stock-picker routed at effort `high` | Request carries the table's provider and model and `model_options.reasoning.effort = high`; a missing cell uses `default_model`, else no model | Implement the router …; Dispatch tasks … |
| C8 | Every routing decision | One log line with speaker, profile, effort, model, confidence, top-3 probabilities, source, latency_ms, sticky | Implement the router … |
| C9 | Realtime `delegate` with `profile_hint`, main path times out, knn not confident | Hint profile used; `source=hint` | SHOULD: profile_hint |
| C10 | `eval-router` on the fixture file (`--json` and table) | Accuracy, macro-F1, confusion matrix, effort exact and off-by-one, p50/p95/p99, deadline-miss rate per backend and composite; exit 0 | Provide `hermes duplex eval-router` |

### D. Dispatch and task tracking

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| D1 | Hermes answers `session_busy` (409) then 202 | Same `session_id` and same `Idempotency-Key` resent after 1 s; task runs | Dispatch tasks via the Hermes API server |
| D2 | Hermes answers `admission_queued` (503) | No resend; task shows `queued`; moves to `running` when a run id is available to poll | Dispatch tasks … |
| D3 | Hermes answers `runtime_draining` / `storage_unavailable` (503 + Retry-After) | Same request and key retried per Retry-After, at most 3 times; then task failed with a spoken reason | Dispatch tasks … |
| D4 | Hermes answers `idempotency_key_conflict` or another 4xx code | Task failed; short spoken reason; error logged with the code | Dispatch tasks … |
| D5 | Hermes not running | Task failed; voice says "Hermes is unreachable"; next task works once Hermes is back | Dispatch tasks … |
| D6 | Second task for the same person and profile while the first runs | Voice says "queued behind task N"; both complete in order | Dispatch tasks … |
| D7 | Tasks for different profiles at the same time | Run concurrently | Dispatch tasks … |
| D8 | SSE stream drops mid-run | Re-subscribe once; else poll every 5 s; task `unknown` after 3 failed polls | Dispatch tasks … (Error Handling) |
| D9 | "what are you working on?" | Spoken summary of that speaker's running tasks; nothing sent to Hermes | Track tasks and support status and cancel |
| D10 | "cancel that" / "cancel task 2" | Most recent (or named) task of that speaker stopped via `/stop`; spoken confirmation | Track tasks and support status and cancel |
| D11 | Task history file | `$HERMES_HOME/duplex/tasks.jsonl` holds every transition; the "started" record exists before the voice announces it | Track tasks … |

### E. People, permissions, and attribution

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| E1 | alex (general only) asks about the portfolio | Routed to stock-picker; no run; spoken refusal names the profile; logged | Enforce people and permissions |
| E2 | Non-allowlisted Discord user talks | Their audio never reaches the provider | Enforce people and permissions |
| E3 | Web `/ws` without a token or with a wrong token (query or subprotocol) | HTTP 401 before upgrade | Enforce people and permissions |
| E4 | Delegation with no voiced speaker in the window | No dispatch; voice asks "who's asking?" | Enforce people and permissions |
| E5 | Two people talk; one asks for a task | Task credited to the person with the most voiced ticks in the window (ties → most recent) | Enforce people and permissions |
| E6 | Delegation after a provider reconnect | Credited on the new session's timeline; old attribution cleared | Enforce people and permissions |

### F. Voice behavior (provider-independent)

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| F1 | Result spoken back | Text framed as a report from the `<profile>` agent, not instructions; announcement turn has tools disabled | Frame Hermes results as data |
| F2 | Very long Hermes result | Truncated to the cap with "…(more in Hermes)" | Frame Hermes results as data |
| F3 | Provider socket closes mid-task | Reconnect (≤3), re-seeded with the last 10 transcript lines and running tasks; result announced on the new session | Handle provider session expiry and disconnects |
| F4 | Provider reconnect fails 3 times | Conversation ends with a spoken message | Handle provider session expiry and disconnects |
| F5 | Switch provider by changing only `provider.name` | Same flows work; no code change | Define a provider interface … |

### G. GPT-Live

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| G1 | Session start | First client event is `session.start` with prompts.py instructions, configured voice, `delegation: {type: "client"}` | Implement the GPT-Live adapter |
| G2 | User asks for work; GPT-Live sends `session.delegation.created` | Task text equals the user's request from the transcript window; progress as `session.thinking.append` (≤1 per 5 s); result as `session.commentary.append` with the delegation id | Implement the GPT-Live adapter |
| G3 | Two requests back to back (<1 s apart) or overlapping speech | Two tasks with separate, correct texts | Implement the GPT-Live adapter |
| G4 | User talks over GPT-Live | Playback flushed and held backlog dropped within one 20 ms tick; no truncate sent; later output plays | Implement the GPT-Live adapter |
| G5 | Delegation target logged | Log shows `target=client` | Implement the GPT-Live adapter |
| G6 | Hermes task fails | Spoken short apology with the reason | Implement the GPT-Live adapter |
| G7 | Pre-warm flag on (MAY; default off) | Routing starts from transcript deltas; same decision as without the flag | MAY: pre-warm routing |

### H. Realtime family (OpenAI Realtime and Grok)

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| H1 | `session.update` (openai) | pcm 24 kHz, configured turn detection, `interrupt_response: true`, tools delegate/task_status/cancel_task | Implement the Realtime adapter … |
| H2 | Model calls `delegate` | Function output `{"status":"started","task_id":N}` within 50 ms; model keeps talking | Implement the Realtime adapter … |
| H3 | Task finishes while the user talks | Announcement waits for speech stopped + model idle + playback drained (max 20 s), then speaks via out-of-band response with tools disabled | Implement the Realtime adapter … |
| H4 | Provider without out-of-band support | System item framed as data + `response.create` with `tool_choice: none` + item delete | Implement the Realtime adapter … |
| H5 | User interrupts after 800 ms of playback | Playback flushed within one tick; `conversation.item.truncate` with `audio_end_ms` 800 ± 20; function calls from the interrupted response not executed | Implement the Realtime adapter … |
| H6 | `provider.name: xai-realtime` | Flat `session.update`, `xai_`-prefixed voice, ping ignored, no truncate sent (cancel-only) | Implement the Realtime adapter … |
| H7 | `task_status` / `cancel_task` tool calls | Answered locally from the task tracker | Implement the Realtime adapter … |

### I. Discord transport

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| I1 | Allowlisted person runs `/duplex join` (or types "join") | Bot joins their voice channel; Hermes's own Discord bot unaffected | Implement the Discord transport with its own bot |
| I2 | Non-allowlisted person runs `/duplex join` | Refused | Implement the Discord transport … |
| I3 | DAVE-encrypted speech from an allowlisted person | Decrypted, decoded, and heard by the provider (no white noise) | Implement the Discord transport … |
| I4 | Nobody talks | Paced silence keeps provider VAD working; no stall | Implement the Discord transport … |
| I5 | Playback queue empty | Bot sends silent frames; player thread keeps running | Implement the Discord transport … |
| I6 | Bot moved or disconnected | Rejoins up to 3 times with the provider session kept; then a spoken end message | Implement the Discord transport … |
| I7 | Rejoin where Discord does not resend SPEAKING | Sole allowlisted member's audio still attributed; with two or more members, unmapped audio dropped | Implement the Discord transport … |
| I8 | discord.py internals guard | Test fails if a hooked private attribute disappears on the pinned version | Implement the Discord transport … |

### J. Web transport

| ID | Behavior | Expected result | Req |
|---|---|---|---|
| J1 | Open the page, enter a token, connect | Token kept in sessionStorage; mic audio streams; responses play | Implement the web transport |
| J2 | User interrupts | Browser playback buffer flushed on `{"type":"interrupt"}` | Implement the web transport |
| J3 | Live transcript and task list | Shown and updated from `{"type":"status",...}` frames | Implement the web transport |
| J4 | Default bind | `127.0.0.1:8765`; host/port configurable | Implement the web transport |

## Tier Coverage

### In-process e2e (default tier, `tests/e2e/test_web_fake_provider.py`)

Web WS → session (fake provider) → knn router → fake Hermes API. Covers C1, C2, D11, E1, E3, F1.

### Real-Hermes e2e (`-m e2e`, `tests/e2e/test_real_hermes.py`)

Real gateway with the plugin installed, API server with per-profile keys, profiles `general` and `stock-picker` backed by `tests/fakes/fake_llm.py`, fake provider scripted to delegate "how is my portfolio doing" then "and what about Tesla", knn router with fixture phrases.

- [ ] A1 — `hermes plugins validate` exits 0.
- [ ] A2 — Plugin installs from the repo path; gateway starts the service in-gateway.
- [ ] C1 — Run created on `/p/stock-picker/...`.
- [ ] C2 — Second task reuses `duplex-rob-stock-picker` and is logged sticky.
- [ ] C7 — `GET /p/stock-picker/v1/runs/{id}` reports `runtime.requested` equal to the table's provider and model; requested effort asserted at the Hermes boundary that carries it; fake LLM received the model name.
- [ ] F1 — Fake provider recorded the announced result in the report template.
- [ ] E1 — alex gets a refusal; no run.
- [ ] Teardown — gateway stopped; temp `HERMES_HOME` removed.

### Contract tier (default)

- HermesApiClient vs aiohttp fake: D1–D5, D8, C7 request shape.
- GPT-Live adapter vs recorded scripts on a fake WS server: G1–G6.
- Realtime adapter (openai and xai) vs recorded scripts: H1–H7.
- Decisions and Jev backends vs aiohttp fakes: request and response mapping for C4–C6.

### Live e2e (`-m live`, opt-in)

- [ ] Skipped with a reason unless `OPENAI_API_KEY` is set and `HERMES_DUPLEX_LIVE=1`.
- [ ] Real Hermes with real profile models, real GPT-Live, Decisions router.
- [ ] Committed WAV fixtures streamed through the web WS at real-time pace.
- [ ] Routed to `stock-picker` (C1 live).
- [ ] Non-empty spoken announcement transcript within 60 s (G2 live).

### Router eval (opt-in for live backends)

- [ ] C10 with `--backend knn` offline.
- [ ] With keys: `--backend decisions`, `--backend jev`, `--backend composite`; record deadline-miss rate for Decisions (plan Risk: no published latency).

## Manual Discord Checklist (`docs/live-checklist.md`)

Run with a real Discord server, the duplex bot invited, and provider keys set.

- [ ] **DAVE receive (I3):** Join with `/duplex join`; speak; the model answers what you said. No hallucinated conversation from noise.
- [ ] **Barge-in, Realtime (H5):** With `openai-realtime`, talk over a long answer. Audio stops within about 200 ms; the model does not repeat the part you did not hear.
- [ ] **Barge-in, GPT-Live (G4):** With `gpt-live`, talk over a long answer. Audio stops within about 200 ms. Tune the gate threshold so background noise does not cut the model off; record the chosen value.
- [ ] **Async delegation (G2/H2/H3):** Ask for a slow task; keep chatting about something else; the result arrives later without talking over you.
- [ ] **Multi-speaker (E5):** Two allowlisted people talk; tasks are credited to the person who asked; permissions apply per person (E1).
- [ ] **Allowlist removal (E2):** Remove a person from `people`, restart; their speech is ignored and `/duplex join` refuses them (I2).
- [ ] **Rejoin (I6/I7):** Move the bot to another channel and back; conversation continues; audio is attributed after rejoin.
- [ ] **Grok switch (H6/F5):** Change only `provider.name` to `xai-realtime`; conversation, delegation, and announcements work; interruptions cancel without truncate.
- [ ] **60-minute call (F3):** Keep a call open past 60 minutes; the provider session reconnects and re-seeds; a task started before the reconnect is announced after it.
- [ ] **Web parity (J1–J3):** Repeat delegation, refusal, and interruption from the web page.
