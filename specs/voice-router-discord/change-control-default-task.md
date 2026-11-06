# Change: default to a task; status/cancel only on strong evidence (user-approved 2026-11-06)

Live test on picard (GPT-Live, router with few profile examples): every hand-off was treated as a status
question ("no tasks running"). Since the active-profile change the router no longer picks the profile, so
the only router decision that can stop a request is `_control`. Cost is asymmetric: a request taken as
status fails silently; a status question taken as a task is a visible wasted run. User rejected a
rules-only router; keep knn / decisions / jev.

## Scope (hermes_duplex/router/composite.py, router/eval.py, config.py if a key is added, tests, docs)
1. `_control` wins only on strong evidence: its probability ≥ a high bar AND beats the best profile by a
   clear margin (named constants, e.g. CONTROL_MIN_PROB / CONTROL_MARGIN; justify values with the eval).
   Otherwise the request is a task on the active profile (control_intent None), whatever the argmax.
2. The fast path (knn) may never settle `_control` alone: early accept is for the active profile only,
   unless the knn match to a control phrase is near-exact (a named similarity bar). Otherwise wait for the
   main path; on main-path timeout/failure, a non-strong `_control` → task.
3. Fallback/no confident decider → task (as today) — verify.
4. Eval: report control precision/recall and "task taken as control" rate (should be ~0) on the fixture;
   add phrases where profiles have few/no examples (a config with only descriptions) so the picard case
   is covered; a unit test reproducing picard: profiles with only a description, "what's the weather
   tomorrow" / "remind me to call mom" → task, "what are you working on" / "cancel that" → control.
5. Docs: README router row + Score the router numbers, architecture.md router policy, live-checklist item 5.
Keep: suggestions rule, effort/model, Decisions/Jev "status when they can't tell" only when _control
passes the strong-evidence bar. Gate + default tests + real-Hermes e2e. Format only changed files.
