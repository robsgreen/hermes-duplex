# Change: load Hermes SOUL.md / USER.md into the voice prompt (user-approved 2026-11-06)

User request: pass each profile's Hermes context to the voice model at runtime.
Files (from $HERMES_HOME, default ~/.hermes = /home/hermes/.hermes on picard):
- default profile: SOUL.md, memories/USER.md
- named profile <p>: profiles/<p>/SOUL.md, profiles/<p>/memories/USER.md
The duplex profile name maps to the Hermes profile name; the Hermes "default" profile maps to the top-level files (decide how a duplex profile names it, e.g. a `hermes_profile` key or the reserved name `default`; check how dispatch already names /p/<profile>).

## Decisions
- Opt-in: `hermes.voice_context_files: [soul, user]` (either, both, or empty/absent = off).
- Framing: SOUL.md is background about the agent the user is talking to (what it is for, can do), NOT the voice model's own instructions; plugin rules and the profile's `voice_context` win on conflict. USER.md is background facts about the owner. Both are rendered as data in each provider's prompter (vendor format), once, for the active profile only.
- USER.md belongs to the Hermes owner: `people.<p>.hermes_owner: true` (at most one person; config error otherwise). Include USER.md only while the conversation is with the owner AND the owner is alone: web (signed-in owner) or a Discord call where the owner is the only allowlisted/human person present. When someone else joins or is heard, drop USER.md from the prompt (instruction update / quiet restart rules as for profile changes); when the owner is alone again, add it back. User decision: "only when I'm alone or on the web".
- Per-person `people.<p>.voice_context` (≤2000 chars) for other people (who have no USER.md), rendered for the person the conversation is with, same presence rules not required (it's config the owner wrote) — but keep it out when several people are present? Keep simple: render for the conversation's current person (web signed-in; Discord joiner then last heard), like profile context.
- Size caps: `hermes.voice_context_max_chars` default 4000 per file; longer files are cut at a line/sentence boundary with one INFO/WARNING log naming the file and sizes. Missing file → nothing, one DEBUG/INFO line; unreadable → one WARNING, never blocks start.
- Freshness: re-read on every connect/reconnect and on profile switch / presence change (mtime cache is fine).
- No Hermes imports; plain file reads. Never log file contents.
- Docs: README Configure table + a "Hermes context files" paragraph (privacy note), architecture.md prompt-context section, duplex.example.yaml, live-checklist items (SOUL in prompt, USER.md alone vs. with others on Discord, web).
- Tests: prompter golden/structural tests (SOUL/USER appear once, framed as background, absent when off), presence rule tests (owner alone → included; second person → removed with instruction update; web → included; non-owner → never), caps/truncation, missing/unreadable files, config validation (two owners, unknown file kinds).

## Addendum (user-approved 2026-11-06): Hermes rewrites the files for voice
- The plugin asks each Hermes profile, via the API server it already uses (`/p/<profile>/v1/runs`, the profile's own key, its own model; a dedicated session id such as `duplex-voice-context-<profile>`, not a person's session), to rewrite its SOUL.md / USER.md for a voice assistant: who the owner is (USER), what the agent is for and can do and what to always hand off (SOUL); short plain sentences; no tools/formatting/markdown instructions; within the size cap. The source text is sent in the request (the plugin reads the file), framed as data.
- Cache under `$HERMES_HOME/duplex/voice_context/<profile>.soul.md` and `<profile>.user.md`, with the source fingerprint (sha256) stored alongside (e.g. a small JSON sidecar or front-matter). Atomic writes.
- Never during a call: conversations use the cached rewrite immediately; when a source file's fingerprint changes, a background refresh runs (one at a time per file, with a timeout) and is used from the next connect/switch. Until a rewrite exists, or if it fails, use the capped original (as planned).
- Hand edits stick: if the cached file was edited by the user (its own content hash differs from what the plugin wrote), don't overwrite it until the source changes or `refresh --force`.
- CLI: `hermes-duplex voice-context show <profile> [--config]` prints exactly what the voice model gets (with which person/presence assumptions noted); `hermes-duplex voice-context refresh [<profile>] [--force]` rewrites now. Also via `hermes duplex voice-context …`.
- Don't log file or rewrite contents. Docs + tests (cache hit/miss, fingerprint change triggers background refresh, failure → capped original, hand edits kept, --force, CLI output).
