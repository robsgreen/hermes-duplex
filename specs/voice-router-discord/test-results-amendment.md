# Test Results: voice-router-discord — Amendment: Active Profile and Profile Voice Context

> **Plan:** `test-plan-amendment.md`
> **Work path:** `/home/user/hermes-duplex-worktrees/voice-router-discord` (HEAD `784e04d`, plus 2 uncommitted test changes listed below)
> **Run:** 2026-11-04
> **Verdict:** PASS. Every automated tier is green. 3 coverage gaps found: 2 are now closed with new tests, and 1 (L9) is a docs gap. The manual tier was not run.

## Tier results

| Tier | Command | Result |
|---|---|---|
| Static | `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pyright` | PASS: ruff clean, 126 files formatted, pyright 0 errors (re-run after the test additions: still clean) |
| Default | `uv run pytest -m "not live and not e2e"` | PASS: 1035 passed at HEAD; **1037 passed** with the 2 added tests (6 deselected) |
| Real-Hermes e2e | `PATH=<scratchpad>/hermes-venv/bin:$PATH uv run pytest -m e2e -rs` | PASS: **5 passed, 0 skipped** (`hermes` was on PATH; 97 s) |
| Router eval | `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` | PASS (exit 0). 103 phrases: effort accuracy 0.306, suggestion precision 1.000, suggestions made 20, recall 0.526, choice accuracy 0.825, macro-F1 0.823, p99 0.6 ms, 0 deadline misses. Matches the README table |
| Manual / live | `docs/live-checklist.md` | NOT RUN: no provider keys or Discord here |

Receipt timing flake (`tests/contract/test_realtime_session.py`, `RECEIPT_BUDGET_MS = 50`): not reproduced. 15 back-to-back runs of the receipt, response.create, and switch tests all passed. With 4 CPU-burning processes running alongside, the receipt took 5.4 ms.

## Tests added (uncommitted, in the worktree)

- `tests/unit/test_session.py::test_two_people_on_different_profiles_each_run_on_their_own_and_the_prompt_follows_the_last_heard` covers **D4**. Rob is on stock-picker and Alex on general, in one conversation. Each hand-off runs on its speaker's profile (`duplex-rob-stock-picker`, `duplex-alex-general`), the router is told each speaker's active profile, and the last instructions sent are for general (the last person heard). The delegations carry `offset_ms`, because frames run ahead of the session and crediting by arrival time would be racy. It passed 20 runs out of 20.
- `tests/unit/test_session_switching.py::test_a_switch_to_a_name_that_fits_no_profile_asks_which_and_changes_nothing` is now parametrized with `alex` → `which_profile(["general"])`. This covers **S2**'s "lists the person's permitted profiles" for a restricted person on the voice path. Before this change only Rob (who may use every profile) was tested there.

## Acceptance items

Legend: OK = covered and asserts the expected result; GAP = missing or weak; FIXED = gap closed by an added test.

### P. Active profile store and config
| ID | Covering tests | Outcome |
|---|---|---|
| P1 | `tests/unit/test_profiles.py::test_default_is_the_persons_own_default_profile` | OK |
| P2 | `test_profiles.py::test_default_is_the_router_default_when_the_person_may_use_it` | OK |
| P3 | `test_profiles.py::test_default_is_the_first_profile_in_the_persons_list` | OK |
| P4 | `test_profiles.py::test_a_switch_survives_a_new_store_on_the_same_file`, `::test_the_file_is_written_by_replacing_it_whole`; `tests/unit/test_service_lifecycle.py::test_start_loads_each_persons_saved_active_profile` | OK |
| P5 | `test_profiles.py::test_a_corrupt_file_gives_defaults_and_logs_an_error`, `::test_a_missing_file_gives_defaults_without_an_error`, `::test_a_corrupt_file_is_rewritten_on_the_next_switch`, `::test_a_file_that_cannot_be_written_logs_an_error_and_the_switch_holds` | OK |
| P6 | `test_profiles.py::test_an_unusable_saved_profile_falls_back_to_the_default_once`, `::test_a_switch_clears_a_pending_fallback`; `tests/unit/test_session_switching.py::test_the_fallback_to_the_default_is_spoken_once_when_the_person_is_first_heard` | OK |
| P7 | `tests/unit/test_config.py::test_person_without_profiles_is_rejected` (`[]` and null; the error names `people.alex.profiles`) | OK (a config load error means nothing starts) |
| P8 | `test_config.py::test_person_default_profile_they_may_not_use_is_rejected`, `::test_alias_used_by_two_profiles_is_rejected`, `::test_voice_context_over_2000_characters_is_rejected` (and `_of_2000_..._accepted`), `::test_suggest_threshold_outside_zero_to_one_is_rejected` (and `_of_one_is_accepted`) | OK |
| P9 | `test_config.py::test_removed_sticky_idle_is_rejected_with_a_migration_message` | OK |

### S. Explicit switching
| ID | Covering tests | Outcome |
|---|---|---|
| S1 | `tests/unit/test_switching.py::test_switch_phrase_names_a_profile` (all 3 phrases); `test_session_switching.py::test_a_switch_request_switches_and_is_confirmed_without_routing_or_a_task` (asserts the confirmation, `update_instructions`, no router request, no task, no `/runs`); contract: `tests/contract/test_realtime_session.py::test_switch_profile_tool_switches_updates_the_instructions_and_confirms_it` | OK |
| S2 | `test_switching.py::test_unknown_target_is_ambiguous`; `test_session_switching.py::test_a_switch_to_a_name_that_fits_no_profile_asks_which_and_changes_nothing` [rob, alex]; `tests/unit/test_discord_bot.py` (alex → `which_profile(["general","notes"])`) | FIXED (restricted-person case added) |
| S3 | `test_switching.py::test_alias_that_fits_two_profiles_is_ambiguous`, `::test_ambiguous_target_carries_no_request` | OK at parser level. At session level, `"ambiguous"` and unknown names take the same branch (`session._switch`), and that branch is covered by S2. A config with a shared alias can't reach a session anyway, because P8 rejects it |
| S4 | `test_session_switching.py::test_a_switch_to_a_profile_the_speaker_may_not_use_is_refused`; Real-Hermes `tests/e2e/test_real_hermes.py::test_alex_runs_on_his_default_and_his_switch_is_refused_without_a_run` | OK |
| S5 | `test_session_switching.py::test_a_switch_to_the_active_profile_says_it_already_is`; `test_profiles.py::test_switch_outcomes` | OK |
| S6 | `test_session_switching.py::test_switch_profile_call_answers_each_outcome_in_its_output`, `::test_switch_profile_call_switches_and_answers_with_the_outcome`; `test_realtime_session.py::test_switch_profile_tool_switches_updates_the_instructions_and_confirms_it`; `tests/contract/test_realtime_adapter.py::test_the_switch_profile_tool_lists_the_hermes_agents_of_the_prompt_context` | OK. Grok shares `RealtimeApiProvider`, and its dialect is parametrized in adapter tests |
| S7 | `tests/contract/test_gpt_live_session.py::test_a_switch_handed_off_as_words_restarts_gpt_live_on_the_new_profile_and_confirms_it` | OK |
| S8 | `tests/e2e/test_router_backends_flow.py::test_switch_requests_give_the_same_result_under_every_backend_without_routing` [knn, decisions, jev] (stub never called, no `route` log line) | OK |
| S9 | `test_discord_bot.py::test_profile_switch_heard_by_a_live_conversation_is_confirmed_there`; `test_session_switching.py::test_a_switch_made_elsewhere_updates_the_instructions_and_is_confirmed`; `test_service_lifecycle.py::test_status_shows_each_persons_active_profile_and_the_transports_switch_it` | OK |
| S10 | `test_discord_bot.py::test_profile_switch_with_no_live_conversation_is_saved_for_next_time` | OK |
| S11 | `tests/e2e/test_web_service_flows.py::test_the_page_picker_switches_the_live_conversation_and_the_next_request` (confirmation spoken, new instructions, next run on `/p/general`, status frame shows the new profile, saved to file); `tests/unit/test_web_transport.py::test_profile_frame_switches_and_every_page_of_that_person_follows` | OK |
| S12 | `test_web_transport.py::test_malformed_text_frames_are_logged_and_ignored` (5 malformed shapes; audio after the bad frame still arrives; log line checked) | OK |
| S13 | `test_switching.py::test_text_without_a_switch_request_is_none` ("talk to me about Tesla", "Talk to us about the plan", …) | OK. The literal phrase "talk to me about the weather" isn't a case, but the same shape is |

### C. Prompt context and instruction updates
| ID | Covering tests | Outcome |
|---|---|---|
| C1 | `tests/unit/test_session.py::test_a_signed_in_conversation_starts_on_that_persons_active_profile` | OK |
| C2 | `test_session.py::test_a_conversation_without_a_signed_in_person_starts_on_the_default_profile_and_moves_to_the_first_heard_persons` | OK |
| C3 | same as C2 (heard Rob → stock-picker instructions); `::test_hearing_someone_on_the_profile_already_spoken_for_changes_nothing` | OK |
| C4 | `test_session_switching.py::test_a_switch_request_switches_...`, `::test_a_switch_made_elsewhere_updates_the_instructions_and_is_confirmed` | OK |
| C5 | `tests/unit/test_session_reconnect.py::test_a_reconnect_after_a_context_change_renders_the_new_profile` | OK |
| C6 | `test_realtime_adapter.py::test_update_instructions_replaces_only_the_instructions_and_the_tools_still_work` [OpenAI, xAI], `::test_announcements_after_update_instructions_use_the_new_instructions`; `test_realtime_session.py::test_a_context_change_replaces_only_the_instructions_in_place` | OK |
| C7 | `test_gpt_live_session.py::test_a_context_change_restarts_gpt_live_with_new_instructions_and_the_task_still_reports`; `test_session_reconnect.py::test_restarts_for_new_instructions_never_end_the_conversation_as_short_sessions`; `test_session.py::test_without_instruction_replacement_a_context_change_restarts_the_provider_with_the_summary`; `tests/contract/test_gpt_live_adapter.py::test_update_instructions_sends_nothing_and_names_the_capability` | OK |
| C8 | `test_session.py::test_a_failed_instruction_update_falls_back_to_a_restart` | OK |
| C9 | `tests/unit/test_session_no_provider_names.py` | OK |

### D. Dispatch to the active profile
| ID | Covering tests | Outcome |
|---|---|---|
| D1 | `test_session.py::test_runs_on_the_speakers_active_profile` (`/p/general`, `duplex-alex-general`); Real-Hermes `test_real_hermes.py::test_alex_runs_on_his_default_and_his_switch_is_refused_without_a_run` | OK |
| D2 | Real-Hermes `test_real_hermes.py::test_rob_switches_to_stock_picker_by_voice_and_his_portfolio_runs_there` | OK (passed) |
| D3 | Real-Hermes `tests/e2e/test_real_hermes_flows.py::test_rob_keeps_his_active_profile_across_a_restart` | OK (passed) |
| D4 | **NEW** `test_session.py::test_two_people_on_different_profiles_each_run_on_their_own_and_the_prompt_follows_the_last_heard` | FIXED. Before this, no automated test had two people on *different* profiles (`test_gpt_live_session.py::test_overlapping_speakers_get_their_own_tasks` has both on stock-picker). The manual check is live-checklist §22 |

### R. Router, suggestions, and held requests
| ID | Covering tests | Outcome |
|---|---|---|
| R1 | `tests/unit/test_router_composite.py::test_model_comes_from_the_active_profiles_table`, `::test_active_profile_without_models_uses_the_hermes_profile_default`, `::test_suggests_another_profile_only_when_clearly_better` (asserts the model stays on the active profile) | OK |
| R2 | `test_router_composite.py::test_suggests_another_profile_only_when_clearly_better` (threshold 0.85 exact, margin 0.2 exact); `tests/unit/test_session_suggestions.py::test_a_request_another_profile_fits_is_held_and_the_speaker_asked` (`suggest_switch` spoken, nothing run) | OK |
| R3 | `test_router_composite.py::test_suggests_another_profile_only_when_clearly_better` (0.84 and margin 0.19 → None), `::test_unconfident_backend_falls_back_without_a_suggestion` | OK |
| R4 | `test_router_composite.py::test_suggestions_can_be_turned_off`; `test_session_suggestions.py::test_the_router_suggests_only_when_on_and_only_a_permitted_profile` | OK |
| R5 | `test_router_composite.py::test_never_suggests_a_profile_the_speaker_may_not_use`; `test_session_suggestions.py::test_a_suggested_profile_the_speaker_may_not_use_is_never_asked_about` | OK |
| R6 | `test_session_suggestions.py::test_yes_switches_and_runs_the_held_request_on_the_new_profile` ["yes", "Sure, go ahead."], `::test_confirm_switch_true_switches_and_runs_there`; contract `test_realtime_session.py::test_a_suggested_switch_is_asked_after_the_receipt_and_confirm_switch_runs_it_there`; Real-Hermes `test_real_hermes_flows.py::test_a_yes_to_the_switch_suggestion_runs_the_request_on_stock_picker_and_status_stays_local` | OK |
| R7 | `test_session_suggestions.py::test_no_runs_the_held_request_on_the_active_profile` ["no", "No thanks.", "stay here"], `::test_confirm_switch_false_runs_on_the_active_profile`; contract `test_gpt_live_session.py::test_a_no_handed_off_as_words_answers_the_switch_question_and_runs_on_the_active_profile` | OK |
| R8 | `test_session_suggestions.py::test_no_answer_within_the_timeout_runs_on_the_active_profile` (controlled sleep for `HOLD_TIMEOUT_S`) | OK |
| R9 | `test_session_suggestions.py::test_a_newer_request_runs_the_held_one_on_the_active_profile_and_is_evaluated`, `::test_a_newer_suggested_request_replaces_the_hold_and_is_asked_about` | OK |
| R10 | `test_session_suggestions.py::test_yes_...` / `::test_no_...` (exactly one run, the held request; the router is asked only for the request: "the answer is never routed") | OK |
| R11 | Router eval run above; `tests/unit/test_router_eval.py::test_a_phrase_on_its_own_profile_counts_no_suggestion_as_right_and_a_suggestion_as_wrong`, `::test_the_fixture_loads_with_profiles_active_profiles_and_follow_ups` | OK. The effort accuracy of 0.306 is expected: `knn` judges no effort, so the score is the share of phrases labelled `medium` (README says so). No threshold is specified |

### T. Receipt and tool schema
| ID | Covering tests | Outcome |
|---|---|---|
| T1 | `test_realtime_session.py::test_delegate_receipt_comes_at_once_and_the_task_reaches_the_router` (< 50 ms; measured 5.4 ms under load), `test_realtime_adapter.py::test_the_delegate_receipt_says_the_request_was_received_not_started`; the follow-up start notice or switch question is covered in `test_session_suggestions.py::test_a_request_another_profile_fits_...` and the contract suggestion test | OK |
| T2 | `test_realtime_adapter.py::test_session_tools_take_a_task_a_task_choice_a_profile_and_a_yes_or_no`, `::test_without_profiles_there_is_no_switch_profile_tool` | OK |
| T3 | grep of `hermes_duplex/` and `tests/fixtures/` for `profile_hint`, `"hint"`, `"sticky"`, `hint=` finds nothing; the only hits are `install_hint` (unrelated) and `config.py`'s `sticky_idle_s` rejection (P9). `RouteSource = Literal["decisions","jev","knn","fallback"]`. Pyright passes | OK |

### V. Profile voice context in prompts
| ID | Covering tests | Outcome |
|---|---|---|
| V1 | `tests/unit/test_prompt_golden.py` (goldens under `tests/fixtures/prompts/{gpt-live,openai-realtime,xai-realtime}/`) | OK |
| V2 | `tests/unit/test_prompts.py::test_every_prompter_carries_only_the_active_profiles_voice_context_once`, `::test_every_prompter_leaves_out_the_active_agent_when_none_is_active` | OK |
| V3 | `test_prompts.py::test_every_prompter_says_what_follows_a_hand_off_and_how_to_switch`, `::test_gpt_live_hands_off_switch_requests_...`, `::test_realtime_switches_and_answers_switch_questions_with_their_tools`, `::test_grok_switches_and_answers_switch_questions_with_their_tools` | OK |
| V4 | `test_prompts.py::test_grok_puts_the_voice_context_in_objective_as_rules_about_words` | OK |

### W. Round-7 task-resolution fixes
| ID | Covering tests | Outcome |
|---|---|---|
| W1 | `test_session.py::test_cancel_the_latest_one_asks_which_when_several_are_running`, `::test_order_and_recency_words_name_no_task` | OK |
| W2 | `test_session.py::test_news_is_a_topic_word_not_a_plural_of_new` | OK |
| W3 | `test_session.py::test_contractions_and_dismissals_add_no_topic_words` | OK |
| W4 | `test_prompts.py::test_a_topic_drops_polite_lead_ins_so_alike_requests_stay_apart` | OK |
| W5 | `test_session.py::test_cancel_naming_a_task_that_is_not_open_while_another_is_finds_none`, `::test_cancel_naming_a_task_when_none_is_open_has_nothing_to_cancel` | OK |
| W6 | `test_session.py::test_cancel_about_another_topic_asks_about_the_only_open_task`, `::test_resolve_task_with_one_open_task_and_other_topic_words_names_none` | OK |
| W7 | `test_session.py::test_resolve_task_with_one_open_task_and_no_topic_words_means_that_one` | OK |
| W8 | grep: `hermes_duplex/providers/realtime/adapter.py:74` contains "only open (queued or running) one" | OK |

### Q. Status
| ID | Covering tests | Outcome |
|---|---|---|
| Q1 | `tests/unit/test_plugin_register.py::test_status_lists_each_persons_active_profile`; `test_service_lifecycle.py::test_status_shows_each_persons_active_profile_and_the_transports_switch_it` | OK |
| Q2 | `test_web_transport.py::test_connect_shows_the_active_profile_and_only_the_permitted_ones`, `::test_a_switch_from_elsewhere_moves_the_picker`, `::test_a_profile_frame_that_changes_nothing_says_why_and_resets_the_picker`; `test_web_service_flows.py::test_the_page_picker_switches_...` | OK |

### Live checklist (manual, NOT RUN)
| ID | `docs/live-checklist.md` section | Status |
|---|---|---|
| L1 | §16 Switch by voice (gpt-live), §19 GPT-Live switch pause | not run |
| L2, L3 | §16 (openai-realtime / xai-realtime `switch_profile`) | not run |
| L4, L5 | §17 Switch suggestions (step 3 is the 30 s silence) | not run |
| L6 | §18 Voice context | not run |
| L7 | §20 `/duplex profile` on Discord | not run |
| L8 | §21 Web picker | not run |
| L9 | **No section.** No step in the checklist removes the saved profile from config, restarts, and listens for "You're back on general" | GAP (docs). Automated coverage exists (P6) |
| (D4) | §22 Two people on different profiles | not run |

## Known notes
- "switch to X and <request>" hand-off is covered in unit and in-process tests (`test_session_switching.py::test_a_switch_followed_by_a_request_switches_then_runs_the_request_there`, `::test_a_request_after_a_switch_that_does_not_happen_is_dropped`, `::test_polite_words_after_a_switch_are_no_request`; `test_session_suggestions.py::test_a_request_said_with_a_switch_runs_on_the_new_profile_without_a_question`; parser `test_switching.py::test_request_after_the_target_is_the_rest`). No Real-Hermes test covers it; the manual check is live-checklist §16 step 4.
- The receipt-budget flake was not reproduced (see above).
