# User decisions (planning conversation)
- 2026-11-03: Discord audio = plugin runs its OWN Discord bot (own token), behind an audio-transport layer; dispatch to Hermes profiles via API server. Swap to shared Hermes bot later if upstream voice hook (RFC #77111) lands.
- 2026-11-03: Must be transport-agnostic: Discord first, also web, and a home smart speaker (likely HTTP-based).
- 2026-11-03: v1 scope = Discord + simple web page transport (option B).
- 2026-11-03: Speakers = owner + a few trusted people (allowlist of identities; per-profile access limits).
- 2026-11-03: Success criteria accepted (7 items: Discord duplex+barge-in, async routed delegation w/ progress, <~300ms routing w/ effort+model+logged rationale, sticky follow-ups, allowlist+per-profile perms, web page parity, provider swap to Grok via config).
- 2026-11-03: Design checkpoint approved; add real e2e against running Hermes (CI tier w/ fake provider+fake LLM; opt-in live tier).
