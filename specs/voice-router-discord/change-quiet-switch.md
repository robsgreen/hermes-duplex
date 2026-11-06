# Change: explicit profile switches on GPT-Live wait for quiet (user-approved 2026-11-06)
Live test: switching profiles on GPT-Live cuts the audio (restart at once).
- On providers without replaces_instructions, an explicit switch (voice, switch_profile tool, Discord /duplex profile, web picker, suggestion yes) changes the active profile at once (dispatch uses it at once) but the provider restart waits for quiet (same _wait_for_quiet rule as first hearing): the current reply finishes, then restart in the next pause.
- Before closing, the old session says a short line: e.g. notices.switching_to(profile) "Switching you to <profile>." (one wording in prompts/notices.py); then the new session confirms with switched_to as today (or skip the duplicate — pick one confirmation, explain).
- A request made before the restart runs on the new profile (already true); the old prompt is still active until the restart — document.
- Several switches during the wait: only the latest applies; switching back to the session's current profile cancels the pending restart.
- Join-privacy restarts stay immediate (they must not wait). Realtime/Grok unchanged (in place).
- Tests (paced frames; reply in progress not cut; one restart after quiet; line said before close; multiple switches coalesce; privacy restart still immediate), docs (README switching section, architecture, live checklist).
