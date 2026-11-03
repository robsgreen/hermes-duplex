# User decisions (planning conversation)
- 2026-11-03: Discord audio = plugin runs its OWN Discord bot (own token), behind an audio-transport layer; dispatch to Hermes profiles via API server. Swap to shared Hermes bot later if upstream voice hook (RFC #77111) lands.
- 2026-11-03: Must be transport-agnostic: Discord first, also web, and a home smart speaker (likely HTTP-based).
