## feat: hermes-duplex voice plugin (realtime voice → routed Hermes profiles)

### Why
Talk to Hermes by voice in a Discord voice channel or a web page, with full-duplex conversation handled by the realtime voice model, and have real work handed off to the right Hermes profile. A fast router picks the profile, effort level and model per request (e.g. a portfolio question goes to `stock-picker`).

### What
A standalone Hermes plugin (Python 3.14, uv) with four swappable layers:
- **Transports:** its own Discord bot (DAVE-encrypted voice receive adapted from Hermes's receiver, MIT, credited in `NOTICE`) and a token-gated web page.
- **Providers (one package each):** OpenAI GPT-Live (client delegation), and one Realtime adapter with OpenAI and xAI Grok dialects. Each has its own prompt strategy following its vendor's guide; `assistant:` config sets name, persona and backchannel.
- **Router:** local static-embedding matcher plus OpenAI Decisions or Jev, run in parallel under a 300 ms deadline, with sticky follow-ups, a breaker, and a profile × effort model table.
- **Dispatcher:** Hermes API server `/p/<profile>/v1/runs`, with SSE progress, stop, and code-keyed retries; per-person sessions per profile.

Also: per-person profile permissions, results framed as data (not instructions), tasks that outlive their conversation (announced "while you were away", restored for 24 h), `hermes duplex serve | doctor | eval-router`.

### How
- Runs inside the gateway via `ctx.register_platform("duplex")`, or standalone with `hermes duplex serve`. No Hermes internals are imported, and there is no `openai` SDK (Hermes pins an older one); dependency ranges contain Hermes's pins (CI `deps-compat` job).
- Interruptions: provider events plus truncate on Realtime; a loudness gate for GPT-Live. One output pacer.
- Design, decisions and review history: `docs/system/architecture.md`, `docs/specs/202611/voice-router-discord.md`.

### Testing
- `uv run ruff check . && uv run ruff format --check . && uv run pyright`: clean.
- `uv run pytest -m "not live and not e2e"`: 785 passed.
- `uv run pytest -m e2e`: 4 passed against a real hermes-agent @6112c2a gateway with two profiles and a fake LLM (recipe in README). The CI e2e job has not run on GitHub Actions yet.
- Router eval (103 labelled phrases, real potion-base-8M): knn 0.825, composite 0.913.
- Not yet run (needs keys or tokens): the live GPT-Live tier, real Discord, real Grok.

### Manual to-dos before marking ready
- [ ] Generate and commit the live fixtures: `OPENAI_API_KEY=… uv run python -m tests.live.make_audio_fixtures`, then `HERMES_DUPLEX_LIVE=1 uv run pytest -m live`.
- [ ] Run `docs/live-checklist.md` on real Discord (DAVE receive, barge-in, multi-speaker, rejoin, tasks across conversations) and items 15+ for each provider's prompt.
- [ ] Confirm the Grok items marked unverified in README known limitations.

### Review observations (not addressed)
- `hermes_duplex/session.py` `_pump_adoptions` (medium): a session keeps adopting for anyone it ever heard. A Discord session the person has left can speak their result, which is then not repeated in their next conversation.
- `hermes_duplex/session.py` `_CANCEL_OR_STATUS_SENTENCE` (medium): on GPT-Live, a question context like "Should I cancel task 3 or task 4?" can resolve to task 3 and cancel it at once.
- Accepted by the user: earlier task results are read in full wherever the person is next heard, including shared Discord channels.
- Plus 8 low observations in the spec's review record (for example: a huge `tasks.restore_max_age_h` fails start; a millisecond window where an ending result can be lost).

### Risk
**Level:** medium. This is a new plugin with no existing users. It cancels and starts Hermes runs on users' behalf, and real-service behavior is unverified until the manual checklist runs.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01JA8ywWLhg3ZaRTiD9oRazX
