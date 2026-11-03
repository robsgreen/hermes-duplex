# Change brief: per-provider package layout + ProviderSpec registry — user-directed, 2026-11-03
Queued AFTER change-prompts.md lands. Mechanical restructure, no behavior change.

Target layout:
hermes_duplex/providers/
  base.py, registry.py, wire.py
  gpt_live/   __init__.py adapter.py prompt.py        (GptLiveProvider + GptLivePrompter)
  realtime/   __init__.py adapter.py                  (shared OpenAI-style RealtimeApiProvider, Dialect type)
    openai/   __init__.py dialect.py prompt.py        (OPENAI dialect + RealtimePrompter)
    xai/      __init__.py dialect.py prompt.py        (XAI dialect + GrokPrompter)
  fake/       __init__.py adapter.py                  (FakeProvider)
hermes_duplex/prompts/ keeps only shared pieces: base.py (PromptContext, VoicePrompter, TaskReport, helpers), blocks.py, notices.py.

Registry: one `ProviderSpec` per provider name (name, factory, prompter, dialect where applicable); `provider_factory(name)` keeps working (fail closed on unknown names).
Rules: relative imports only; no behavior change — every golden prompt file under tests/fixtures/prompts/ must stay byte-identical (do not regenerate them); move tests accordingly if they import moved modules; update AGENTS.md directory map, README/architecture references. All gates + e2e green.
