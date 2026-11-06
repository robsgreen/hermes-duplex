# Fix round — review 9 (see review.md "Review round 9", seats A and B)
Worktree /home/user/hermes-duplex-worktrees/voice-router-discord, base d7ba585 (pushed). Urgent: A1 is live on PR #1.
1. A1 (critical): `person` in PromptContext must not depend on open tasks; GPT-Live comparison must ignore everything that only task changes touch. Test: voice_context_files off, person without voice_context, GPT-Live caps → hand-off and result never restart (1 connect), result arrives via deliver_result.
2. A2/B-F1 (high): file texts are read at connect/reconnect, explicit switch, first hearing and presence flip only; task-change rebuilds replace only open_tasks. Any GPT-Live restart caused by file text waits for quiet. Test: file changes mid-call → no restart on task events; picked up at next connect/switch.
3. A3 (high): on alone→not-alone, drop held output (pacer drop + flush_playback + truncate where supported, like _barge_in) before the restart/update; reseed after such a restart carries open tasks only, no transcript lines (DEFAULT pending user confirmation). Tests: held owner-only audio not played after join; reseed has no transcript.
4. A4/B-F2: fresh Hermes session id per rewrite/briefing run (suffix run uuid or source sha), never a person's session.
5. A5: SOUL rewrite prompt: text is heard by anyone on a call; must say nothing about the owner or any person.
6. A6: record failure backoff on cache-write OSError.
7. B-F5: briefing_refresh_h finite (math.isfinite), likewise voice_context_max_chars sanity.
8. B-F6: when the context person is no longer present, move context to conversation.person or the sole present allowlisted person (re-evaluate alone).
9. B-F7: drop the mtime/size cache; read files directly.
10. U2 DEFAULT (pending user): defer background rewrites/briefing refreshes while any conversation is active; run them when none is (and at start if none active). Calls never wait.
11. U3 DEFAULT (pending user): Grok gets a lower background cap (e.g. 2000 chars per file) per xAI guide; document.
12. B-F4: align README/architecture on when context changes reach the prompt.
Gate + default tests + goldens + real-Hermes e2e; format only changed files; update test counts.
