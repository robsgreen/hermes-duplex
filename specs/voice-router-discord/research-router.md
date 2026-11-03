# Research: routing classifier (sdlc-researcher, 2026-11-03)

- "jev" = TypeSafe AI Jev decision model (POST api.typesafe.ai/v1/systemone, jev-latest). choice/score/predicate questions, probabilities+confidence. LiteLLM benchmark p50 127ms/p95 231ms (80 synthetic prompts). ~$0.042/1M in (third-party). Python SDK typesafe-sdk.
- "OpenAI decisions API" = OpenAI Decisions API (POST /v1/decisions, gpt-6-luna), GA 2026-10-22. predicate/choice/score; $0.10/1M in, output free; no published absolute latency.
  - GPT-Live (gpt-live-1) native delegation: session.delegation.type "decisions", decision.event/decision.completed; session.triggers on input_transcript.delta (route while user speaks); match on source_event_id. Likely what user means by "client dispatch mode" -- VERIFY with voice-API researcher.
- Cloudflare Clef / Clef-flash: Apache-2.0 open weights, "Jev compatible" API, Workers AI; vendor claims 39ms median.
- Embedding kNN (model2vec / bge-small / semantic-router, MIT): few ms, needs examples + threshold.
- Small LLM JSON: mostly 300-700ms -> too slow for 150ms target. Arch-Router-1.5B: commercial licence restriction.
- Recommended: composite router = local kNN fast path || decision-model main path (profile choice + effort choice + "is follow-up" predicate), ~350ms timeout, fallback sticky/general. Model = lookup in (profile, effort) matrix, not classified. Sticky routing for follow-ups. Router Protocol w/ backends Knn/OpenAIDecisions/Jev(Clef via base URL)/LlmJson.
- Eval: 30-50 labelled utterances per profile incl. ASR noise + follow-ups + out-of-scope; confusion matrix, macro-F1, calibration, latency p50/p95.
