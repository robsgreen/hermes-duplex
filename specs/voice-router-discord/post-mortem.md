# Post-Mortem: voice-router-discord (hermes-duplex)

Date: 2026-11-03
Branch: sdlc/voice-router-discord (worktree /home/user/hermes-duplex-worktrees/voice-router-discord)
Diff: 0fd20c6...c339ac5, 30 commits, 135 files, +24567/-1

## Summary

We built the hermes-duplex plugin as planned: four layers (transports, providers, router, dispatcher) around `VoiceSession` and `TaskTracker`. It ships the web and Discord transports, the GPT-Live and Realtime (OpenAI and xAI) adapters, the knn, Decisions, and Jev router backends with the composite policy, the Hermes API client, `serve`, `eval-router`, and `doctor`, plus CI with deps-compat and real-Hermes e2e jobs. 159 of 161 sub-tasks are done. 4.3.6 (a MAY item) is deferred, and 7.1.1 (spoken WAV fixtures) is waiting on a user key. Final checks are green: 667 default tests three runs in a row, and 4 real-Hermes e2e tests.

The plan held up structurally. Most drift is interface amendments recorded mid-implementation (master-plan.md "Interface amendments during implementation"). Two departures need a user decision. First, service stop leaves Hermes runs running, which contradicts a MUST. Second, the delegate receipt no longer carries a task number. Review fixed both high findings (P1, P2) in one cycle. Three medium issues remain open: P7, P25, and P27.

Overall: significant drift. It is mostly deliberate and documented, with two items open for the PR gate.

## Planned vs. Built

| Planned outcome | Evidence in source |
|---|---|
| Plugin installs and validates under hermes-agent @6112c2a | `plugin.yaml`; `tests/e2e/real_hermes.py:151-152` runs `hermes plugins validate`; `.github/workflows/ci.yml:72-99` e2e job |
| Deps fit core pins, no `openai` | `pyproject.toml:14-22`; `ci.yml:32-70` deps-compat with a "Fail if openai resolves" step |
| Gateway lifecycle seam | `hermes_duplex/plugin.py:4,65` `ctx.register_platform` (register() runs on a loop-less thread) |
| Normalized provider events + capabilities, session branches only on capabilities | `providers/base.py:12-27,79-195`; `tests/unit/test_session_no_provider_names.py` |
| GPT-Live client delegation, thinking/commentary appends | `providers/gpt_live.py:41,177-181`; `tests/contract/test_gpt_live_session.py` |
| Realtime openai + xai dialects, truncate/out-of-band capability split | `providers/realtime.py:157-158,202-203,379,415-428` |
| Composite router: deadline, fallback, sticky, breaker, model table, decision log | `router/composite.py`; `tests/e2e/test_router_backends_flow.py` (CT-10, CT-11) |
| Hermes API dispatch with error-code mapping and stable `session_id` | `dispatch/hermes_api.py:58-73`; `tests/contract/test_hermes_api_client.py`; CT-3/CT-4 |
| Adapted DAVE receiver, allowlist mixing, paced silence, never-empty playback | `transports/discord/receiver.py`, `audio/mixer.py`, `transports/discord/playback.py`; `tests/unit/test_discord_internals_guard.py` |
| Web transport with token 401 before upgrade | `transports/web/server.py`; CT-8 |
| Real-Hermes e2e (sticky, model, refusal) | `tests/e2e/test_real_hermes.py`, `tests/e2e/test_real_hermes_flows.py` (4 passed) |
| Live tier with committed WAVs | `tests/live/test_live_gpt_live.py:178,191` loads `portfolio.wav`/`tesla.wav`. Only `tests/fixtures/audio/silence.wav` exists |

## Plan Drift

| Area | Planned | Actual | Impact | Evidence |
|---|---|---|---|---|
| `delegate` tool receipt | `{"status":"started","task_id":N}` within 50 ms (master-plan.md:417, test scenario :688) | `{"status":"started"}`. Tasks are numbered after routing and the permission check, and the number is spoken in the start or "queued behind task N" notice | High | `session.py:583`; amendment at master-plan.md:616; `providers/realtime.py:63-64` |
| Service stop | MUST "Service stop cancels all tasks" (master-plan.md:400) | `stop()` cancels voice sessions and stops following runs. The Hermes runs keep going and their results are never spoken | High | `service.py:101-108` docstring; review.md P23 |
| Provider interface | Event set and `RealtimeProvider` per Interfaces (master-plan.md:505-521) | Added the `ResponseStarted` event and `send_function_output(call_id, payload) -> bool`. After P1 it returns bool and defers `response.create` until the calling response is done | Medium | `providers/base.py:94,184,227`; `providers/realtime.py:453-468`; amendment at master-plan.md:614-615 |
| Dispatcher interface | `start_run(...) -> str` (master-plan.md:572) | Returns `RunStart(run_id | None, queued)` and raises `HermesApiError(reason, code)` | Medium | `dispatch/hermes_api.py:58,73`; amendment at master-plan.md:617 |
| Router interface | Session passes `RouteRequest` with `active_profile` (master-plan.md:548) | `CompositeRouter.route(text, speaker, *, history, hint, permitted)` owns the active profile per speaker. The `permitted` predicate (P12) stops a refused profile from becoming sticky. Adds `RouterBackend.close()` and `router/factory.py` | Medium | `router/composite.py:164-190`; `router/factory.py`; amendment at master-plan.md:618 |
| Transport interface and time base | `flush_playback() -> int` ms played. `InputFrame.t_ms` resets per provider session and drives attribution (master-plan.md:450,539) | `flush_playback() -> None`. The session's pacer counts played ms, and `t_ms` is informational and never resets. Attribution and transcripts key on the session's own input-ms counter per provider session. Not listed in the amendments | Medium | `transports/base.py:24-28,59-62`; review.md P6, P9 |
| Unmapped-SSRC inference | "attribute to the only allowlisted member in channel" (master-plan.md:459) | Infers only when the channel holds exactly one non-bot person and that person is allowlisted (P2 fix). Every bot is skipped, so another bot's stream is still credited (P25) | Medium | `transports/discord/receiver.py:398-423`; repro-P2.md |
| Task lifecycle | States routing→queued→running→terminal (master-plan.md:444) | Adds an "unfollowed" set for tasks whose session ended. Those tasks drop out of `active_for`, `ahead_of`, and `cancel` while their Hermes runs continue | Medium | `tasks.py:90,156-181,238`; review.md P11, P30 |
| Result cap | Results truncated to fit 500 tokens for GPT-Live (master-plan.md:413,455) | A character heuristic (`CHARS_PER_TOKEN = 2.5`) applies to all providers, with no tokenizer. Numeric text measured 529-1117 tokens against the 500 cap | Medium | `prompts.py:8-17`; `providers/gpt_live.py:41-43`; review.md P7 |
| Real-Hermes effort assertion | Effort asserted at the Hermes boundary via run status / `requested_runtime` (master-plan.md:480) | No run status field carries effort. The test asserts `reasoning_effort` on the request the fake LLM received | Medium | `tests/e2e/test_real_hermes.py:87-89`; status.md 03:57 and 05:21 |
| Packaging | Upper-bounded deps containing core pins (master-plan.md:321-325) | Also adds a `pynacl>=1.6,<1.7` override copied from Hermes, plus a uv `conflicts` entry between Hermes's and our `dev` groups so workspace installs lock | Medium | `pyproject.toml` `[tool.uv]` |
| Live tier fixtures | Committed TTS WAV fixtures (master-plan.md:370,484) | Only `silence.wav` is committed. A generator script exists, and the live tier skips without the fixtures | Medium | `tests/fixtures/audio/`; `tests/live/make_audio_fixtures.py`; README.md:299-303; task-spec.md:1039 |
| GPT-Live routing pre-warm | MAY: pre-warm from transcript deltas behind a flag (master-plan.md:491) | Not built. Deferred because it needs a side-effect-free router mode | Medium | task-spec.md:699 (4.3.6) |
| Module layout | `doctor` lives in `cli.py` (master-plan.md:363) | Split out to `hermes_duplex/doctor.py`, with test fakes added under `tests/fakes/` | Low | `hermes_duplex/doctor.py`; `tests/fakes/fake_hermes_api.py` |

## Critical Decisions

- **Own Discord bot, all work through the API server.** We added no dependency on Hermes internals, and a grep test enforces it (`tests/unit/test_no_hermes_internals.py`). The cost is a second bot to invite.
- **Protocols over `websockets`, no `openai` SDK.** Both wire protocols were written from openai 3.42.1 types and have only been exercised against recorded scripts (`providers/wire.py`, `tests/fixtures/provider_scripts/*.jsonl`). No real GPT-Live or Realtime session has run.
- **Gateway seam = `ctx.register_platform("duplex")`.** `register()` runs on a loop-less loader thread, so `spawn_task` was ruled out (`plugin.py:4,65`). Note that `plugin_isolation.py:35` refuses `register_platform` under `isolation: host`.
- **Tasks are numbered after routing and permission.** Refusals and `_control` intents burn no numbers. That is why the receipt dropped `task_id` (`session.py:583`).
- **Stopping the service or ending a session stops following runs but does not stop them** (`service.py:104-106`, `tasks.py:238`). The user still has to confirm this (P23/P30).
- **A refused profile never becomes sticky.** `route(..., permitted=)` keeps the rule that the router sees all profiles, and the permission check runs after routing (`router/composite.py:190`).

## Risks and Concerns

- **P25, security residual:** `receiver.py:415-419` skips every bot when counting channel members. A TTS or music bot's unmapped SSRC is credited to the sole allowlisted person and acts with that person's permissions. `tests/unit/test_receiver.py::test_other_bots_do_not_prevent_inference` pins this behavior.
- **P27:** `realtime.py:462-468` sends the deferred `response.create` on any `response.done`, including one cancelled by barge-in. The model can talk over the user, and the floor count goes stale.
- **P7:** a GPT-Live append can exceed 500 tokens for numeric or CJK results, so a result can be dropped silently (`prompts.py:8`, `gpt_live.py:41-43`). The "fits whole" claim in `docs/system/architecture.md` is false.
- **Orphaned Hermes runs:** after stop or session end, runs keep consuming model spend. The user cannot cancel them by voice because unfollowed tasks are excluded from cancel (`tasks.py:169-201`).
- **Unverified live paths:** the DAVE receiver depends on discord.py 2.7.1 private socket attributes (`receiver.py`; guarded by `test_discord_internals_guard.py`). It, GPT-Live, and the Grok dialect quirks listed in status.md 05:08 (voice prefix, flat shape, `item.delete`) have never run against real services.
- **Low leftovers carried to the PR gate:** P26 (double cancel confirmation, `session.py:777-779`), P28, P29, P21, P22, P10, P17, P18, P20 (review.md:46-52, 82-84).

## Learnings

- **Fixtures that replay events in one burst hide ordering bugs.** P1 survived because `_responding()` sent created, call, and done back to back. Provider fixtures should separate the events that real servers separate, such as `function_call_arguments.done` and `response.done`.
- **A chars-per-token constant is a guess, not a bound.** Two reviewers disagreed on P7 until one measured tokens. A cap backed by a hard provider limit needs a test that counts tokens against that limit.
- **Authorization rules copied from upstream need negative cases in the plan.** The "only allowlisted member" wording (master-plan.md:459) carried the P2 bypass into task 5.2. The fix then reopened as P25 because "bot" was underspecified.
- **Parallel module tasks leave wiring gaps.** At one point `build_service` raised `NotImplementedError` for decisions/jev and Discord (status.md 05:00) until an unplanned wiring follow-up landed. Give service wiring its own task.
- **Fake-clock tests must advance the clock only after the waiter has read it.** M1/P13 failed up to 8 of 25 runs for this reason (`tests/contract/test_realtime_session.py:376-394`).
- **Recording interface amendments in the master plan during implementation** (master-plan.md:612-619) made review and this retro tractable. The flush/t_ms change was not recorded there and surfaced only as docstring findings P6 and P9.

## Action Items

- [ ] Decide P23/P30: either stop open Hermes runs at service stop and session end and keep unfollowed tasks cancellable, or amend the MUST at master-plan.md:400 (`service.py:101-131`, `tasks.py:169-201,238`).
- [ ] Fix P25: exclude only our own bot from the sole-member count in `transports/discord/receiver.py:415-419`, and invert `test_other_bots_do_not_prevent_inference`.
- [ ] Fix P27: on a cancelled `response.done`, drop the due reply and release the floor count (`providers/realtime.py:462-468`).
- [ ] Fix P7: give GPT-Live appends a provably safe cap (about 1 char/token, split across appends, or a tokenizer dependency, which needs a plan decision). Correct the claim in `docs/system/architecture.md` (`prompts.py:8-17`, `providers/gpt_live.py:41-43`).
- [ ] Resolve P24/7.1.1: generate `portfolio.wav` and `tesla.wav` with `OPENAI_API_KEY=... uv run python -m tests.live.make_audio_fixtures`, commit them under `tests/fixtures/audio/`, and run `HERMES_DUPLEX_LIVE=1 uv run pytest -m live` once. The tier cannot run until then (`tests/live/test_live_gpt_live.py:178,191`).
- [ ] Track deferred 4.3.6 (GPT-Live routing pre-warm behind a flag, which needs a side-effect-free router mode) as a follow-up issue (task-spec.md:699).
- [ ] Run the manual Discord checklist (`docs/live-checklist.md`) before relying on the Discord transport. DAVE receive, the gate threshold, and Grok dialect acceptance have no automated coverage against real services.

## What Went Well

The plan's layer boundaries held across 30 commits: 159 of 161 sub-tasks, 667 default tests, and 4 real-Hermes e2e tests. One review cycle closed both high findings with regression tests taken from the repro files.

<!-- DRIFT-SUMMARY v1.0
{ "rubricVersion": "1.0", "drift": { "high": 2, "medium": 11, "low": 1 }, "actionItems": 7 }
-->
