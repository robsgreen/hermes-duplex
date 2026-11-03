# Review: voice-router-discord

Frozen diff: e5c2a70 (merge base 0fd20c6), 135 files, +24090/-1.
Panel: A=sdlc-reviewer/opus, B=sdlc-reviewer/opus. Fan-out: Area 1 (voice side) and Area 2 (orchestration side), each seat all four passes per area.

## Mechanical checks (round 1)

- Check: uv sync --frozen → pass
- Check: uv run ruff check . → pass
- Check: uv run ruff format --check . → pass
- Check: uv run pyright → pass
- Check: uv run pytest -m "not live and not e2e" → fail (1 of 3 runs: 1 failed, 645 passed; other 2 runs 646 passed)
- Check: PATH=<hermes-venv>/bin:$PATH uv run pytest -m e2e → pass (4 passed)
- Check: uv run pytest -m live → skipped (no OPENAI_API_KEY; not runnable here)

M1 (gating, test): tests/contract/test_realtime_session.py::test_announcement_is_spoken_anyway_after_the_wait_cap is flaky — 2 of 8 isolated runs fail (TimeoutError after 5.5 s), 1 of 3 full-suite runs. Doc-keeper hypothesis: _wait_for_floor starts its 20 s count at first clock read, so a clock advanced before the pump reads it under-counts the wait. Not a base-branch failure (test is new).
