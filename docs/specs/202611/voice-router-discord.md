# Voice Router over Discord and Web

Completed: 2026-11-03

## Problem

We wanted to talk to Hermes in a Discord voice channel. The voice model should chat directly and hand
real work to the right Hermes profile, at the right effort and model. The voice plugins that existed
all failed in one of three ways:

- **No routing.** Each one sent all work to a single profile. hermes-talk reads one
  `TALK_AGENT_PROFILE` and breaks on multi-profile gateways (its issue #185).
- **Blocking hand-offs.** A voice model that waits on a synchronous function call goes silent for the
  whole Hermes task, which can take minutes, and nobody can interrupt it.
- **No usable Discord receive.** Discord has required DAVE end-to-end encryption since March 2026.
  `discord-ext-voice-recv` cannot decrypt it on discord.py 2.7. Hermes core has a working receiver,
  but no plugin hook reaches it, and patching private attributes is not allowed in the catalog.

hermes-duplex is a standalone Hermes plugin. It has four layers (transports, providers, router,
dispatcher) around `VoiceSession` and `TaskTracker`. v1 ships the Discord and web transports, the
GPT-Live and Realtime (OpenAI, xAI Grok) providers, and the knn, Decisions, and Jev router backends.
It reaches Hermes only through the API server. How it works: `docs/system/architecture.md`; usage:
`README.md`.

## Alternatives Considered

| Approach | Pros | Cons | Why Rejected |
|----------|------|------|--------------|
| Fork hermes-talk | Already does Discord, GPT-Live, Grok, background tasks; MIT, active | About 122k lines built around one profile; Discord audio patches private Hermes attributes | Too large to reshape around routing. We borrowed its tricks instead: paced silence, silent underrun frames, truncation at the played offset, data-framed results |
| Build on Pipecat 1.15 | Adapters for every voice provider | No Discord transport; its pipeline would own session logic; large dependency tree vs Hermes's upper-bounded pins | We read its adapter source for edge cases only |
| Voice model picks the profile (enum on `delegate`) | Free | No calibrated confidence, weak on effort and model, and GPT-Live has no tools to put it on | Kept as `profile_hint`, one more input to the router |
| Hermes `delegate_task` via `ctx.dispatch_tool` | No HTTP hop | Ignores requested model and effort when routing to a profile; child starts with no conversation | API server honors both per call and keeps one session per person and profile |
| `discord-ext-voice-recv` or py-cord for receive | Maintained libraries | voice-recv broken under DAVE; py-cord's recording is still marked unreliable, and switching leaves discord.py | Adapted Hermes's `VoiceReceiver` (MIT, attributed in `NOTICE`); py-cord stays the fallback |
| Official `openai` SDK | Typed GPT-Live and Realtime clients | Hermes pins `openai==2.54.0`; GPT-Live needs 3.x; out-of-process runtime loses the gateway lifecycle | Both wire protocols written over `websockets`, using openai 3.42.1 types as the reference |
| GPT-Live native `decisions` delegation | Routing inside OpenAI's session | Still need our own dispatch, permissions, task tracking; locks routing to one provider | One router serves every provider |
| Share Hermes's Discord bot | One bot to invite | Needs a plugin voice hook that does not exist (RFC #77111) | Own bot, own token; can switch when the hook lands |

## Decisions & Trade-offs

- **Decision:** Run our own Discord bot and reach Hermes only through its API server.
  **Trade-off:** A second bot to create and invite, and one loopback HTTP hop per task.
  **Rationale:** No dependency on Hermes internals (a grep test enforces it). Per-call profile, model,
  effort, progress, stop, and session continuity come with the API.
- **Decision:** A router separate from the voice model. A fast local matcher (knn over static
  embeddings) runs alongside a decision model (Decisions or Jev) under a 300 ms deadline, with a
  sticky rule and a profile-by-effort model table from config.
  **Trade-off:** One more moving part and up to one extra API call per task.
  **Rationale:** The voice model never needs to know the profiles, so GPT-Live's tool-less delegation
  fits as is, and adding a profile is a config edit. Model choice stays deterministic. On the 103-phrase
  eval set, knn alone scored 0.825 and the composite 0.913.
- **Decision:** Hand-offs never block the voice model. GPT-Live uses client delegation with
  thinking and commentary appends. Realtime providers get a `delegate` tool that returns a receipt at
  once; results are queued announcements spoken when the floor is free, capped at a 20 s wait.
  **Trade-off:** The session has to rebuild GPT-Live's task text from transcript windows, since the
  delegation event carries none, and it has to track the Realtime floor itself.
  **Rationale:** Conversations stay live and interruptible while Hermes works for minutes.
- **Decision:** The router sees every profile, and permission is checked after routing.
  **Trade-off:** A refused request still costs a routing call.
  **Rationale:** A child asking about the portfolio is told no out loud instead of landing on the wrong
  profile. Review then made sure a refused profile never becomes the speaker's sticky profile.
- **Decision:** Tasks are numbered only after routing and the permission check, so the `delegate`
  receipt is `{"status": "started"}` without a task number (the plan asked for one).
  **Trade-off:** The model cannot quote the number in its first reply; it comes in the start or
  "queued behind task N" notice.
  **Rationale:** Refusals and `_control` intents burn no numbers, and the receipt stays under 50 ms.
- **Decision:** The gateway seam is `ctx.register_platform("duplex")`, whose `connect()` starts the
  service. `register()` runs on a loader thread with no event loop, so `spawn_task` was ruled out.
  **Trade-off:** Hermes refuses plugin platforms under `plugins.isolation: host`; there, run standalone.
  **Rationale:** It is the only documented seam with a running loop and a stop hook.
- **Decision:** Pin discord.py to 2.7.1 and guard the adapted receiver's hooked internals with a test.
  **Trade-off:** We own a 436-line adapted receiver and every discord.py upgrade is a deliberate change.
  **Rationale:** It is the only DAVE receive path that works today on the library Hermes ships.
- **Decision (open):** Stopping the service, or ending a session, stops following its tasks but does
  not stop their Hermes runs. This contradicts the plan's MUST "Service stop cancels all tasks".
  Unfollowed tasks also leave status, "queued behind", and cancel. It goes to the PR gate (review P23,
  P30) as stop the runs or amend the plan.

## What Changed

Everything is new except `README.md` (135 files in all, most of them tests and fixtures).

- `hermes_duplex/session.py`, `tasks.py` — `VoiceSession` and `TaskTracker`
- `hermes_duplex/providers/` — provider interface and capabilities, shared WebSocket client, GPT-Live
  and Realtime (openai, xai) adapters, scriptable fake provider
- `hermes_duplex/router/` — knn, Decisions, Jev backends, `CompositeRouter`, factory, eval harness
- `hermes_duplex/dispatch/hermes_api.py` — Hermes API server client (runs, SSE, stop, error codes)
- `hermes_duplex/transports/` — Discord bot, adapted DAVE receiver, playback source; web server and page
- `hermes_duplex/audio/` — framing, soxr resampling, allowlist mixer and attribution, gate, pacer
- `hermes_duplex/plugin.py`, `service.py`, `cli.py`, `doctor.py`, `config.py`, `people.py`, `prompts.py`
- `.github/workflows/ci.yml` — lint and types, default tests, deps-compat against Hermes pins (fails
  if `openai` resolves), real-Hermes e2e job (not yet run on GitHub Actions)
- Tests: 667 default (unit, contract, in-process e2e), 4 real-Hermes e2e, 1 opt-in live
- See `docs/system/architecture.md` for the design, `AGENTS.md` for conventions, `README.md` for setup
  and known limitations, `docs/live-checklist.md` for manual checks

## Lessons Learned

- Recording interface amendments in the master plan as they happened made review and this retro
  easy to follow. The one change not recorded there (`flush_playback` no longer returns played ms,
  `InputFrame.t_ms` became informational) surfaced only as stale-docstring findings.
- Parallel module tasks left a wiring gap: for a while `build_service` raised `NotImplementedError`
  for the Decisions and Jev backends and for Discord, until an unplanned follow-up wired them. Next
  time, give service wiring its own task.
- Provider fixtures that replay events back to back hide ordering bugs (see P1 below). Fixtures should
  space out the events real servers space out.
- A fake-clock test must advance the clock only after the waiter has read it. One such race failed
  up to 8 of 25 runs before review fixed it.
- 159 of 161 sub-tasks landed. Deferred: GPT-Live routing pre-warm (a MAY item; needs a side-effect-free
  router mode). Pending: the spoken WAV fixtures for the live tier, which need an OpenAI key. No
  real GPT-Live, Realtime, Grok, or Discord DAVE session has run yet.

## Post-Mortem Findings

- **Finding:** Service stop and session end leave Hermes runs running, against a plan MUST; unfollowed
  tasks cannot be cancelled by voice.
  **Impact:** Orphaned runs keep spending model time, and their results are never spoken.
  **Resolution:** Documented in the README's Known limitations; decision deferred to the PR gate (P23, P30).
- **Finding:** The result cap is a character heuristic (2.5 chars per token), not a token bound.
  Numeric text measured 529-1117 tokens against GPT-Live's 500-token append limit.
  **Impact:** A long numeric or CJK result can be silently dropped.
  **Resolution:** Open (P7). The architecture doc now calls the cap an estimate. Options: about one
  char per token for GPT-Live, splitting across appends, or a tokenizer (a new dependency, so a plan
  decision).
- **Finding:** The live tier cannot run: only `silence.wav` is committed.
  **Impact:** No automated check against a real voice provider.
  **Resolution:** Open (P24). The generator is `tests/live/make_audio_fixtures.py`.
- **Finding:** The DAVE receiver, GPT-Live, and the Grok dialect details (voice prefix, flat session
  shape, `tool_choice`, `item.delete`) were built from types and recorded scripts only.
  **Impact:** The first real sessions may fail in ways the fakes do not model.
  **Resolution:** `docs/live-checklist.md` covers each one; run it before relying on Discord.

## Review Highlights

- **P1 (high), Realtime response race.** `send_function_output` sent `response.create` while the
  response that made the call was still active, which OpenAI rejects. Status, cancel, and follow-up
  outputs were never spoken. The fixtures replayed `function_call_arguments.done` and `response.done`
  together, so tests hid it. Fixed: the adapter defers `response.create` until `response.done`. The
  round-2 follow-up P27 (open) is the deferred reply firing even after a barge-in cancelled the
  response.
- **P2 (high), Discord authorization bypass.** Unmapped-SSRC inference counted only allowlisted
  members, so a stranger's audio was credited to the one allowlisted person. The plan's wording
  ("the only allowlisted member in channel") carried the hole into the task spec. Fixed for human
  strangers; the fix skips every bot, so another bot's audio is still credited (P25, open, listed in
  the README). Lesson: authorization rules copied from upstream need negative cases in the plan, and
  "who counts" must be defined exactly.
- **P3, P4, P5 (medium).** Unencrypted frames bypassed DAVE when passthrough was off; 40 and 60 ms
  Opus packets crashed the mixer; the aiohttp access log wrote `?token=`. All fixed. Each one is
  valid input reaching a path the happy-path tests never sent.
- **P7 (medium, open).** Two reviewers disagreed until one counted tokens. A cap backed by a hard
  provider limit needs a test that measures tokens against that limit.
- **P11, P12 (medium).** Tasks from an ended session stayed "running" in the shared tracker, and a
  refused profile became sticky. Both fixed. Both are cross-session or cross-layer state that unit
  tests of one module did not cover.
