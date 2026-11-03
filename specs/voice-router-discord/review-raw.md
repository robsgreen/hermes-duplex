# Raw panel reports (for consolidation)

## Seat A / Area 1
- A1-F1 high introduced logic: realtime.py:345-354 + session.py:581-588 — send_function_output sends response.create while the calling response is still active (function_call_arguments.done precedes response.done) -> OpenAI conversation_already_has_active_response; status/cancel/who's-asking outputs and post-delegate follow-up never spoken. Repro run vs fake server. Fix: defer response.create until calling response done (pending flag, one create per response.done).
- A1-F2 medium introduced logic: receiver.py:394-412 — sole-allowlisted-member inference counts only allowlisted members; stranger's unmapped SSRC credited to the allowlisted person (no-DAVE / passthrough). Repro run. Fix: infer only when exactly one non-bot member and allowlisted.
- A1-F3 medium introduced logic: web/server.py:277 — aiohttp default access log logs path_qs incl. ?token=. Fix: access_log=None or path-only logger. (repro not run)
- A1-F4 low introduced logic: Red flag — Repetition: double OutputPacer (session + transports); transport count only logged.
- A1-F5 low mechanical: realtime.py:61-63 delegate tool description promises a task number (drift after receipt amendment).
- Gaps: GPT-Live offset_ms timeline vs _sent_ms; truncate on out-of-band announcement item may error; flaky test cause = test races clock before pump reads it.

## Seat B / Area 2
- B2-F1 medium logic: session.py:346-351 + tasks.py — running tasks stay `running` in the shared tracker after their session ends -> wrong "queued behind", status, and cancel of finished runs. Repro run.
- B2-F2 medium logic: composite.py:346-347 — refused profile still becomes speaker's sticky profile. (not run)
- B2-F3 medium logic(test): test_realtime_session.py:385-394 flaky (8/25) — test advances clock before announcement queued; = M1.
- B2-F4 low mechanical: dispatch/hermes_api.py:19-20 absolute imports -> second module copy under hermes_plugins.<slug>.
- B2-F5 low logic: Red flag — Information Leakage: prompts.HERMES_UNREACHABLE/not_configured dead; spoken text decided in hermes_api reasons.
- B2-F6 low logic: decisions.py:173-174 any-question refusal discards valid profile answer.
- B2-F7 low logic: decisions.py:120-124 assistant lines sent as role user.
- B2-F8 low logic: hermes_api.py:192-197 POST timeout reported unreachable; retry once with same Idempotency-Key.
- B2-F9 low logic: session.py:743-778 cancel confirmed twice in race.
- B2-F10 low logic: eval.py composite effort/deadline-miss misstated.
- Needs user decision: (1) service stop cancel Hermes runs? (2) commit live WAVs?
- Gaps: status_text cross-thread dict iteration.

## Seat B / Area 1
- B1-F1 HIGH logic: receiver.py:394-412 sole-allowlisted inference credits stranger audio (same as A1-F2, A rated medium). Repro run. Spec wording needs fix too.
- B1-F2 medium logic: receiver.py:372-376 accepts "Unencrypted" DAVE frames when passthrough disabled -> bypasses DAVE sender binding; enables F1 with DAVE on. Stub pins it.
- B1-F3 medium logic: mixer.py:127-128 + receiver/bot — 40/60 ms Opus packets decode to 7680/11520 bytes -> mix_allowed ValueError ends conversation. Repro run. Fix: per-person FrameCarry rechunk.
- B1-F4 medium logic: web/server.py access log logs ?token= (same as A1-F3). Repro run.
- B1-F5 medium logic: gpt_live.py + prompts CHARS_PER_TOKEN=4 — 2000 chars may exceed 500-token cap for numeric text. Not run.
- B1-F6 medium logic: Red flag — Repetition: double pacer + unused played-ms (same as A1-F4, A rated low).
- B1-F7 low mechanical(docstring)/logic: InputFrame.t_ms unused; AttributionRing docstring false.
- B1-F8 low logic: bot.py move handling force-rejoins (discord.py handles moves natively).
- Note: B says response.create right after function output is acceptable (contradicts A1-F1) — investigate.
- Gaps: GPT-Live timeline wall-clock vs input-audio; out-of-band interrupt behaviour; browser echo cancel; Realtime 4s attribution window vs long model speech before tool call.
