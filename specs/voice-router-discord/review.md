# Review: voice-router-discord

Frozen diff: e5c2a70 (merge base 0fd20c6), 135 files, +24090/-1.
Panel: A=sdlc-reviewer/opus, B=sdlc-reviewer/opus. Fan-out: Area 1 (voice side) and Area 2 (orchestration side), each seat all four passes per area.

## Mechanical checks (round 1)

- Check: uv sync --frozen → pass
- Check: uv run ruff check . → pass
- Check: uv run ruff format --check . → pass
- Check: uv run pyright → pass
- Check: uv run pytest -m "not live and not e2e" → fail (1 of 3 runs: 1 failed, 645 passed; other 2 runs 646 passed)
- Check: PATH=<hermes-venv>/bin:$PATH uv run pytest -m e2e → pass (4 passed)
- Check: uv run pytest -m live → skipped (no OPENAI_API_KEY; not runnable here)

M1 (gating, test): tests/contract/test_realtime_session.py::test_announcement_is_spoken_anyway_after_the_wait_cap is flaky — 2 of 8 isolated runs fail (TimeoutError after 5.5 s), 1 of 3 full-suite runs. Doc-keeper hypothesis: _wait_for_floor starts its 20 s count at first clock read, so a clock advanced before the pump reads it under-counts the wait. Not a base-branch failure (test is new).

## Consolidated findings (round 1)

Contract tier: 3 (all four dispatches; new repo, no external consumers). Contract-tier gate: none.

### Gating (critical/high) — sent to Reproduce
- P1 high introduced logic — hermes_duplex/providers/realtime.py:345-354 + session.py:581-588 — `send_function_output` sends `response.create` immediately; the adapter emits the call on `response.function_call_arguments.done`, before the calling response's `response.done`, so the request races an active response (OpenAI `conversation_already_has_active_response`, modelled in the repo's own fixtures). Status/cancel/who's-asking outputs and the post-delegate follow-up are never spoken. (Seat A Area 1; Seat B Area 1 judged it acceptable — orchestrator checked source: DelegationRequested is emitted at realtime.py:457 on arguments.done, before response.done at :467; A upheld.) Preserve: receipt <50 ms; exactly one output per call; _Floor accounting.
- P2 high introduced logic — hermes_duplex/transports/discord/receiver.py:394-412 — sole-allowlisted-member inference counts only allowlisted members, so a stranger's unmapped SSRC is credited to the one allowlisted person (stranger audio reaches the mix and acts with that person's permissions). Both seats; A=medium, B=high; recorded high (authorization bypass of MUST "Audio from unknown Discord users is dropped before mixing"). Spec wording (task 5.2) also needs correcting. Preserve: solo-member rejoin inference; SPEAKING remaps.
- M1 / P13 medium introduced logic (test) — tests/contract/test_realtime_session.py:376-394 — flaky (orchestrator 2/8, B 8/25, A 4/8): the test advances the fake clock before the announcement is queued (tracker state set before fsync await; _announce after). Gating as a failing test check. Preserve: cap assertion.

### Escalated (medium/low logic, explicitly escalated by orchestrator)
- P3 medium — receiver.py:372-376 — accepts DAVE "Unencrypted" frames when davey passthrough is disabled; bypasses DAVE sender binding and enables P2 with DAVE on. Escalated: security, same attack path as P2.
- P4 medium — audio/mixer.py:127-128 with receiver/bot — 40/60 ms Opus packets decode to 7680/11520 bytes; mix_allowed raises and ends the conversation (repro run by Seat B). Escalated: crash on valid input. Fix: per-person FrameCarry re-chunk.
- P5 medium — transports/web/server.py:209/277 — aiohttp access log writes `?token=` (both seats; B ran it). Escalated: secret in logs (AGENTS.md rule).
- P7 medium — providers/gpt_live.py:41,194-196 + prompts CHARS_PER_TOKEN=4 — 2000-char cap can exceed GPT-Live's 500-token append limit for numeric text. Escalated: silent loss of results; cheap conservative fix.
- P11 medium — session.py:346-351 + tasks.py:148-171 — tasks `running` when their session ends stay `running` in the shared tracker (wrong "queued behind", status, cancel of finished runs; repro run by Seat B). Escalated.
- P12 medium — router/composite.py:346-347 + session.py:660-672 — a refused profile still becomes the speaker's sticky profile. Escalated. Preserve: router sees all profiles; _control never touches active profile.
- P16 low — router/decisions.py:173-174 — any refused question discards a valid profile answer. Escalated (cheap robustness fix).
- P19 low — session.py:743-778 + tasks.py:202-206 — a cancel can be confirmed twice when `run.cancelled` beats `stop()`. Escalated: spec wants exactly one confirmation.

### Red-flag findings (selected)
- P6 medium — Red flag — Repetition — double OutputPacer + transport "played ms" counts nobody uses (transports/web/server.py, discord/bot.py, discord/playback.py, transports/base.py). Both seats (A=low, B=medium). Preserve: ≤ lead at transport; flush within one tick; Discord source never returns b"".
- P15 low — Red flag — Information Leakage — prompts.HERMES_UNREACHABLE / not_configured dead; spoken failure wording decided in dispatch/hermes_api.py reasons. Both seats.

### Gating-mechanical
- P8 low mechanical — providers/realtime.py:61-63 — delegate tool description promises a task number (drift after receipt amendment).
- P9 low mechanical — transports/base.py:25-28, audio/mixer.py:60-61 — InputFrame.t_ms / AttributionRing docstrings false (keyed by session `_sent_ms`). Docstring fix only.
- P14 low mechanical — dispatch/hermes_api.py:19-20 — absolute self-imports (second module copy when loaded as hermes_plugins.<slug>; Seat A reproduced ModuleNotFoundError without the wheel). Both seats.

### Observations (not fixed this round)
- P10 low logic — discord/bot.py:200-215 — moves force a full rejoin (discord.py handles moves natively).
- P17 low logic — session.py:658-660 + decisions.py/jev.py — history drops speaker roles; assistant lines sent as user. Both seats. Plan-level change to RouteRequest.history.
- P18 low logic — dispatch/hermes_api.py:192-197 — POST timeout reported unreachable; could retry once with the same Idempotency-Key.
- P20 low logic — router/eval.py — composite effort and deadline-miss metrics misstated.
- P21 low logic — session.py:611-633 — back-to-back GPT-Live windows can merge if the 2nd delegation event arrives >~500 ms late.
- P22 low logic — session.py:794-804 + wire.py — announcement sent between socket close and reconnect is lost.

## Needs user decision
- P23 service.py:105-131 — plan MUST "Service stop cancels all tasks": stop also POST /stop each open Hermes run, or amend the plan (current: runs keep going, results not spoken).
- P24 tests/fixtures/audio — commit TTS-generated portfolio.wav/tesla.wav (needs a key) or accept the live tier unrunnable until generated.

## Repro not confirmed (warnings)
- Repro not confirmed: P5 server.py:277 — Seat A — not run (Seat B ran it: confirmed).
- Repro not confirmed: P7 gpt_live.py:41 — Seat B — needs live GPT-Live.
- Repro not confirmed: P12 composite.py:347 — Seat B — needs two-person harness.
- Repro not confirmed: P19 session.py:743 — Seat B — needs fake ordering.
- Repro not confirmed: P21, P22 — Seat A — timing windows.

## Reproductions (cycle 1)
- P1: reviewer-right — 2 new tests fail (response.create sent before the calling response's response.done; receipt 5.4-5.6 ms). Source: repro-P1.md.
- P2: reviewer-right — stranger on unmapped SSRC delivered as 'rob' with DAVE off and on; preserve cases pass. Tester labelled it "reproduced-correct" but its evidence shows the bug; recorded as reviewer-right. Source: repro-P2.md.

## Fix Once (cycle 1) — commit after e5c2a70
Coder fixed all 17 selected (P1-P9, P11-P16, P19; M1). Orchestrator verification: build gate pass; default tier 667 passed ×3; e2e 4 passed; wait-cap test 0/20 failures; P8 description no longer promises a number (realtime.py:63-64); P9 docstrings say attribution keys by session input ms (transports/base.py:26-29, mixer.py:60-61); P14 no absolute self-imports (grep empty) + AST test; P15 dead prompts removed (grep empty).
Deviations accepted: P19 uses TaskTracker.stopping() so _end stays quiet during a cancel (one confirmation on tool and control paths); P12 passes a `permitted` predicate into route(); P15 deleted dead prompts; P1 changes send_function_output to return bool (Tier 3); P7 2.5 chars/token applies to all providers.
Logic fixes sent to scoped re-review: P1, P2, P3, P4, P5, P6, P7, P11, P12, P13, P15, P16, P19.

## Scoped re-review (cycle 2) — on c339ac5
Seat A: 12/13 resolved (P11 mostly; P19 race). Seat B: 11/13 resolved (P7 not resolved; P2 partly).
Resolution per original finding: P1 resolved; P2 resolved for human strangers (bot case reopened as P25); P3, P4, P5, P6, P12, P13, P15, P16 resolved; P7 NOT resolved (Seat B measured 1250-char numeric results at 529-1117 o200k tokens > 500; Seat A's "resolved" only checked the char cap — orchestrator accepts B's measurement); P11 mostly (P28 leftover); P19 mostly (P26 race).
Mechanical fixes P8, P9, P14 verified by orchestrator (no re-review needed).

### New findings surfaced at the two-round cap (observations, reason: surfaced at two-round cap)
- P25 medium introduced logic — transports/discord/receiver.py:415-419 — inference skips every bot, so another bot's (e.g. TTS/music) unmapped SSRC is credited to the sole allowlisted person; test_other_bots_do_not_prevent_inference pins it. Both seats (B ran it). Fix: exclude only our own bot. Security residual of P2 — flagged for the user at the PR gate.
- P27 medium introduced logic — providers/realtime.py:462-468 — deferred response.create is sent even when the calling response was cancelled by barge-in (talks over the user; stale floor count). Both seats (A ran it). Fix: on cancelled response.done drop the due reply and release the floor count.
- P26 low introduced logic — session.py:777-779 — _end reads tracker.stopping() after awaiting the transition, so a cancel can still be confirmed twice (A ran 5/5). Fix: read the flag before the await.
- P28 low introduced logic — session.py:785-788 — status of a task by number still says "running" for an unfollowed task.
- P29 low introduced logic — tasks.py:169-181 + session.py:722-726 — "Task N started" spoken for a task Hermes queued behind an unfollowed run.
- P7 (unresolved, medium escalated logic) — prompts.py:8-16, gpt_live.py:41-43,197 — 2.5 chars/token is not a safe bound for numeric/CJK text; docstring + architecture.md "fits whole" claim false. Fix options: ~1 char/token for GPT-Live appends, split across appends, or a real tokenizer (new dependency → plan decision).

### Needs user decision (added)
- P30 tasks.py:169-201 — unfollowed tasks are excluded from ahead_of/cancel: a new task may be announced "started" while Hermes queues it, and the person cannot cancel the still-running run. Decide together with P23 (stop Hermes runs at session/service end?).

## Final checks (on c339ac5)
- Check: uv sync --frozen → pass
- Check: uv run ruff check . → pass
- Check: uv run ruff format --check . → pass
- Check: uv run pyright → pass
- Check: uv run pytest -m "not live and not e2e" → pass (667 passed, 3 consecutive runs)
- Check: PATH=<hermes-venv>/bin:$PATH uv run pytest -m e2e → pass (4 passed)
- Check: wait-cap test isolated ×20 → pass (0 failures)
Final checks: pass

## Resolution summary
- Gating critical/high: P1 resolved; P2 resolved for its reported case (human stranger), bot variant recorded as P25 (medium) for the PR gate. M1/P13 resolved.
- Gating-mechanical: P8, P9, P14 resolved (verified).
- Red-flag: P6, P15 resolved.
- Escalated: P3, P4, P5, P11 (P28 leftover), P12, P16, P19 (P26 leftover) resolved; P7 unresolved → observation (not critical/high).
- Observations carried to PR gate: P7, P10, P17, P18, P20, P21, P22, P25, P26, P27, P28, P29.
- Needs user decision: P23, P24, P30.
Review complete (two-round cap reached; no unresolved critical/high or gating-mechanical findings).

## Review round 3 — user-directed prompt strategy + provider layout (baa2178...6e7837c)
Panel: A=sdlc-reviewer/opus, B=sdlc-reviewer/opus (full four passes over the two commits). Contract tier 3; gate none.
Mechanical (6e7837c): uv sync, ruff check, ruff format --check, pyright pass; default 719 passed; e2e 4 passed; goldens identical across the move.
Selected for fix:
- Q1 medium logic (both seats) — realtime/openai/prompt.py:76-77, realtime/xai/prompt.py:57 — unconditional "Do not call a tool again with the same arguments" blocks repeat task_status / fresh delegate (guide scopes it to after failure). Escalated. Fix: scope to delegate of in-flight work.
- Q2 medium Red flag — Information Leakage (B) — delegate receipt {"status":"started"} in session.py:582, realtime/adapter.py:70, openai/prompt.py:64. Fix: one DELEGATE_RECEIPT constant rendered with json.dumps; goldens byte-identical.
- Q3 medium logic (A) — persona key means different things per provider (drops assistant.name on GPT-Live/Grok; contradictory identity on Realtime); whitespace-only persona/name accepted. Escalated. Fix: identity line always from name; persona replaces only style/tone; reject whitespace-only.
- Q4 medium logic (A) — gpt_live/prompt.py lost the progress-notes rule while the session still sends thinking appends. Escalated.
- Q5 medium mechanical — Concepts and vocabulary: RealtimePrompter is OpenAI-only though "Realtime" names the family (A). Vocabulary escalation. Fix: rename OpenAiRealtimePrompter (+ docs).
- Q6 low logic (B, A noted) — prompts/base.py json_report over-truncates escape-heavy text. Escalated (cheap).
- Q7 low mechanical (B) — session.py:5, AGENTS.md:46 say session adapts only via ProviderCapabilities.
- Q8 low logic (A) — TaskReport status Literal wider than produced. Escalated: narrow or document.
- Q9 low Red flag — Information Leakage (A) — ProviderSpec.prompter duplicates provider.prompter; golden test checks the spec's. Fix: goldens derive from a built provider's prompter (or drop spec field).
Pre-existing:
- Q10 config.py:23,441-469; doctor.py:21-23 — provider names/xai VAD rule duplicated outside the registry (follow-up).
Needs user decision (resolved): Q11 cancel confirmation — user chose A (2026-11-03): cancel immediately when a task is named or only one is running; ask "which one?" when several run and none is named.

### Round 3 fix (92c9d65) — scoped re-review
Both seats: Q1–Q9 resolved, preserves kept; Q11 option A implemented in all three prompts.
Surfaced at two-round cap (observations, reason: surfaced at two-round cap):
- Q12 medium logic introduced (both seats) — realtime/openai/prompt.py:82-83, realtime/xai/prompt.py:60-61 — "unless the user explicitly asks you to check again" lets the model re-delegate work still running (duplicate run / side effects, e.g. trades). Fix: while running, "check again" → task_status; re-delegate only after the report arrived and the user asks to check again.
- Q13 medium logic newly exposed (Seat A) — openai/prompt.py:74-77, xai/prompt.py:49-52, gpt_live/prompt.py:28-31 — "names the task" includes by description, but task_id is only passed for spoken numbers; without task_id the session cancels the latest running task → wrong task. Fix: pass task_id whenever the model knows which task; if several run and the number is unclear, ask which one (or task_status first).
- Q14 low logic introduced (Seat B) — cancel policy hand-written in three prompters while architecture.md says shared rules live once in blocks.py. Fix: CANCEL_POLICY in blocks.py or reword the doc.
### Final checks (92c9d65)
- Check: uv sync --frozen → pass; ruff check → pass; ruff format --check → pass; pyright → pass
- Check: pytest -m "not live and not e2e" → pass (732, 2 runs); pytest -m e2e (real Hermes) → pass (4)
Final checks: pass

## Review round 5 — final fixes (92c9d65..791dccd)
Panel A/B opus. Contract tier 3; gate none. Mechanical (791dccd): gate pass; 760 default ×2; e2e 4.
Items 1–7 implemented per brief (both seats). Findings:
- S1 medium logic (both) — session.py:706-711,951-961 _task_number: any bare number wins ("top 5 stocks"→5, "30 day report"→30); context fallback reads stale/ended "task N" → wrong task cancelled at once (ran). Fix: only "task N" forms; context only from cancel/status sentences or active tasks; else None.
- S2 medium logic (A; B low) — session.py:431-433 adoption only on first hearing: tasks left by a lingering/parallel session never adopted by an already-open session; false "I'll tell you" promise.
- S3 medium logic (A) — tasks.py restore: torn UTF-8 line or naive timestamp raises and blocks service start (ran).
- S4 low logic (B) — completed result queued but unspoken at session end is lost (not unattended/announced).
- S5 low mechanical (A) — task_summary first-person promise inside reseed context.
- S6 low mechanical (A) — _unattended comment wrong.
- S7 low logic (A) — unreadable status of adopted run → live wording instead of "while you were away".
- S8 low logic Red flag — Repetition (B) — gist re-implements fit_text; two _SENTENCE_END regexes.
Needs user decision:
- U1 (both) privacy — adopted results announced in whatever conversation first hears the person (private web task spoken in a shared Discord channel; non-allowlisted members hear it).
- U2 (A) restore has no age cutoff (stale/pre-upgrade tasks; Hermes forgets run status after 1 h → "lost track").

## Review round 6 — round-5 fixes (791dccd..fea792e), last review before push
Both seats: items 1–10 resolved, preserves kept; no critical/high. Per user decision (round-5 decision 3 = A) remaining items go to the PR as known issues; no further fix round.
Known issues carried to the PR:
- K1 medium (Seat B) — session.py:470-476 _pump_adoptions: re-adopts for anyone ever heard in a session; a Discord session the person has left can adopt and speak their result, which then is not repeated in their next conversation. Fix idea: last-heard window or Discord channel membership.
- K2 medium (Seat B, pre-existing from S1) — session.py:300-303 _CANCEL_OR_STATUS_SENTENCE: a question context ("Should I cancel task 3 or task 4?") yields 3 → cancels task 3 at once (GPT-Live). Fix: anchor progressive form, require exactly one number.
- K3 low (both seats) — config.py _tasks / tasks.py restore: huge/inf/nan restore_max_age_h passes validation and fails start (OverflowError); unguarded stale-marking append.
- K4 low (Seat B) — session.py:883-884 _end: result lost if the session ends during the transition awaits (ms window).
- Earlier accepted lows recorded above (P10, P17, P18, P20, P21, P22) and the README known limitations.
Final checks (fea792e): uv sync, ruff check, ruff format --check, pyright pass; pytest -m "not live and not e2e" 785 passed ×2; real-Hermes e2e 4 passed. Final checks: pass
