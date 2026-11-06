## feat: hermes-duplex voice plugin (realtime voice → your active Hermes profile)

### Why
Talk to Hermes by voice in a Discord voice channel or a web page. The realtime voice model handles the full-duplex conversation and hands real work to Hermes. Each person talks to one Hermes profile at a time (for example `stock-picker`) and switches explicitly, by voice, from Discord, or on the web page.

### What
A standalone Hermes plugin (Python 3.14, uv) with four swappable layers:
- **Transports:** its own Discord bot (DAVE-encrypted voice receive adapted from Hermes's receiver, MIT, credited in `NOTICE`) and a token-gated web page.
- **Providers (one package each):** OpenAI GPT-Live (client delegation), and one Realtime adapter with OpenAI and xAI Grok dialects. Each has its own prompt strategy following its vendor's guide; `assistant:` config sets name, persona and backchannel.
- **Router:** a local static-embedding matcher plus OpenAI Decisions or Jev, run in parallel under a 300 ms deadline, with a breaker. It sets the effort and the model from the active profile's effort table, and can suggest a switch.
- **Dispatcher:** Hermes API server `/p/<profile>/v1/runs`, with SSE progress, stop, and code-keyed retries; per-person sessions per profile.

**Active profiles.** Each person's active profile is saved in `$HERMES_HOME/duplex/active_profiles.json` and changes only when they ask:
- **By voice:** "switch to stocks", "let me talk to the stock picker". "Talk to stocks about Tesla" switches, then runs "Tesla". Nicknames come from `aliases`.
- **By the provider's tool:** the `switch_profile` tool on OpenAI Realtime and Grok.
- **From Discord:** `/duplex profile <name>`.
- **On the web page:** the "Talking to" picker.

When the router thinks another profile fits better, the voice asks "Want me to switch?" and holds the request until the person answers: yes runs it on the new profile, no runs it where they are. A profile's `voice_context` goes into the voice prompt while it is active. A Hermes Desktop "Bot" is a Hermes profile, so each Bot can be a duplex profile.

Also:
- per-person profile permissions;
- results framed as data, not instructions;
- tasks that outlive their conversation, announced "while you were away" and restored for 24 h, and named by topic;
- `hermes duplex serve | doctor | eval-router`.

### How
- It runs inside the gateway via `ctx.register_platform("duplex")`, or standalone with `hermes duplex serve`.
  - No Hermes internals are imported.
  - There is no `openai` SDK, because Hermes pins an older one; dependency ranges contain Hermes's pins (CI `deps-compat` job).
- **Prompt changes when someone switches:**
  - **OpenAI Realtime and Grok:** update their instructions in place, and the plugin waits for the server's confirmation.
  - **GPT-Live:** reconnects, and never while someone is speaking.
- **Interruptions:** provider events plus truncate on Realtime; a loudness gate for GPT-Live. One output pacer.
- Design, decisions and review history: `docs/system/architecture.md`, `docs/specs/202611/voice-router-discord.md`.

### Testing
- `uv run ruff check . && uv run ruff format --check . && uv run pyright`: clean.
- `uv run pytest -m "not live and not e2e"`: 1530 passed.
- `uv run pytest -m e2e`: 6 passed against a real hermes-agent @6112c2a gateway with two profiles and a fake LLM (recipe in README). They cover a voice switch, a refused switch, a "yes" to a switch suggestion, the active profile surviving a restart, a SOUL.md rewrite used in a call, and doctor. The CI e2e job has not run on GitHub Actions yet.
- **Router eval** (103 labelled phrases, real potion-base-8M, knn):
  - choice accuracy 0.825;
  - switch suggestions 20 of 20 right, 20 of 38 needed ones made;
  - effort accuracy 0.306 (knn doesn't judge effort).
- **Not yet run** (needs keys or tokens): the live GPT-Live tier, real Discord, real OpenAI Realtime, real Grok.

### Manual to-dos before marking ready
- [ ] Generate and commit the live fixtures: `OPENAI_API_KEY=… uv run python -m tests.live.make_audio_fixtures`, then `HERMES_DUPLEX_LIVE=1 uv run pytest -m live`.
- [ ] Run `docs/live-checklist.md` on real Discord.
  - Items 1–15: DAVE receive, barge-in, multi-speaker, rejoin, tasks across conversations, and each provider's prompt.
  - Profile items 16–25, especially:
    - on OpenAI Realtime, a "yes" to "Want me to switch?" and to "Cancel that?";
    - on Grok, that an instructions-only update is confirmed and tools still work after a switch.
- [ ] Confirm the Grok items marked unverified in README known limitations.

### Since the first push
- **Hermes context in the voice prompt (opt-in):** each profile's SOUL.md, the owner's USER.md and a "current projects" briefing, rewritten for voice by the profile itself and cached; the person's open tasks; per-person `voice_context`. USER.md and the briefing appear only on the web or when the owner is alone on Discord; once someone joins, a session that carried them is restarted and nothing from it reaches the newcomer.
- **Router:** a request is a task unless status/cancel is near-certain; abandoned backend calls no longer log unretrieved errors.
- **Switching:** quiet switches on GPT-Live ("Switching you to …" after the reply, then reconnect); switch phrasings from one table; targets matched by meaning when an agent is named, by name otherwise. Held-out switch set (141 phrases): precision 1.000, recall 0.920, task taken as switch 0.000.
- **Fixes from live testing on picard:** Discord DAVE decryption (davey pinned to Hermes's 0.1.x), CI download rate limit, a racy test.

### Known issues (from review, not addressed)
- **Decisions/Jev are unmeasured** for the status/cancel bar (0.995, tuned on knn); a loosely worded cancel may start a task instead. Needs `eval-router --config` with keys.
- **Decisions often misses the 300 ms deadline** on picard; raising `router.deadline_ms` to ~1000 is suggested.
- **A few unusual switch wordings** ("back to the regular assistant", "the agent that does flights") run as tasks; "switch to <name>" always works.
- **An announcement being spoken when someone joins** an owner-only call is cut and not repeated.
- **Cancel by number** on GPT-Live ("Should I cancel task 3 or task 4?") can cancel task 3.
- **Accepted by the user:** earlier task results are read in full wherever the person is next heard.
- **Plus low observations** in the spec's review record.

### Risk
**Level:** medium. This is a new plugin with no existing users. It cancels and starts Hermes runs on users' behalf, and real-service behavior is unverified until the manual checklist runs.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01JA8ywWLhg3ZaRTiD9oRazX
