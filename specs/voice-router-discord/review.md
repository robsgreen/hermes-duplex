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
