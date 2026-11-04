# Task Spec: voice-router-discord — Amendment: Active Profile and Profile Voice Context

> **Source:** `.sdlc/specs/voice-router-discord/amendment-active-profile.md` (amends `master-plan.md`)
> **Created:** 2026-11-04
> **Last Updated:** 2026-11-04
> **Progress:** 0 / 86 sub-tasks complete

**Work path:** `/home/user/hermes-duplex-worktrees/voice-router-discord` (branch `sdlc/voice-router-discord`, draft PR robsgreen/hermes-duplex#1, base HEAD `f09eeb8`). All paths below are relative to this worktree.

**Shared validation commands** (each task's Validation section names which apply):

| Name | Command |
|---|---|
| Gate | `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pyright` |
| Default tests | `uv run pytest -m "not live and not e2e"` |
| Real-Hermes e2e | `PATH=/tmp/claude-0/-home-user/8f7b0a21-796e-5beb-a136-b29bc25c4a84/scratchpad/hermes-venv/bin:$PATH uv run pytest -m e2e` |

Format only the files you change (`uv run ruff format <files>`, AGENTS.md convention 8).

**Run order and file ownership.** Tasks in the same row can run in parallel: their file lists do not overlap. `session.py` has one owner at a time, so the session tasks run in a chain.

| Step | Tasks (parallel) | Waits for |
|---|---|---|
| 1 | 1.1, 1.2, 1.3 | — |
| 2 | 2.1, 2.2 | 1.1 (both), 1.3 (2.2) |
| 3 | 2.3 | 1.3, 2.2 |
| 4 | 3.1 | 1.1, 2.1, 2.2 |
| 5 | 3.2, 3.3 | 3.1 (both), 2.3 (3.3) |
| 6 | 3.4 | 1.2, 2.1, 3.3 |
| 7 | 3.5, 4.1 | 3.4 (both) |
| 8 | 4.2, 4.3 | 3.5, 4.1 (both) |

**Interim state.** Between tasks the build stays green. Task 2.2 offers the `switch_profile` and `confirm_switch` tools before the session handles them (until Task 3.4 and 3.5 the session answers them with `unknown_tool`). Task 3.1 dispatches every hand-off to the speaker's active profile before any switching path exists (until Task 3.4 the active profile is always the default).

---

## Phase 1: Foundations

### Task 1.1: Config keys for aliases, voice context, default profiles, and suggestions

Add the new config keys and checks. Config fails closed, so each key goes into the parser, its dataclass, `duplex.example.yaml`, and the README together (AGENTS.md convention 6). The removal of `router.sticky_idle_s` is not here: `router/composite.py` still reads it, so Task 3.1 removes it.

**Modules:** `.` (single module)
**Depends on:** none
**Owns:** `hermes_duplex/config.py`, `tests/unit/test_config.py`, `tests/fixtures/config/*.yaml`, `duplex.example.yaml`, `README.md` (Configure table only)

**Files:**

```
hermes_duplex/config.py               (modify)
tests/unit/test_config.py             (modify)
tests/fixtures/config/valid.yaml      (modify)
duplex.example.yaml                   (modify)
README.md                             (modify)
```

**Sub-tasks:**

- [ ] **1.1.1** — Add `ProfileConfig.voice_context: str | None` (default None), parsed from `profiles.<p>.voice_context`. A value longer than 2000 characters is a `ConfigError` that names the key.
- [ ] **1.1.2** — Add `ProfileConfig.aliases: tuple[str, ...]` (default empty), parsed from `profiles.<p>.aliases`. An alias used by two profiles is a `ConfigError` that names the alias and both profiles. Compare aliases case-insensitively with whitespace collapsed, because people say them.
- [ ] **1.1.3** — Add `PersonConfig.default_profile: str | None`, parsed from `people.<p>.default_profile`. A profile that is not defined under `profiles`, or that the person may not use (their `profiles` list, `"*"` grants all), is a `ConfigError` that names the key.
- [ ] **1.1.4** — Reject a person whose `profiles` list is empty or missing (`profiles: []`) with a `ConfigError` that names `people.<p>.profiles`.
- [ ] **1.1.5** — Add `RouterConfig.suggest_switch: bool` (default true) and `RouterConfig.suggest_threshold: float` (default 0.85). A threshold that is not in the range 0 < x ≤ 1 is a `ConfigError`.
- [ ] **1.1.6** — Document the new keys in `duplex.example.yaml` (use the `stock-picker` example with `aliases` and `voice_context` from the amendment's Interfaces section) and in the README Configure table.
- [ ] **1.1.7** — Unit tests in `tests/unit/test_config.py`: each new key parsed with its default; `voice_context` of 2001 characters rejected; duplicate alias across two profiles rejected; `default_profile` undefined or not permitted rejected; `profiles: []` rejected; `suggest_threshold` of 0 and 1.5 rejected, 1.0 accepted; unknown keys under the new sections still rejected. Update `tests/fixtures/config/valid.yaml` to set every new key.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; new config tests cover every case in 1.1.7
- [ ] **Requirements:**
    - [ ] "Reject a person with an empty profile list in config." Test: config rejects `profiles: []`.
    - [ ] Scope: `profiles.<p>.voice_context (str, ≤ 2000 chars)`, `profiles.<p>.aliases (list[str], unique across profiles)`, `people.<p>.default_profile (optional, must be permitted)`, `router.suggest_switch (bool, default true)`, `router.suggest_threshold (0<x≤1, default 0.85)`.
    - [ ] "Per-profile `voice_context` is optional, at most 2000 characters."
- [ ] **Verify:** `uv run hermes-duplex doctor --config duplex.example.yaml` loads the example config past the parser (env-var rows may fail; no `ConfigError` for the new keys). `grep -n "voice_context\|aliases\|default_profile\|suggest_switch\|suggest_threshold" README.md duplex.example.yaml` shows each key in both files.

---

### Task 1.2: Switch request parser and yes/no answers

Create the pure helpers the session uses to recognise a spoken switch request and a short yes or no. No I/O and no state.

**Modules:** `.`
**Depends on:** none
**Owns:** `hermes_duplex/switching.py`, `tests/unit/test_switching.py`

**Files:**

```
hermes_duplex/switching.py            (create)
tests/unit/test_switching.py          (create)
```

**Sub-tasks:**

- [ ] **1.2.1** — Implement `parse_switch_request(text: str, names: Mapping[str, Sequence[str]]) -> str | Literal["ambiguous"] | None`. `names` maps each profile name to its aliases. The text is a switch request when it holds a switch phrase ("switch to", "talk to", "back to", "change to") followed by a target. Matching ignores case, punctuation, and extra whitespace, and accepts the words around it ("let me talk to the stock picker", "go back to general").
    - [ ] **1.2.1a** — Resolve the target in Hermes Bot Mode's order: an exact profile name first, then aliases. Return the profile name.
    - [ ] **1.2.1b** — Return `"ambiguous"` when an alias fits two profiles, and when a switch phrase is followed by a target that matches no name or alias ("switch to finance"), so the session can answer with `which_profile`.
    - [ ] **1.2.1c** — Return None when the text holds no switch phrase. Words such as "talk to me about the weather" (the phrase is followed by "me", "us", or "you") are not a switch request and return None.
- [ ] **1.2.2** — Implement `is_affirmative(text) -> bool` and `is_negative(text) -> bool` for short answers only ("yes", "yeah", "sure", "do it", "please do"; "no", "nope", "no thanks", "don't", "stay here"). A long request that starts with "yes" or "no" and carries other content returns False, so it is handled as a new request.
- [ ] **1.2.3** — Unit tests: "switch to stock-picker" → `stock-picker`; "let me talk to the stock picker" (alias) → `stock-picker`; "go back to general" → `general`; "switch to finance" → `"ambiguous"`; an alias configured as an alias-like phrase that fits two profiles → `"ambiguous"`; an exact name wins over another profile's alias; "what's the weather in Paris" → None; "talk to me about Tesla" → None; yes/no tables with positive and negative cases, including mixed case and punctuation.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; `tests/unit/test_switching.py` covers every case in 1.2.3
- [ ] **Requirements:**
    - [ ] "It checks the hand-off text for a switch phrase ("switch to", "talk to", "back to", "change to") followed by a profile's name or one of its spoken aliases from config."
    - [ ] "Name resolution follows Hermes Bot Mode's order: an exact profile name first, then aliases; an alias that fits two profiles is refused with a short list (`which_profile`) instead of a guess."
    - [ ] Interface: `parse_switch_request(text, names) -> str | Literal["ambiguous"] | None`; `is_affirmative(text) -> bool`; `is_negative(text) -> bool`.
- [ ] **Verify:** `grep -n "^import\|^from" hermes_duplex/switching.py` shows only standard-library imports (pure helpers).

---

### Task 1.3: Round-7 task-resolution fixes

Fix how a status or cancel request finds the task it means (review round 7, findings V1–V5, and the default for V6).

**Modules:** `.`
**Depends on:** none
**Owns (this step):** `hermes_duplex/session.py` (only `_FILLER_WORDS`, `_resolve_task`, `_topic_words`, `_status`, `_cancel`), `hermes_duplex/prompts/notices.py`, `hermes_duplex/providers/realtime/adapter.py` (docstring only), `tests/unit/test_session.py`, `tests/unit/test_prompts.py`

**Files:**

```
hermes_duplex/session.py                         (modify)
hermes_duplex/prompts/notices.py                 (modify)
hermes_duplex/providers/realtime/adapter.py      (modify)
tests/unit/test_session.py                       (modify)
tests/unit/test_prompts.py                       (modify)
```

**Sub-tasks:**

- [ ] **1.3.1** — Ordinal and recency words ("latest", "last", "newest", "new", "first", "second", "other", and similar) add no topic words, so "cancel the latest one" with two open tasks asks `which_to_cancel` and stops nothing.
- [ ] **1.3.2** — Stop the plural strip from turning "news" into "new": "any news about Tesla" keeps "news" as a topic word.
- [ ] **1.3.3** — Contraction and dismissal words add no topic words: "don't", "won't", "never mind", "now", "just" (no "don", "won", "never", "mind", "now", "just").
- [ ] **1.3.4** — `notices.topic()` strips polite lead-ins ("could you please", "can you", "find out for me", "tell me", and similar) before it applies `TOPIC_MAX_CHARS`, so "Could you please find out for me what the weather will be in Paris" and "…in London" give two different topics.
- [ ] **1.3.5** — In `_cancel` and `_status`: a task resolved by `task_id` or "task N" that is not open, while other tasks are open → `NO_SUCH_TASK`. `NOTHING_TO_CANCEL` only when the person has no open task.
- [ ] **1.3.6** — With exactly one open task: when the words carry topic words and none overlap that task, cancel stops nothing and asks with a new notice in `notices.py` worded "The only one running is <topic>. Cancel that?". When the words carry no topic words ("cancel that"), cancel it as today. This is the default while the user decision on V6 is open.
- [ ] **1.3.7** — Change the `_TASK_CHOICE_PARAMETERS` docstring in `providers/realtime/adapter.py` to say "only open (queued or running) one".
- [ ] **1.3.8** — Unit tests, one per case in the Requirements list below, in `tests/unit/test_session.py` (resolver and cancel) and `tests/unit/test_prompts.py` (`topic()`).

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; one new test per round-7 case
- [ ] **Requirements:**
    - [ ] "cancel the latest one" with two open tasks → `which_to_cancel`, nothing stopped.
    - [ ] "any news about Tesla" keeps "news" as a topic word (no plural strip to "new").
    - [ ] "don't", "won't", "never mind", "now", "just" add no topic words.
    - [ ] `topic()` for "Could you please find out for me what the weather will be in Paris" vs "…in London" → two different topics; polite lead-ins are stripped.
    - [ ] A resolved task (by `task_id` or "task N") that is not open, while others are running → `NO_SUCH_TASK`. `NOTHING_TO_CANCEL` only when none are open.
    - [ ] One open task, and the words carry topic words that don't overlap it → cancel asks "The only one running is <topic>. Cancel that?" and stops nothing.
    - [ ] One open task, and the words carry no topic words ("cancel that") → cancels it.
    - [ ] The `_TASK_CHOICE_PARAMETERS` docstring says "only open (queued or running) one".
- [ ] **Verify:** `grep -n "only open (queued or running) one" hermes_duplex/providers/realtime/adapter.py` matches; `git diff --stat` shows no other change in `adapter.py`.

---

## Phase 2: Store, providers, and prompts

### Task 2.1: ActiveProfiles store

Create the per-person active-profile store with persistence and switch notifications. The session, the Discord bot, and the web transport use it in later tasks.

**Modules:** `.`
**Depends on:** 1.1 (`PersonConfig.default_profile`, non-empty `profiles`)
**Owns:** `hermes_duplex/profiles.py`, `tests/unit/test_profiles.py`

**Files:**

```
hermes_duplex/profiles.py             (create)
tests/unit/test_profiles.py           (create)
```

**Sub-tasks:**

- [ ] **2.1.1** — Implement `ActiveProfiles(path, permissions, profiles, default_profile)` with the interface in the amendment (`SwitchOutcome`, `get`, `fallback_pending`, `switch`, `subscribe`, `load`).
- [ ] **2.1.2** — `get(person)`: the saved profile when it is still configured and permitted; otherwise the default, resolved as `people.<p>.default_profile`, else `router.default_profile` if the person may use it, else the person's first permitted profile (config order; `"*"` means every profile in config order).
- [ ] **2.1.3** — `load()`: read `$HERMES_HOME/duplex/active_profiles.json` (the `path` argument). A missing, unreadable, or corrupt file gives defaults for everyone, logs an error, and never raises. A saved profile that was removed from config or is no longer permitted is replaced by the default and recorded for `fallback_pending`.
- [ ] **2.1.4** — `fallback_pending(person)`: the default profile to announce once after a fallback, then None.
- [ ] **2.1.5** — `switch(person, profile)`: `"unknown"` for a profile not in config; `"refused"` for one the person may not use; `"already_active"` for the current one; otherwise save with an atomic write (temporary file, then replace), notify every listener subscribed for that person with the new profile, and return `"switched"`. A listener that raises is logged and does not stop the others or the switch.
- [ ] **2.1.6** — `subscribe(person, listener)` returns an unsubscribe callable; several listeners per person are allowed.
- [ ] **2.1.7** — Unit tests: default resolution, all three steps; persistence round trip (switch, new store on the same path, `load`, `get`); corrupt file and missing file → defaults, an error in the log, no exception; saved profile removed or no longer permitted → default and `fallback_pending` returns it once; each `SwitchOutcome`; listeners of that person notified and others not; unsubscribe works; a failing listener does not block the switch.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; `tests/unit/test_profiles.py` covers every case in 2.1.7
- [ ] **Requirements:**
    - [ ] "Keep one active profile per person, resolved as `people.<p>.default_profile`, else `router.default_profile` if the person may use it, else their first permitted profile. Persist it across conversations and restarts."
    - [ ] Tests: default resolution, all three steps; persistence round trip; a corrupt or missing file → defaults, an error in the log, and start continues; a saved profile that was removed or is no longer permitted → default, plus `fallback_to_default` pending for when the person is first heard.
    - [ ] Error handling: "active_profiles.json corrupt/unreadable → Defaults for everyone; error logged; start continues; rewritten on next switch".
- [ ] **Verify:** `grep -n "os.replace\|\.replace(" hermes_duplex/profiles.py` shows the atomic write; `tests/unit/test_profiles.py` has a test that asserts the error log record on a corrupt file.

---

### Task 2.2: Provider capability, instruction updates, new tools, and the received receipt

Give providers the `replaces_instructions` capability and `update_instructions`, change the `delegate` receipt, add the two new tools, and remove `profile_hint` from the tool and fixtures. The session does not use these yet.

**Modules:** `.`
**Depends on:** 1.1 (`config.py`), 1.3 (`providers/realtime/adapter.py`)
**Owns:** `hermes_duplex/providers/base.py`, `hermes_duplex/providers/realtime/adapter.py`, `hermes_duplex/providers/realtime/openai/dialect.py`, `hermes_duplex/providers/realtime/xai/dialect.py`, `hermes_duplex/providers/gpt_live/adapter.py`, `hermes_duplex/providers/fake/adapter.py`, `hermes_duplex/config.py` (`_capabilities` only), and the tests below

**Files:**

```
hermes_duplex/providers/base.py                        (modify)
hermes_duplex/providers/realtime/adapter.py            (modify)
hermes_duplex/providers/realtime/openai/dialect.py     (modify)
hermes_duplex/providers/realtime/xai/dialect.py        (modify)
hermes_duplex/providers/gpt_live/adapter.py            (modify)
hermes_duplex/providers/fake/adapter.py                (modify)
hermes_duplex/config.py                                (modify)
tests/contract/test_realtime_adapter.py                (modify)
tests/contract/test_realtime_session.py                (modify)
tests/unit/test_fake_provider.py                       (modify)
tests/unit/test_config.py                              (modify)
tests/fixtures/provider_scripts/realtime_delegation.jsonl  (modify)
```

**Sub-tasks:**

- [ ] **2.2.1** — In `providers/base.py`: add `ProviderCapabilities.replaces_instructions: bool`; add `RealtimeProvider.update_instructions(text)` (called only when `replaces_instructions`); set `DELEGATE_RECEIPT = {"status": "received"}` and update its docstring; add `SWITCH_PROFILE_TOOL = "switch_profile"` and `CONFIRM_SWITCH_TOOL = "confirm_switch"`; remove `profile_hint` from the `DELEGATE_TOOL` docstring.
- [ ] **2.2.2** — Set `replaces_instructions` to true in the OpenAI and xAI dialects and false in GPT-Live's `CAPABILITIES`. GPT-Live's `update_instructions` sends nothing and raises an error that names the capability.
- [ ] **2.2.3** — In `providers/realtime/adapter.py` `session_tools`: `delegate(task)` with no `profile_hint`; add `switch_profile(profile)` whose `profile` is a string `enum` of the configured profile names from `ctx.profiles`, with a description that says the user may name a profile by a spoken alias and the model passes the profile name; add `confirm_switch(accept: boolean, required)` whose description says it answers the assistant's question about switching. Update the `delegate` description so it no longer says the task "started".
- [ ] **2.2.4** — `RealtimeApiProvider.update_instructions(text)` sends `session.update` with `instructions` only (no `tools`, no audio settings) in the dialect's session shape.
- [ ] **2.2.5** — Fake provider: `DEFAULT_CAPABILITIES` gains `replaces_instructions` (true, as for the Realtime family); the capability is overridable from config (`config.py` `_capabilities` accepts it); `update_instructions` is recorded as a `SentCall` and in `record_path` like the other calls.
- [ ] **2.2.6** — Remove `profile_hint` from `tests/fixtures/provider_scripts/realtime_delegation.jsonl` and from the tool-schema and session contract tests that use it (`tests/contract/test_realtime_adapter.py`, `tests/contract/test_realtime_session.py`).
- [ ] **2.2.7** — Contract tests: the `delegate` schema has no `profile_hint`; `switch_profile` and `confirm_switch` schemas as in 2.2.3; for the OpenAI and xAI dialects against the fake WebSocket server, `update_instructions` sends one `session.update` whose `session` holds the new `instructions` and no `tools` key, and a later function call still works (the tools stay in place). Unit tests: the fake records `update_instructions`; config accepts the `replaces_instructions` override.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; new contract tests per 2.2.7
- [ ] **Requirements:**
    - [ ] Scope: `ProviderCapabilities.replaces_instructions (bool)`; `RealtimeProvider.update_instructions(text)`; `DELEGATE_RECEIPT`; tool name constants `SWITCH_PROFILE_TOOL`, `CONFIRM_SWITCH_TOOL`.
    - [ ] "`DELEGATE_RECEIPT` is `{"status":"received"}`."
    - [ ] "Remove `profile_hint` from the `delegate` tool and fixtures." Test: "the tool schema has no `profile_hint`, and pyright passes."
    - [ ] Contract: "Realtime and Grok send `session.update` with the new instructions, and the tools stay in place."
    - [ ] Realtime tools: `delegate(task)`, `task_status`, `cancel_task`, `switch_profile(profile: enum of configured profile names + "aliases accepted")`, `confirm_switch(accept: bool)`.
- [ ] **Verify:** `grep -rn "profile_hint" hermes_duplex/providers/realtime/adapter.py hermes_duplex/providers/base.py tests/fixtures/provider_scripts/` returns nothing.

---

### Task 2.3: Prompt context, voice-context rendering, and switch notices

Teach every prompter about the active profile and its voice context, add the switch notices, and update the goldens.

**Modules:** `.`
**Depends on:** 1.3 (`prompts/notices.py`), 2.2 (tool name constants and receipt)
**Owns:** `hermes_duplex/prompts/*`, `hermes_duplex/providers/gpt_live/prompt.py`, `hermes_duplex/providers/realtime/openai/prompt.py`, `hermes_duplex/providers/realtime/xai/prompt.py`, `tests/unit/test_prompts.py`, `tests/unit/test_prompt_golden.py`, `tests/fixtures/prompts/**`

**Files:**

```
hermes_duplex/prompts/base.py                          (modify)
hermes_duplex/prompts/blocks.py                        (modify)
hermes_duplex/prompts/notices.py                       (modify)
hermes_duplex/providers/gpt_live/prompt.py             (modify)
hermes_duplex/providers/realtime/openai/prompt.py      (modify)
hermes_duplex/providers/realtime/xai/prompt.py         (modify)
tests/unit/test_prompts.py                             (modify)
tests/unit/test_prompt_golden.py                       (modify)
tests/fixtures/prompts/gpt-live/*                      (modify)
tests/fixtures/prompts/openai-realtime/*               (modify)
tests/fixtures/prompts/xai-realtime/*                  (modify)
```

**Sub-tasks:**

- [ ] **2.3.1** — `PromptContext` gains `active_profile: str | None = None`, `active_profile_description: str | None = None`, and `voice_context: str | None = None` (the amendment's Scope names all three).
- [ ] **2.3.2** — Add notices in `prompts/notices.py` (one wording for all providers): `switched_to(profile)` ("You're now talking to <profile>."), `switch_refused(profile)`, `already_active(profile)`, `which_profile(permitted)` (lists the permitted names), `suggest_switch(profile)` ("That sounds like a <profile> question. Want me to switch?"), `fallback_to_default(profile)` ("You're back on <profile>."), and `no_profiles_allowed`.
- [ ] **2.3.3** — Add shared rules to `prompts/blocks.py`: the `delegate` receipt means the request was received, and a start notice or a question about switching follows; how a switch question is answered.
- [ ] **2.3.4** — GPT-Live prompter: an "active profile" section (name, description, voice_context) in its format; instructions to hand off any request to change who the user talks to, and to hand off the user's yes or no answer to a switch question; no `profile_hint`.
- [ ] **2.3.5** — OpenAI Realtime prompter: the "active profile" section; call `switch_profile` when the user asks to switch; call `confirm_switch` with the user's answer to a switch question; receipt wording per 2.3.3; remove `profile_hint`.
- [ ] **2.3.6** — xAI Grok prompter: the same as 2.3.5, with the voice context and pronunciation guidance phrased as rules about the words spoken, not about sound (xAI prompting guide).
- [ ] **2.3.7** — Each prompter renders `voice_context` only for the context's active profile, exactly once, and renders no "active profile" section when `active_profile` is None.
- [ ] **2.3.8** — Tests: the golden context gains an active profile with a `voice_context` (regenerate with `HERMES_DUPLEX_UPDATE_PROMPTS=1 uv run pytest tests/unit/test_prompt_golden.py` and review the diff under `tests/fixtures/prompts/<provider>/`); a structural test per prompter that the active profile's `voice_context` appears exactly once and no other profile's voice context appears; tests that GPT-Live and Realtime/Grok prompts say how to switch and how to answer a switch question; the existing backtick-name test in `test_prompts.py` no longer expects `profile_hint` and expects `switch_profile`, `confirm_switch`, `accept`, `profile` where used; a test per new notice.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; goldens regenerated and reviewed; structural and notice tests per 2.3.8
- [ ] **Requirements:**
    - [ ] "Each prompter renders it only for the current context's active profile, in an "active profile" section in its vendor's format (name, description, voice_context)."
    - [ ] "Golden tests per provider with an active profile that has voice_context."
    - [ ] "A structural test: the voice_context appears exactly once, and no other profile's voice_context appears."
    - [ ] "GPT-Live and Realtime/Grok prompts instruct how to switch and how to answer a switch question."
    - [ ] "The prompts say that a start notice, or a question about switching, follows."
    - [ ] Notices: `switched_to`, `switch_refused`, `already_active`, `which_profile` (lists permitted), `suggest_switch`, `fallback_to_default`, `no_profiles_allowed`.
    - [ ] "On xAI it is phrased as rules about the words spoken, not about sound."
- [ ] **Verify:** `grep -rn "profile_hint" hermes_duplex/ tests/fixtures/prompts/` returns nothing except `hermes_duplex/session.py` (removed in Task 3.1); `git diff --stat tests/fixtures/prompts/` shows only `instructions.txt` files changed.

---

## Phase 3: Session integration

### Task 3.1: Router reduced to effort and suggestion; dispatch to the active profile

The router stops choosing a profile. It returns effort, the model from the active profile's table, an optional suggestion, and a control intent. The session dispatches every hand-off to the speaker's active profile from the store. Sticky routing, `hint`, and `router.sticky_idle_s` are removed.

**Modules:** `.`
**Depends on:** 1.1, 2.1, 2.2
**Owns:** `hermes_duplex/router/base.py`, `composite.py`, `decisions.py`, `jev.py`, `knn.py`, `hermes_duplex/session.py`, `hermes_duplex/service.py`, `hermes_duplex/config.py`, and the tests below. Does not touch `router/eval.py` beyond what pyright needs (Task 3.2 owns its logic).

**Files:**

```
hermes_duplex/router/base.py                     (modify)
hermes_duplex/router/composite.py                (modify)
hermes_duplex/router/decisions.py                (modify)
hermes_duplex/router/jev.py                      (modify)
hermes_duplex/router/knn.py                      (modify)
hermes_duplex/router/eval.py                     (modify)
hermes_duplex/session.py                         (modify)
hermes_duplex/service.py                         (modify)
hermes_duplex/config.py                          (modify)
duplex.example.yaml                              (modify)
README.md                                        (modify)
tests/fixtures/config/valid.yaml                 (modify)
tests/unit/test_config.py                        (modify)
tests/unit/test_router_composite.py              (modify)
tests/unit/test_router_knn.py                    (modify)
tests/unit/test_router_factory.py                (modify)
tests/contract/test_router_decisions.py          (modify)
tests/contract/test_router_jev.py                (modify)
tests/unit/test_session.py                       (modify)
tests/unit/test_service_lifecycle.py             (modify)
tests/contract/test_realtime_session.py          (modify)
tests/contract/test_gpt_live_session.py          (modify)
tests/e2e/test_router_backends_flow.py           (modify)
tests/e2e/test_web_fake_provider.py              (modify)
tests/e2e/test_web_service_flows.py              (modify)
tests/e2e/test_real_hermes.py                    (modify)
tests/e2e/test_real_hermes_flows.py              (modify)
```

**Sub-tasks:**

- [ ] **3.1.1** — `router/base.py`: `RouteDecision(effort, model, suggestion, control_intent, probabilities, source, latency_ms)` as in the amendment's Interfaces; `RouteSource` drops `"hint"` and `"sticky"`; `RouteRequest` drops `hint` (keeps `active_profile`, now always the speaker's active profile).
- [ ] **3.1.2** — `CompositeRouter.route(text, speaker, *, active_profile, permitted, history)`: remove the per-speaker sticky state, the leave margins, and the hint fallback. Keep the deadline, the circuit breaker, the fast path, and the fallback. A confident `_control` winner returns `control_intent` (`status` or `cancel`) and no suggestion. The effort comes from the deciding backend (default effort on fallback); `model` comes from the active profile's effort table.
- [ ] **3.1.3** — Suggestion rule: return `suggestion=<profile>` only when `suggest_switch` is true, the best other profile the person may use (`permitted`) has probability ≥ `suggest_threshold`, and it beats the active profile by ≥ 0.2. Never suggest the active profile or a profile the person may not use. Update the `route` decision log line (no `sticky`, add `suggestion`).
- [ ] **3.1.4** — Drop `hint` from the Decisions, Jev, and knn request handling; keep their scoring. Keep `router/eval.py` compiling with the new types (its metrics change in Task 3.2).
- [ ] **3.1.5** — Remove `RouterConfig.sticky_idle_s`. The key in a config file is a `ConfigError` with a migration message (for example: "router.sticky_idle_s was removed: each person now keeps an active profile until they switch"). Remove it from `tests/fixtures/config/valid.yaml`, `duplex.example.yaml`, and the README router row.
- [ ] **3.1.6** — `session.py`: the `Router` protocol takes the new signature; `VoiceSession` takes the `ActiveProfiles` store; `_delegate` drops `hint`, reads `profiles.get(speaker)`, routes with it as `active_profile`, answers control intents locally as today, checks permission against the active profile (refusal notice when not permitted), and creates the task on the active profile with session id `duplex-{person}-{profile}`. `_handle_tool_call` no longer reads `profile_hint`. The decision's `suggestion` is ignored until Task 3.5.
- [ ] **3.1.7** — `service.py`: build `ActiveProfiles` at `$HERMES_HOME/duplex/active_profiles.json` from config, call `load()` in `start()` before transports start, and pass it to every `VoiceSession`.
- [ ] **3.1.8** — Tests: composite tests rewritten for effort, model from the active profile's table, control intents, breaker, deadline, and fallback with no sticky or hint cases; suggestion cases (a confident other profile → suggestion; below threshold → none; margin below 0.2 → none; `suggest_switch: false` → none; a profile the person may not use → never suggested); config rejects `sticky_idle_s` with the migration message; session dispatch runs on the speaker's active profile with session id `duplex-{person}-{profile}`; `tests/e2e/test_router_backends_flow.py` "falls to the hint else active else default" becomes "every backend failing → default effort, dispatched to the active profile". Update real-Hermes e2e expectations: a request with no switch runs on the person's default profile (switching e2e comes in Task 4.2).

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; Real-Hermes e2e passes (or skips only because `hermes` is missing from PATH, which must be reported)
- [ ] **Requirements:**
    - [ ] "Dispatch every hand-off to the speaker's active profile, with session id `duplex-{person}-{profile}`. Check permission against that profile." "A request with no switch runs on the person's default."
    - [ ] "The router decides effort, and the model comes from the active profile's table. It may also return a `suggestion` and never changes the active profile."
    - [ ] "When it suggests: `suggest_switch` is true, the best other profile the person may use has probability ≥ `suggest_threshold`, and it beats the active profile by ≥ 0.2." Tests: suggestions off → no question; a profile the person may not use is never suggested.
    - [ ] "Remove … `hint` from routing, and RouteSource "hint" and "sticky"."
    - [ ] Scope: "remove router.sticky_idle_s (reject with a migration message)"; "remove sticky state, `hint`"; "keep deadline/breaker/fallback".
- [ ] **Verify:** `grep -rn "sticky\|hint" hermes_duplex/router/ hermes_duplex/session.py hermes_duplex/config.py` returns no match for sticky routing or the hint (matches inside unrelated words must be explained in the coder's report).

---

### Task 3.2: Router eval: effort accuracy and suggestion precision

Change `eval-router` to measure what the router now decides. Runs in parallel with Task 3.3.

**Modules:** `.`
**Depends on:** 3.1
**Owns:** `hermes_duplex/router/eval.py`, `hermes_duplex/cli.py` (eval output only), `tests/unit/test_router_eval.py`, `tests/fixtures/router_eval/phrases.yaml`

**Files:**

```
hermes_duplex/router/eval.py                     (modify)
hermes_duplex/cli.py                             (modify)
tests/unit/test_router_eval.py                   (modify)
tests/fixtures/router_eval/phrases.yaml          (modify)
```

**Sub-tasks:**

- [ ] **3.2.1** — Each labelled phrase gains an `active` profile (the speaker's active profile when they say it). The file format rejects a phrase without `active` or with an undefined one, and its header comment explains the field.
- [ ] **3.2.2** — `eval.py` reports effort accuracy and suggestion precision (of the suggestions made, the share whose suggested profile is the labelled profile and differs from `active`), plus how many suggestions were made. Remove sticky and hint handling (`hint=None`, sticky follow-up logic) from `_run_backend` and `_run_composite`.
- [ ] **3.2.3** — `format_table` and `to_json` show the new metrics; `eval-router` prints them.
- [ ] **3.2.4** — Unit tests: a small phrase set with known outcomes gives the expected effort accuracy and suggestion precision; a phrase whose labelled profile equals `active` counts no suggestion as correct; format and JSON output carry the new fields.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; eval tests per 3.2.4
- [ ] **Requirements:**
    - [ ] Scope: "eval: effort accuracy and suggestion precision on labelled phrases that carry an `active` profile"; "router_eval phrases gain `active`".
- [ ] **Verify:** `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` exits 0 and prints effort accuracy and suggestion precision; record the numbers in the coder's report for Task 4.3's README update.

---

### Task 3.3: Per-conversation prompt context and instruction updates by capability

Each conversation holds its own current `PromptContext`, used on every connect and reconnect. A change of context updates the voice model's instructions by capability.

**Modules:** `.`
**Depends on:** 2.3, 3.1
**Owns:** `hermes_duplex/session.py`, `hermes_duplex/service.py`, `hermes_duplex/transports/base.py`, `hermes_duplex/transports/web/server.py`, `hermes_duplex/transports/discord/bot.py` (conversation `person` only), and the tests below

**Files:**

```
hermes_duplex/session.py                         (modify)
hermes_duplex/service.py                         (modify)
hermes_duplex/transports/base.py                 (modify)
hermes_duplex/transports/web/server.py           (modify)
hermes_duplex/transports/discord/bot.py          (modify)
tests/unit/test_session_reconnect.py             (modify)
tests/unit/test_session.py                       (modify)
tests/unit/test_web_transport.py                 (modify)
tests/unit/test_discord_conversation.py          (modify)
tests/contract/test_gpt_live_session.py          (modify)
tests/contract/test_realtime_session.py          (modify)
tests/fakes/*                                    (modify, where a fake conversation lives)
```

**Sub-tasks:**

- [ ] **3.3.1** — `Conversation.person: str | None` in `transports/base.py`. `WebConversation.person` becomes the signed-in person's name (today it holds the `Person` object; keep the object under another attribute). `DiscordConversation.person` is None. Every test fake conversation gains the attribute.
- [ ] **3.3.2** — `service.py` builds the `PromptContext` for a given active profile: the base context (`prompt_context(config)`) plus `active_profile`, that profile's description, and its `voice_context`. The session receives what it needs to build the context for any profile.
- [ ] **3.3.3** — Initial context in the session: for a conversation with a `person` (web), that person's active profile from the store; otherwise (Discord) the default profile (`router.default_profile`). `_connect` renders `prompter.instructions(<current context>)` on the first connect and on every reconnect.
- [ ] **3.3.4** — When a person is first heard and their active profile differs from the current context, the context changes to their profile (Discord rule).
- [ ] **3.3.5** — A context change, by capability: when `replaces_instructions`, call `update_instructions(prompter.instructions(ctx))`; when that call fails, log it and fall back to the restart path. Otherwise restart: close the provider session and reconnect through the reconnect path with fresh instructions and the reconnect summary (`prompter.reseed`). A restart does not count toward `MAX_SHORT_PROVIDER_SESSIONS` or reconnect failures, and running Hermes tasks continue and report to the new provider session. The session branches on the capability only, never on a provider name.
- [ ] **3.3.6** — Tests: initial context per transport (web person's active profile; Discord default); an update on first hearing; a reconnect after a context change renders the new profile (the fake provider records `connect` instructions); Realtime/Grok contract: a context change sends `session.update` with the new instructions only; GPT-Live contract: a context change closes and reconnects with the new instructions and the reconnect summary, does not end the conversation after `MAX_SHORT_PROVIDER_SESSIONS` changes in a row, and a running task's result is still announced afterwards; `update_instructions` failure → restart path.

**Validation:**

- [ ] **Build:** Gate exits 0; `tests/unit/test_session_no_provider_names.py` passes
- [ ] **Tests:** Default tests pass; new tests per 3.3.6
- [ ] **Requirements:**
    - [ ] "Each conversation holds a current `PromptContext`, which every connect and reconnect renders." Initial: web → the signed-in person's active profile; Discord → the default profile. Updates: when a person is first heard and their active profile differs.
    - [ ] Tests: initial context per transport; an update on first hearing; a reconnect after a switch renders the new profile (fake provider records `connect` instructions).
    - [ ] "`replaces_instructions` → `update_instructions(prompter.instructions(ctx))`, which sends `session.update` with `instructions` only; otherwise → restart the provider session with fresh instructions and the reconnect summary. This reuses the reconnect path, does not count toward the short-session cap or failures, and running Hermes tasks continue."
    - [ ] Contract tests: "Realtime and Grok send `session.update` with the new instructions, and the tools stay in place"; "GPT-Live closes and reconnects with the new instructions, and a running task is still announced afterwards."
    - [ ] Error handling: "update_instructions fails (Realtime/Grok) → Log; fall back to restart path."
    - [ ] Scope: `Conversation.person (the signed-in person, web) or None (Discord)`.
- [ ] **Verify:** `grep -n "prompt_context" hermes_duplex/session.py` shows the session no longer renders `self._provider.prompt_context` on connect; it renders its own current context.

---

### Task 3.4: Switching in the session

Handle every voice switch path in the session, react to store notifications, and announce fallbacks. A switch never reaches the router or Hermes.

**Modules:** `.`
**Depends on:** 1.2, 2.1, 3.3
**Owns:** `hermes_duplex/session.py`, `tests/unit/test_session_switching.py`, `tests/e2e/test_router_backends_flow.py`, `tests/contract/test_realtime_session.py`, `tests/contract/test_gpt_live_session.py`

**Files:**

```
hermes_duplex/session.py                         (modify)
tests/unit/test_session_switching.py             (create)
tests/contract/test_realtime_session.py          (modify)
tests/contract/test_gpt_live_session.py          (modify)
tests/e2e/test_router_backends_flow.py           (modify)
```

**Sub-tasks:**

- [ ] **3.4.1** — Subscribe to the store for each person the session hears (and the conversation's `person` at start); unsubscribe when the session ends. On a notification: change the prompt context to the new profile (Task 3.3 path; for Discord "the profile of whoever switched last") and announce `switched_to(profile)`. Each switch is confirmed once per conversation.
- [ ] **3.4.2** — `switch_profile` tool call: resolve the argument (exact profile name, then alias, as `parse_switch_request` does), call `store.switch(speaker, profile)`, and answer with one function output that carries the outcome. Spoken result: `"switched"` → the listener's `switched_to`; `"already_active"` → `already_active`; `"refused"` → `switch_refused` (active unchanged); `"unknown"` or ambiguous → `which_profile` listing the profiles the speaker may use. No router call and no Hermes task.
- [ ] **3.4.3** — Hand-off text (a `delegate` task text and a live delegation's words) runs through `parse_switch_request` with each profile's name and aliases, before routing. A target → the same handling as 3.4.2; `"ambiguous"` → `which_profile`; None → continue to routing. The `delegate` receipt still goes out first.
- [ ] **3.4.4** — When a person is first heard and `store.fallback_pending(person)` returns a profile, announce `fallback_to_default(profile)` once.
- [ ] **3.4.5** — Unit tests in `tests/unit/test_session_switching.py`: "switch to stock-picker", "let me talk to the stock picker" (alias), "go back to general" → switched and `switched_to`; "switch to finance" → `which_profile`, active unchanged; a profile the person may not use → `switch_refused`, active unchanged; the already-active profile → `already_active`; a switch request never reaches the router or Hermes; `switch_profile` tool path; a store notification from outside (Discord or web) with a live conversation → instructions updated and confirmation spoken; `fallback_to_default` spoken once when the person is first heard. Contract tests: the same switch over the Realtime tool and over a GPT-Live live hand-off. In `tests/e2e/test_router_backends_flow.py`: the same switch result under backends knn, decisions, and jev (stubbed), with no router call recorded.

**Validation:**

- [ ] **Build:** Gate exits 0; `tests/unit/test_session_no_provider_names.py` passes
- [ ] **Tests:** Default tests pass; Real-Hermes e2e passes; new tests per 3.4.5
- [ ] **Requirements:**
    - [ ] "Switch only on an explicit request." Paths: the `switch_profile` tool (Realtime and Grok); a hand-off whose text `parse_switch_request` recognises (GPT-Live, and as a fallback for any provider). "Every live conversation for that person confirms aloud, and none of these paths creates a Hermes task."
    - [ ] Tests: switched / `switched_to`; unknown → `which_profile`; not permitted → `switch_refused`; already active → `already_active`; "A switch request never reaches the router or Hermes"; "The same result under router backends knn, decisions and jev (stubbed)"; "A Discord or web switch with a live conversation → instructions updated and confirmation spoken."
    - [ ] Prompt context updates "when that person, or anyone in the conversation, switches."
    - [ ] "a saved profile that was removed or is no longer permitted → default, plus `fallback_to_default` spoken when the person is first heard."
- [ ] **Verify:** In the default test log, a switch test shows no `route ...` log line and no fake-Hermes run for the switch request.

---

### Task 3.5: Switch suggestions and held requests

When the router suggests another profile, the session holds the request and asks. The answer comes through `confirm_switch` or a short live yes/no hand-off. Runs in parallel with Task 4.1.

**Modules:** `.`
**Depends on:** 1.2, 3.4
**Owns:** `hermes_duplex/session.py`, `tests/unit/test_session_suggestions.py`, `tests/contract/test_realtime_session.py`, `tests/contract/test_gpt_live_session.py`

**Files:**

```
hermes_duplex/session.py                         (modify)
tests/unit/test_session_suggestions.py           (create)
tests/contract/test_realtime_session.py          (modify)
tests/contract/test_gpt_live_session.py          (modify)
```

**Sub-tasks:**

- [ ] **3.5.1** — On a decision with a `suggestion`: hold the request for that speaker (one hold per speaker) and announce `suggest_switch(profile)`. Without a suggestion, dispatch at once as today.
- [ ] **3.5.2** — A newer request from the same speaker while one is held: the older held request runs on the active profile, and the newer request is evaluated as usual.
- [ ] **3.5.3** — Resolve a hold: `confirm_switch(true)`, or a live hand-off from that speaker that `is_affirmative` matches while a hold is pending → `store.switch` to the suggested profile, then run the held request on it (the routed effort, with the model from the new profile's effort table); `confirm_switch(false)` or `is_negative` → run it on the active profile. A yes or no hand-off is checked before switch parsing and routing, and never reaches Hermes as a task. A `confirm_switch` call with no hold pending is answered with a function output that says nothing is waiting.
- [ ] **3.5.4** — No answer within 30 seconds (on the session's `clock`/`sleep`, so tests do not wait) → run the held request on the active profile. Pending holds end when the session ends: each runs on the active profile or is dropped, consistently with how open work is left today; state the choice in the coder's report.
- [ ] **3.5.5** — Tests in `tests/unit/test_session_suggestions.py`: a confident other profile → ask; yes → switch and run there; no → run on active; timeout → run on active; a newer request while one is held → the older runs on active and the newer is evaluated; suggestions off → no question; a profile the person may not use is never suggested (router stub plus session); the `delegate` receipt `{"status":"received"}` arrives within 50 ms and the start notice or the switch question follows. Contract tests: `confirm_switch` over the Realtime tool; a yes/no live hand-off over GPT-Live.

**Validation:**

- [ ] **Build:** Gate exits 0; `tests/unit/test_session_no_provider_names.py` passes
- [ ] **Tests:** Default tests pass; Real-Hermes e2e passes; new tests per 3.5.5
- [ ] **Requirements:**
    - [ ] "On a suggestion: the session holds the request (one per speaker; a newer request replaces an older hold, which then runs on the active profile) and speaks `suggest_switch(profile)`."
    - [ ] "`confirm_switch(true)`, or a live hand-off that `is_affirmative` matches → switch, then run the held request on the new profile; `confirm_switch(false)` or `is_negative` → run it on the active profile; no answer within 30 s → run it on the active profile. Without a suggestion: dispatch at once."
    - [ ] Tests: ask; yes; no; timeout; newer request while held; suggestions off; never suggest a profile the person may not use.
    - [ ] "Test: the receipt still arrives within 50 ms, and the start notice or question follows."
- [ ] **Verify:** `grep -n "30" hermes_duplex/session.py` shows the hold timeout as a named, documented module constant.

---

## Phase 4: Transports, end-to-end, and docs

### Task 4.1: Discord `/duplex profile`, web picker, and status

Add the Discord and web switch paths and show each person's active profile in status. Runs in parallel with Task 3.5 (no shared files).

**Modules:** `.`
**Depends on:** 2.1, 3.4
**Owns:** `hermes_duplex/transports/discord/bot.py`, `hermes_duplex/transports/web/server.py`, `hermes_duplex/transports/web/static/*`, `hermes_duplex/service.py`, `hermes_duplex/plugin.py`, and the tests below

**Files:**

```
hermes_duplex/transports/discord/bot.py          (modify)
hermes_duplex/transports/web/server.py           (modify)
hermes_duplex/transports/web/static/app.js       (modify)
hermes_duplex/transports/web/static/index.html   (modify)
hermes_duplex/service.py                         (modify)
hermes_duplex/plugin.py                          (modify)
tests/unit/test_discord_bot.py                   (modify)
tests/unit/test_web_transport.py                 (modify)
tests/unit/test_service_lifecycle.py             (modify)
tests/unit/test_plugin_register.py               (modify)
tests/e2e/test_web_service_flows.py              (modify)
```

**Sub-tasks:**

- [ ] **4.1.1** — Discord `/duplex profile <name>` in the existing `/duplex` group, with autocomplete of the profiles the member may use. It calls `store.switch` and replies privately (ephemeral) per outcome. With no live conversation for that person, the switch is saved only and the reply says it applies next time. A member who is not allowlisted is refused.
- [ ] **4.1.2** — Web: accept an inbound JSON text frame `{"type":"profile","name":...}` and call `store.switch` for the signed-in person. Malformed or unknown text frames are logged and ignored; binary audio handling is unchanged.
- [ ] **4.1.3** — Web status frame carries the person's active profile (and the profiles they may use) on connect and after every switch from any path.
- [ ] **4.1.4** — Page: a picker of the profiles the person may use; choosing one sends the control frame; the picker follows the status frame.
- [ ] **4.1.5** — Service: pass the store to the Discord and web transports. `ServiceStatus` and `/duplex status` (`plugin.py` `status_text`) show each person's active profile.
- [ ] **4.1.6** — Tests: Discord command per outcome (switched with and without a live conversation, refused, unknown, already active) and autocomplete lists only permitted profiles; web control frame switches and a live web session hears the confirmation (in-process e2e in `tests/e2e/test_web_service_flows.py`); malformed frame ignored; status frame carries the active profile; `/duplex status` text lists active profiles.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; new tests per 4.1.6
- [ ] **Requirements:**
    - [ ] Switch paths: "Discord `/duplex profile`"; "a web control frame". Tests: "A Discord or web switch with a live conversation → instructions updated and confirmation spoken"; "The same with no live conversation → saved only, and Discord's private reply says so."
    - [ ] Scope: `/duplex profile <name> (autocomplete permitted) → store; private text reply`; `inbound JSON control frame {"type":"profile","name":...} → store; status frame carries active profile; page picker of permitted profiles`; "/duplex status shows active profile per person".
    - [ ] SHOULD: "`/duplex status` and the web status frame show each person's active profile. The web page shows a picker of the profiles that person may use."
- [ ] **Verify:** Start `uv run hermes-duplex serve --config <test config with the fake provider and web enabled>`, open the page with a token, and confirm the picker lists only the permitted profiles; or, where no browser is available, the in-process e2e test from 4.1.6 passes and its name is reported.

---

### Task 4.2: Real-Hermes and in-process end-to-end flows for active profiles

Prove the whole path against a real Hermes gateway: a voice switch, then a hand-off that lands on the switched profile.

**Modules:** `.`
**Depends on:** 3.5, 4.1
**Owns:** `tests/e2e/test_real_hermes.py`, `tests/e2e/test_real_hermes_flows.py`, `tests/e2e/real_hermes.py`, `tests/fixtures/provider_scripts/*` (new scripts), `tests/fixtures/hermes_home/duplex.yaml`

**Files:**

```
tests/e2e/test_real_hermes.py                    (modify)
tests/e2e/test_real_hermes_flows.py              (modify)
tests/e2e/real_hermes.py                         (modify)
tests/fixtures/provider_scripts/<switch script>.jsonl  (create)
tests/fixtures/hermes_home/duplex.yaml           (modify)
```

**Sub-tasks:**

- [ ] **4.2.1** — Fixture config: profiles carry `aliases` and a `voice_context`; `rob` has `default_profile: general`; no `sticky_idle_s`.
- [ ] **4.2.2** — Real-Hermes e2e: rob switches to stock-picker by voice (fake provider script), hears `switched_to`, then the portfolio request runs on `/p/stock-picker` with session id `duplex-rob-stock-picker`.
- [ ] **4.2.3** — Real-Hermes e2e: a request with no switch runs on the person's default profile; alex's switch to stock-picker is refused with `switch_refused` and no run starts.
- [ ] **4.2.4** — Real-Hermes e2e: after a switch, a service restart keeps the active profile (read back from `$HERMES_HOME/duplex/active_profiles.json`), and the next request runs on it.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass; Real-Hermes e2e passes with the new tests (report the count; a skip because `hermes` is missing counts as not validated)
- [ ] **Requirements:**
    - [ ] "Test: the real-Hermes e2e switches to stock-picker by voice, then the portfolio request runs on `/p/stock-picker`. A request with no switch runs on the person's default."
    - [ ] "Persist it across conversations and restarts."
- [ ] **Verify:** The Real-Hermes e2e output lists the new test names as passed.

---

### Task 4.3: Docs and live checklist

Bring the user guide, the architecture doc, the contributor entry point, and the manual checks in line with active profiles (AGENTS.md convention 10). Runs in parallel with Task 4.2.

**Modules:** `.`
**Depends on:** 3.5, 4.1 (and 3.2's eval numbers)
**Owns:** `README.md`, `AGENTS.md`, `docs/system/architecture.md`, `docs/live-checklist.md`, `duplex.example.yaml`

**Files:**

```
README.md                         (modify)
AGENTS.md                         (modify)
docs/system/architecture.md       (modify)
docs/live-checklist.md            (modify)
duplex.example.yaml               (modify)
```

**Sub-tasks:**

- [ ] **4.3.1** — README: active profile and the four ways to switch; suggestions (`suggest_switch`, `suggest_threshold`) and the yes/no flow; `voice_context`; current `eval-router` numbers from Task 3.2.
- [ ] **4.3.2** — README and `duplex.example.yaml` also say: a duplex "profile" is what Hermes Desktop calls a "Bot"; voice conversations are not Desktop Bot Chats; aliases may mirror Bot titles (copied by hand); the plugin's Discord bot must use its own token, never a profile gateway bot's token.
- [ ] **4.3.3** — `docs/system/architecture.md`: replace sticky routing, the hint, and `profile_hint` with the active profile, the store, switching, per-conversation prompt context, instruction updates by capability, the router's effort and suggestion job, and held requests; document the limitation for two people on one Discord call (hand-offs follow each speaker; the voice prompt shows the last switcher's profile).
- [ ] **4.3.4** — `AGENTS.md`: the opening paragraph and "Architecture at a Glance" (the router no longer picks the profile); the directory map gains `profiles.py` and `switching.py`.
- [ ] **4.3.5** — `docs/live-checklist.md`: a voice switch per provider (GPT-Live, OpenAI Realtime, Grok); a suggestion answered yes and no; the effect of `voice_context`; the GPT-Live pause on a switch; `/duplex profile` with and without a live conversation; the web picker.

**Validation:**

- [ ] **Build:** Gate exits 0
- [ ] **Tests:** Default tests pass (docs only; `test_packaging` and the example-config tests stay green)
- [ ] **Requirements:**
    - [ ] Scope (docs): "a duplex "profile" is what Hermes Desktop calls a "Bot"; voice conversations are not Desktop Bot Chats; aliases may mirror Bot titles (copied by hand); the plugin's Discord bot must use its own token, never a profile gateway bot's token".
    - [ ] Test Strategy, Live/manual: "live-checklist: voice switch per provider, suggestion yes/no, voice_context effect, GPT-Live switch pause".
    - [ ] Error handling: "Two people on one Discord call with different active profiles → Documented limitation".
- [ ] **Verify:** `grep -rn "profile_hint\|sticky_idle_s\|sticky" README.md AGENTS.md docs/system/ docs/live-checklist.md duplex.example.yaml` returns nothing (the retro under `docs/specs/` is history and stays).

<!-- END OF SPEC -->
