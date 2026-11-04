# AGENTS.md

## What Is This?

hermes-duplex is a Hermes Agent plugin that gives Hermes a full-duplex voice front end. People
(config's `people`) join a conversation through a transport (the plugin's own Discord bot or a web
page). Each conversation runs one `VoiceSession`, which joins it to one provider session on a
realtime voice model (GPT-Live, OpenAI Realtime, or xAI Grok). Each person has one active Hermes
profile (`ActiveProfiles`), which changes only when they ask to switch. When the voice model hands
work off, the session credits the request to a person, checks whether it is a switch request or an
answer to a switch question, and otherwise asks the router for an effort (the model comes from the
active profile's effort table) and an optional switch suggestion. The task runs on the speaker's
active profile through the Hermes API server. The `TaskTracker` follows each task to its end, and the
result is spoken back as a report.

## Backend/Dependencies

- **Hermes API server**: `{hermes.api_base}/p/<profile>/v1/runs` (start, SSE events, stop) with each
  profile's own `API_SERVER_KEY`. The only way the plugin reaches Hermes at runtime.
- **Hermes plugin `ctx`**: `register_command`, `register_cli_command`, `register_platform`, and
  `on_unload`, called once from `register(ctx)`.
- **Voice providers** over WebSocket: GPT-Live (`wss://api.openai.com/v1/live/sessions`), OpenAI
  Realtime (`wss://api.openai.com/v1/realtime`), xAI (`wss://api.x.ai/v1/realtime`).
- **Router APIs** over HTTP: OpenAI Decisions (`/v1/decisions`) and TypeSafe System One
  (`/v1/systemone`). The local `knn` backend downloads `minishlab/potion-base-8M` from Hugging Face
  once.
- **Discord**: the bot's own gateway and voice connections (DAVE end to end encryption).

## Tech Stack

- Python 3.14, uv (`uv.lock` committed), hatchling
- aiohttp (Hermes client, router APIs, web transport), websockets 15 (providers)
- discord.py[voice] 2.7.1 with `davey` (DAVE) and PyNaCl; libopus at runtime
- numpy, soxr (resampling), model2vec (static embeddings), PyYAML
- pytest + pytest-asyncio, ruff, pyright

## Architecture at a Glance

```
Transport (Discord | web) ──InputFrame──▶ VoiceSession ──audio──▶ RealtimeProvider (GPT-Live | Realtime)
                          ◀──PCM, flush──              ◀─events──
VoiceSession ──switch request──▶ ActiveProfiles ──switched (notify live sessions, transports)──▶ VoiceSession
VoiceSession ──text, speaker, active profile──▶ CompositeRouter ──effort, model, suggestion──▶ VoiceSession
VoiceSession ──PermissionChecker──▶ TaskTracker + HermesApiClient ──▶ /p/<active profile>/v1/runs
```

`build_service(config)` in `service.py` wires it all, the same way inside the gateway (the `duplex`
gateway platform's `connect()` starts it) and standalone (`hermes-duplex serve`). The session adapts
to a provider only through `ProviderCapabilities` and the provider's prompter, never its name. The
router never picks the profile a task runs on. Full narrative: `docs/system/architecture.md`.

## Before Making Changes

| Area | Read first |
|---|---|
| Any change | `docs/system/architecture.md` (Layers) |
| Plugin registration, gateway start/stop | `docs/system/architecture.md` (Run modes, Gateway lifecycle seam), `hermes_duplex/plugin.py` |
| Session, delegation, barge-in, reconnect | `docs/system/architecture.md` (Hand-off, Interruptions, Reconnect), `hermes_duplex/session.py` |
| Providers | `hermes_duplex/providers/base.py`, then the provider's package (`adapter.py`) and `registry.py` |
| Prompts, results, spoken notices | `docs/system/architecture.md` (Prompt strategy), `hermes_duplex/prompts/base.py` |
| Router | `docs/system/architecture.md` (Router policy), `hermes_duplex/router/composite.py` |
| Active profiles, switching, suggestions | `docs/system/architecture.md` (Active profiles and switching), `hermes_duplex/profiles.py`, `hermes_duplex/switching.py` |
| Hermes client, error codes | `docs/system/architecture.md` (Dispatch to Hermes), `hermes_duplex/dispatch/hermes_api.py` |
| Discord receive | `hermes_duplex/transports/discord/receiver.py` header, `NOTICE` |
| Config keys | `hermes_duplex/config.py`, `duplex.example.yaml`, `README.md` (Configure) |
| Manual checks | `docs/live-checklist.md` |

## Key Conventions

1. **No Hermes internals at runtime.** Never import `gateway`, `hermes_cli`, `agent`, `tools`, or any
   other hermes-agent module, and never monkey-patch Hermes. Use only the documented `ctx` methods and
   the API server. `tests/unit/test_no_hermes_internals.py` enforces the imports.
2. **Never add the `openai` SDK**, directly or through another package. Hermes pins `openai==2.54.0`;
   the provider protocols are implemented over `websockets`. The `deps-compat` CI job and
   `tests/unit/test_packaging.py` fail if it appears.
3. **Every dependency range must contain Hermes core's exact pin** for that package (hermes-agent
   @ `6112c2adb9e11f69734a29be9d2696f345ffa06a`), because the plugin installs into the gateway's
   environment. Keep upper bounds. `deps-compat` resolves against those pins.
4. **The session branches on capabilities only**, never on a provider's name.
   `tests/unit/test_session_no_provider_names.py` checks it.
5. **Secrets come only from env vars** that config `*_env` keys name. Never accept a literal secret
   in config, never log or print one (`doctor` rows included).
6. **Config fails closed.** Unknown keys are errors, so a new config key goes into `config.py`'s
   parser, its dataclass, `duplex.example.yaml`, and the README together.
7. **Layout.** The repo root is the Hermes plugin directory: `plugin.yaml` and a root `__init__.py`
   (`from .hermes_duplex.plugin import register`). Code lives in `hermes_duplex/` with relative
   imports. Keep `tests/conftest.py`: it stops pytest from importing the root `__init__.py`.
8. **Format only what you change.** Run `uv run ruff format <changed files>`, not on the whole tree,
   so your diff holds only your change; CI runs `ruff format --check .`.
9. **Hermes results are data.** The provider's prompter writes them (`VoicePrompter.result`:
   `frame_result` for GPT-Live, `json_report` for Realtime and Grok), and they are spoken with tools
   disabled.
10. **Keep docs in sync.** A behavior change updates `docs/system/architecture.md`, and a user-facing
    one `README.md` (and `docs/live-checklist.md` when it needs a manual check), in the same change.
11. **Prompts are per provider, notices are not.** To change what one provider's model reads, edit
    its prompter, the `prompt.py` in its package (`hermes_duplex/providers/gpt_live/`,
    `providers/realtime/openai/`, `providers/realtime/xai/`), following that vendor's prompting
    guide; a rule every prompt states goes in `hermes_duplex/prompts/blocks.py`, and a spoken notice
    in `prompts/notices.py` (one wording for all). Then regenerate the golden files with
    `HERMES_DUPLEX_UPDATE_PROMPTS=1 uv run pytest tests/unit/test_prompt_golden.py` and review
    their diff under `tests/fixtures/prompts/<provider>/`.

## Build & Dev

```bash
uv sync --frozen                                   # install (Python 3.14)
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run pytest -m "not live and not e2e"            # 1035 tests: unit, contract, in-process end-to-end
uv run pytest -m e2e -rs                           # 5 tests, real Hermes gateway; skip without `hermes` on PATH
HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs     # real keys and WAV fixtures, spends money
uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn
uv run hermes-duplex doctor --config duplex.yaml
uv run hermes-duplex serve --config duplex.yaml
```

The README's "Test" section has the hermes-agent install recipe for `-m e2e`, the live tier's
variables and fixture generator (`uv run python -m tests.live.make_audio_fixtures`), and the current
`eval-router` scores.

## Directory Map

```
plugin.yaml, __init__.py      Hermes manifest and entry point (repo root is the plugin dir)
duplex.example.yaml           documented example config
hermes_duplex/
  plugin.py                   register(ctx): /duplex, `hermes duplex`, the duplex gateway platform
  cli.py, doctor.py           serve | eval-router | doctor
  config.py                   duplex.yaml loading and validation
  service.py                  build_service, DuplexService
  session.py                  VoiceSession
  tasks.py                    TaskTracker (+ $HERMES_HOME/duplex/tasks.jsonl)
  people.py                   PermissionChecker
  profiles.py                 ActiveProfiles (+ $HERMES_HOME/duplex/active_profiles.json)
  switching.py                switch-request parser, yes/no answers
  prompts/                    shared prompt pieces: base (VoicePrompter, PromptContext), blocks, notices
  audio/                      frames, resample (soxr), mixer + attribution, gate, pacer
  providers/                  base (interface), wire (WebSocket), registry (ProviderSpec per provider.name)
    gpt_live/                 adapter, prompt (GptLivePrompter)
    realtime/                 adapter (shared RealtimeApiProvider, Dialect)
      openai/, xai/           dialect, prompt (OpenAiRealtimePrompter, GrokPrompter)
    fake/                     adapter (FakeProvider)
  router/                     base, knn, decisions, jev, composite, factory, eval
  dispatch/hermes_api.py      HermesApiClient
  transports/                 base; discord/ (bot, receiver, playback); web/ (server, static page)
tests/
  conftest.py                 collects the repo root as a plain directory (keep it)
  unit/, contract/            per-module tests; adapters and clients against fakes
  e2e/                        in-process web, Discord, router flows (default tier);
                              test_real_hermes*.py + real_hermes.py: real gateway (marker e2e)
  live/                       real GPT-Live + Decisions on a real gateway (marker live); WAV generator
  fakes/, fixtures/           fake WS server, fake Hermes API, fake LLM; scripts, phrases, Hermes home,
                              prompts/<provider>/ golden prompt files
docs/system/architecture.md   how it works
docs/live-checklist.md        manual Discord, GPT-Live, and Grok checks
```
