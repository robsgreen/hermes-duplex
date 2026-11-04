# Test Plan: voice-router-discord — Amendment: Active Profile and Profile Voice Context

> **Source:** `.sdlc/specs/voice-router-discord/amendment-active-profile.md`
> **Created:** 2026-11-04
> **Scope:** Acceptance-level, black-box behaviors of the amendment. Each item says what a person, a provider, Hermes, or a test observes from outside the session. The original `test-plan.md` still applies to everything the amendment does not change. The detailed `test-spec.md` update comes later.

## Tiers

| Tier | Command | What it proves here |
|---|---|---|
| Static checks | `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pyright` | New types and removals type-check (`profile_hint`, `hint`, `sticky` gone) |
| Default (unit, contract, in-process e2e) | `uv run pytest -m "not live and not e2e"` | Store, parser, config, router, session flows, prompts, provider instruction updates, transports |
| Real-Hermes e2e | `PATH=/tmp/claude-0/-home-user/8f7b0a21-796e-5beb-a136-b29bc25c4a84/scratchpad/hermes-venv/bin:$PATH uv run pytest -m e2e` | A voice switch and the next hand-off land on the right Hermes profile; persistence across restart |
| Router eval | `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` | Effort accuracy and suggestion precision are reported |
| Manual | `docs/live-checklist.md` | Real voice models, real Discord |

A Real-Hermes e2e run that skips because `hermes` is not on PATH does not count as a pass.

## Acceptance Items

"Req" names the amendment Requirement (MUST/SHOULD) by its first words.

### P. Active profile store and config

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| P1 | Person with `default_profile` set | Active profile is that profile | Unit | Keep one active profile per person |
| P2 | Person without `default_profile` who may use `router.default_profile` | Active profile is `router.default_profile` | Unit | Keep one active profile |
| P3 | Person without `default_profile` who may not use `router.default_profile` | Active profile is their first permitted profile | Unit | Keep one active profile |
| P4 | Switch, then build a new store on the same file and load it | The switched profile is still active | Unit | Keep one active profile (persist) |
| P5 | `active_profiles.json` corrupt, or missing | Everyone gets defaults; an error is logged; start continues; the file is rewritten on the next switch | Unit | Keep one active profile |
| P6 | Saved profile removed from config, or permission lost | Default is used; "You're back on <default>" is spoken once when the person is first heard | Unit + session | Keep one active profile |
| P7 | Config with `profiles: []` for a person | Config error naming `people.<p>.profiles`; nothing starts | Unit | Keep one active profile (reject empty list) |
| P8 | Config with `default_profile` the person may not use, a duplicate alias, a `voice_context` over 2000 characters, or `suggest_threshold` outside 0 < x ≤ 1 | Config error naming the key | Unit | Config scope; Per-profile `voice_context` |
| P9 | Config that still sets `router.sticky_idle_s` | Config error with a migration message | Unit | Remove `profile_hint` … sticky (Scope) |

### S. Explicit switching

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| S1 | Say "switch to stock-picker", "let me talk to the stock picker" (alias), "go back to general" | Active profile changes; "You're now talking to <profile>." is spoken; no Hermes task; no router call | Unit (session) + contract | Switch only on an explicit request |
| S2 | Say "switch to finance" (unknown) | `which_profile` lists the person's permitted profiles; active profile unchanged | Unit | Switch only on an explicit request |
| S3 | An alias that fits two profiles | `which_profile`; no guess | Unit (parser + session) | Switch only on an explicit request |
| S4 | Switch to a profile the person may not use | `switch_refused`; active profile unchanged | Unit + Real-Hermes e2e (alex) | Switch only on an explicit request |
| S5 | Switch to the already-active profile | `already_active` | Unit | Switch only on an explicit request |
| S6 | Realtime/Grok model calls `switch_profile` | Same outcomes as S1–S5; one function output; no Hermes task | Contract | Switch only on an explicit request |
| S7 | GPT-Live hands off "switch to stock-picker" | Same outcome as S1 through the live hand-off | Contract | Switch only on an explicit request |
| S8 | S1 under router backends knn, decisions, and jev (stubbed) | Same result; no backend is called | In-process e2e | Switch only on an explicit request |
| S9 | Discord `/duplex profile stock-picker` while the person is in a live conversation | Live conversation's instructions update and it says the confirmation; Discord private reply confirms | Unit + in-process | Switch only on an explicit request |
| S10 | Discord `/duplex profile` with no live conversation | Saved only; private reply says it applies next time | Unit | Switch only on an explicit request |
| S11 | Web picker sends `{"type":"profile","name":...}` during a live call | Switched; confirmation spoken; status frame shows the new profile | In-process e2e | Switch only on an explicit request; SHOULD status |
| S12 | Malformed web control frame | Ignored and logged; audio continues | Unit | Scope (web control frame) |
| S13 | "Talk to me about the weather" | Not a switch; handled as a normal request | Unit (parser) | Switch only on an explicit request |

### C. Prompt context and instruction updates

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| C1 | Web conversation starts | First `connect` instructions describe the signed-in person's active profile | Unit (fake provider records `connect`) | Each conversation holds a current `PromptContext` |
| C2 | Discord conversation starts | First `connect` instructions describe the default profile | Unit | Each conversation holds a current `PromptContext` |
| C3 | Discord: a person whose active profile differs is first heard | Instructions change to that person's profile | Unit | Each conversation holds a current `PromptContext` |
| C4 | Someone in the conversation switches | Instructions change to the switcher's profile | Unit | Each conversation holds a current `PromptContext` |
| C5 | Provider drops after a switch and reconnects | Reconnect `connect` instructions describe the switched profile | Unit | Each conversation holds a current `PromptContext` |
| C6 | Realtime or Grok: context change | One `session.update` with `instructions` only; tools still work afterwards | Contract | Update the voice model's instructions |
| C7 | GPT-Live: context change | Provider session closes and reconnects with new instructions and the reconnect summary; a running task's result is still announced; repeated switches do not end the call | Contract | Update the voice model's instructions |
| C8 | Realtime `update_instructions` fails | Error logged; restart path used; call goes on | Unit | Update the voice model's instructions |
| C9 | Session code | No branch on a provider name (`test_session_no_provider_names.py`) | Unit | Update the voice model's instructions (by capability) |

### D. Dispatch to the active profile

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| D1 | Request with no switch | Runs on the person's default profile, session id `duplex-{person}-{profile}` | Unit + Real-Hermes e2e | Dispatch every hand-off |
| D2 | Switch to stock-picker by voice, then "how's my portfolio" | Run starts on `/p/stock-picker` with session id `duplex-rob-stock-picker` | Real-Hermes e2e | Dispatch every hand-off |
| D3 | After D2, restart the service, then a new request | Runs on stock-picker (saved active profile) | Real-Hermes e2e | Keep one active profile (persist) |
| D4 | Two people on one Discord call with different active profiles | Each hand-off runs on its speaker's profile; the voice prompt shows the last switcher's profile | Unit (session) | Dispatch every hand-off; Error Handling |

### R. Router, suggestions, and held requests

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| R1 | Any routed request | Effort from the router; model from the active profile's effort table; active profile never changed by the router | Unit | The router decides effort |
| R2 | Another permitted profile scores ≥ `suggest_threshold` and beats active by ≥ 0.2 | Request held; "That sounds like a <profile> question. Want me to switch?" spoken | Unit | The router decides effort (suggestion) |
| R3 | Below threshold, or margin under 0.2 | No question; dispatched at once | Unit | The router decides effort |
| R4 | `router.suggest_switch: false` | No question ever | Unit | The router decides effort |
| R5 | The best other profile is one the person may not use | Never suggested | Unit | The router decides effort |
| R6 | Answer yes (`confirm_switch(true)` or GPT-Live "yes") | Switched, confirmation spoken, held request runs on the new profile | Unit + contract | The router decides effort |
| R7 | Answer no (`confirm_switch(false)` or GPT-Live "no") | Held request runs on the active profile; no switch | Unit + contract | The router decides effort |
| R8 | No answer in 30 s | Held request runs on the active profile | Unit (test clock) | The router decides effort |
| R9 | New request from the same speaker while one is held | Older runs on active; newer is evaluated (may itself be held) | Unit | The router decides effort |
| R10 | A yes/no hand-off while a hold is pending | Never sent to Hermes as a task | Unit | The router decides effort |
| R11 | `eval-router` on the phrase file (phrases carry `active`) | Prints effort accuracy and suggestion precision | Router eval + unit | Scope (eval) |

### T. Receipt and tool schema

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| T1 | Realtime `delegate` call | Function output `{"status":"received"}` within 50 ms; then a start notice or a switch question | Unit + contract | `DELEGATE_RECEIPT` is `{"status":"received"}` |
| T2 | Realtime tool schema | `delegate(task)` has no `profile_hint`; `switch_profile(profile)` and `confirm_switch(accept)` present | Contract | Remove `profile_hint` |
| T3 | Repo search | No `profile_hint`, `hint` routing, or RouteSource "hint"/"sticky" in `hermes_duplex/` or fixtures; pyright passes | Static | Remove `profile_hint` |

### V. Profile voice context in prompts

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| V1 | Golden instructions per provider (GPT-Live, OpenAI Realtime, xAI) with an active profile that has `voice_context` | Golden files match; an "active profile" section with name, description, voice_context | Unit (golden) | Per-profile `voice_context` |
| V2 | Several profiles with voice contexts | Only the active profile's voice_context appears, exactly once | Unit (structural) | Per-profile `voice_context` |
| V3 | Prompts for each provider | Say how to switch and how to answer a switch question; say a start notice or switch question follows the receipt | Unit | Per-profile `voice_context`; `DELEGATE_RECEIPT` |
| V4 | xAI prompt | Voice-context guidance phrased as rules about words spoken | Unit (golden review) | Per-profile `voice_context` |

### W. Round-7 task-resolution fixes

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| W1 | "cancel the latest one" with two open tasks | `which_to_cancel`; nothing stopped | Unit | Apply the round-7 fixes |
| W2 | "any news about Tesla" | "news" stays a topic word | Unit | Apply the round-7 fixes |
| W3 | "don't", "won't", "never mind", "now", "just" | Add no topic words | Unit | Apply the round-7 fixes |
| W4 | `topic()` for "Could you please find out for me what the weather will be in Paris" vs "…in London" | Two different topics; polite lead-ins stripped | Unit | Apply the round-7 fixes |
| W5 | Resolved task (by id or "task N") not open while others run | `NO_SUCH_TASK`; `NOTHING_TO_CANCEL` only when none are open | Unit | Apply the round-7 fixes |
| W6 | One open task; words name a different topic | "The only one running is <topic>. Cancel that?"; nothing stopped | Unit | Apply the round-7 fixes |
| W7 | One open task; "cancel that" | That task is cancelled | Unit | Apply the round-7 fixes |
| W8 | `_TASK_CHOICE_PARAMETERS` docstring | Says "only open (queued or running) one" | Static (grep) | Apply the round-7 fixes |

### Q. Status

| ID | Behavior | Expected result | Tier | Req |
|---|---|---|---|---|
| Q1 | Hermes `/duplex` status | Lists each person's active profile | Unit | SHOULD: `/duplex status` |
| Q2 | Web status frame on connect and after a switch | Carries the active profile; page picker lists only permitted profiles | Unit + in-process e2e | SHOULD: web status frame and picker |

## Live Checklist Items (manual, `docs/live-checklist.md`)

- [ ] **L1** — GPT-Live: say "switch to stock-picker". Hear the confirmation after about a one-second pause; the next portfolio question runs on stock-picker.
- [ ] **L2** — OpenAI Realtime: the same switch through the `switch_profile` tool; no pause; the confirmation is spoken.
- [ ] **L3** — xAI Grok: the same as L2.
- [ ] **L4** — On general, ask "what did NVDA do today?". Hear "That sounds like a stock-picker question. Want me to switch?". Say yes: switched, and the answer comes from stock-picker. Repeat and say no: the answer comes from general.
- [ ] **L5** — Ask the L4 question and stay silent for 30 s: the request runs on the active profile.
- [ ] **L6** — With stock-picker's `voice_context` set ("say tickers letter by letter"), ask about NVDA: the voice says "N-V-D-A" and does not quote a price from memory.
- [ ] **L7** — Discord `/duplex profile stock-picker` during a call: confirmation spoken in the channel and a private reply. Again with no call: private reply says it applies next time; the next call starts on stock-picker for that person once heard.
- [ ] **L8** — Web page: the picker lists only the signed-in person's permitted profiles; choosing one is confirmed aloud.
- [ ] **L9** — Remove the saved profile from config and restart: "You're back on general" is spoken when the person is first heard.

## Traceability

| Amendment Requirement | Items |
|---|---|
| MUST: Keep one active profile per person … | P1–P7, D3 |
| MUST: Switch only on an explicit request … | S1–S13, L1–L3, L7, L8 |
| MUST: Each conversation holds a current `PromptContext` … | C1–C5, D4 |
| MUST: Update the voice model's instructions … by capability | C6–C9, L1 |
| MUST: Dispatch every hand-off to the speaker's active profile … | D1–D4 |
| MUST: The router decides effort … suggestion … | R1–R11, L4, L5 |
| MUST: `DELEGATE_RECEIPT` is `{"status":"received"}` … | T1, V3 |
| MUST: Remove `profile_hint` … | T2, T3, P9 |
| MUST: Per-profile `voice_context` … | P8, V1–V4, L6 |
| MUST: Apply the round-7 fixes … | W1–W8 |
| SHOULD: `/duplex status` and the web status frame … picker | Q1, Q2, S11 |
| Error Handling: stale profile, corrupt file | P5, P6, L9 |
