# Fix round — join-privacy re-review (review.md "Join-privacy fix (6bc2771) + router (3219250) — scoped re-review")
Base 6bc2771 (local, unpushed). Prefer the simplest robust design over patching races.
1. NF1 (high): a join with owner texts must ALWAYS end the muted connection with a restart, whatever ran in between. E.g. a pending-newcomer-restart flag (or per-connection "carries owner notes" flag set at _connect) that every _apply_prompt_context honours: while a connection is muted, no in-place update is sent; the restart runs even if _same_instructions says equal. Also the variant: join while _connect holds the lock with the private context → that new connection is restarted. A muted connection can never outlive the restart (add a safety: if muted and provider open with no restart pending, restart).
2. NF2 / M-B1: build the reseed summary per attempt inside the retry lambda (or inside _connect under the lock, from the same moment as the instructions).
3. M-B2 / L1: ignore InputTranscript from the muted connection (or end the line at the join); _report_progress waits out the mute like announcements.
4. L-B1 / L2: tests for the re-mute in _apply_prompt_context, the muted pacer drop in _reconnect, the muted floor wait, plus repros: scratchpad/rereview9c-A/repo/tests/unit/test_rereview9c_A.py (test_c_*), scratchpad/rereview9c-B/repro_rr9c_B.py.
5. L-B2: README — when alone again, Realtime/Grok update at once in place; GPT-Live restarts once quiet.
Gate + default tests + real-Hermes e2e; format only changed files; counts.
