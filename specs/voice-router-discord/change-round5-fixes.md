# Change brief: round-5 fixes before push — user-directed, 2026-11-03
Evidence: review.md "## Review round 5". Decisions: research-decisions.md (U1 = C, U2 = A, decision 3 = A).

1. S1 — session._task_number: accept a number from the request only in explicit "task N" form (or when the whole request is just a number); accept a number from the assistant context only from a cancel/status sentence ("Cancelling task N", "Checking task N") AND only if it is one of the speaker's active tasks; otherwise None (status → summary; cancel → per CANCEL_POLICY / latest running only when exactly one is running, else ask which one via a spoken notice instead of cancelling). Parametrized tests for "top 5 stocks", "30 day report", stale "Task 3 … running", "Task 1 has finished", plus preserved cases ("cancel task 2"→2, "Cancelling task 3." + "the weather one"→3).
2. S2 — adoption must also happen for people a session has already heard: re-check adopt() when the credited speaker delegates/asks status/cancels, and whenever the tracker gains unattended tasks for a person heard in a live session (e.g. a tracker notification/generation counter). Test: session C already hearing rob when session A ends → C adopts and announces.
3. S3 — TaskTracker.restore never blocks start: read bytes and decode per line inside the try (skip UnicodeDecodeError lines), reject or normalise naive timestamps to UTC, guard restore so a bad history only logs. Tests for a torn UTF-8 last line and a naive timestamp.
4. S4 — results queued but not yet spoken at session end are not lost: track unspoken announcements per task; on session end mark them for adoption so the next conversation delivers them with the "while you were away" wording. Test.
5. S5 — reseed context: use a neutral state phrase (no first-person "I'll tell you") inside history_summary; spoken status keeps the promise. Update reseed goldens.
6. S6 — fix the _unattended comment (or discard numbers on terminal transitions).
7. S7 — adopted run whose status can't be read and later ends: use the "while you were away" wording.
8. S8 — Red flag Repetition: build notices.gist on fit_text (add a marker parameter); name or share the sentence-end patterns (one source).
9. U2 — restore only tasks created within `tasks.restore_max_age_h` (new config key, default 24); older open tasks are marked unknown silently (logged). Config validation + tests.
10. U1 — README known limitations / usage: results from earlier tasks are read in full in whichever conversation next hears the person, including shared Discord channels where non-allowlisted members can hear.
Docs: architecture.md, README, AGENTS.md test count, live-checklist as needed.
