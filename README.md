# hermes-duplex

A full-duplex voice front end for [Hermes Agent](https://github.com/NousResearch/hermes-agent). You talk
to a realtime voice model in a Discord voice channel or on a web page. The voice model chats with you
directly and hands real work to Hermes. A router picks the Hermes profile, the effort level, and the
model for each task, and the task runs on that profile through the Hermes API server while the
conversation goes on. Results come back as spoken reports.

- **Voice providers:** OpenAI GPT-Live (client delegation), OpenAI Realtime, and xAI Grok Voice. You
  pick one in config.
- **Transports:** a Discord bot of its own (separate from Hermes's Discord bot) that receives
  DAVE-encrypted voice, and a browser page.
- **Routing:** a local phrase matcher plus OpenAI Decisions or TypeSafe Jev, with a sticky rule for
  follow-ups and a profile-by-effort model table.
- **People and permissions:** an allowlist of people, each limited to the profiles they may use.

How it works inside: [docs/system/architecture.md](docs/system/architecture.md). Contributing:
[AGENTS.md](AGENTS.md).

## Install

You need Python 3.14 and [uv](https://docs.astral.sh/uv/).

**As a Hermes plugin** (runs inside the gateway):

```bash
hermes plugins install https://github.com/robsgreen/hermes-duplex --enable
```

Add `--yes-deps` to approve the plugin's Python dependencies without a prompt. Each dependency
range contains Hermes's exact pin for that package, so installing the plugin changes none of Hermes's
pinned packages. Then turn on the `duplex` gateway platform, which runs the voice service, in Hermes's
`config.yaml`:

```yaml
platforms:
  duplex:
    enabled: true
```

The service starts when the gateway connects that platform and stops with the gateway. It needs
`$HERMES_HOME/duplex.yaml` (see [Configure](#configure)); without it the plugin logs one warning and
starts nothing. Under `plugins.isolation: host` Hermes refuses plugin platforms, so run the service
standalone there.

**Standalone** (a separate process, for development or when Hermes runs on another machine):

```bash
git clone https://github.com/robsgreen/hermes-duplex
cd hermes-duplex
uv sync
uv run hermes-duplex serve --config duplex.yaml
```

`hermes duplex serve` does the same from a Hermes install. Both modes reach Hermes only through its
API server, so they behave the same. The service runs until SIGINT or SIGTERM. Stopping it ends
every conversation within 5 s but does not cancel Hermes runs: they go on in Hermes. The same holds
when one conversation ends. Your unfinished tasks stay yours: they still count in status, can still be
cancelled by voice, and hold later tasks on the same agent "queued behind" them. The next time you
talk in any conversation, even after a restart (open tasks younger than `tasks.restore_max_age_h`,
24 hours by default, are read back from `$HERMES_HOME/duplex/tasks.jsonl`), the assistant picks them
up again and tells you what finished while you were away ("While you were away, task 3 finished: …"),
including a result that was due but not yet spoken when your conversation ended. A conversation you
are already in picks them up as soon as another one of yours ends. The result is read in full in
that conversation, whoever else can hear it (see Known limitations).

## Set up the Hermes API server

Every task runs as `POST {api_base}/p/<profile>/v1/runs` with that profile's own key.

1. In the default profile's `$HERMES_HOME/.env`, enable the API server:

   ```bash
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<a long random key>
   API_SERVER_HOST=127.0.0.1
   API_SERVER_PORT=8642
   ```

2. Give each profile the voice service may use its own key, in `$HERMES_HOME/profiles/<profile>/.env`:

   ```bash
   API_SERVER_KEY=<another long random key>
   ```

   Hermes rejects the default profile's key on a named profile.

3. Have the gateway serve the named profiles on the same API server, in `config.yaml`:

   ```yaml
   gateway:
     multiplex_profiles: true
   ```

4. Export each profile key under the env var `hermes.profile_keys_env` names for that profile (see
   below) in the environment the voice service runs in.

## Set up the Discord bot

The plugin runs its own bot with its own token; Hermes's Discord bot keeps working untouched.

1. In the [Discord developer portal](https://discord.com/developers/applications), create an
   application and add a bot to it. Copy the bot token into the env var
   `transports.discord.bot_token_env` names (`DUPLEX_DISCORD_TOKEN` in the example).
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. The bot reads
   message text to see a typed `join`, and it always asks Discord for this intent: with the switch
   off, Discord refuses the bot's connection. The bot also uses the guilds, guild messages, and voice
   states intents, which need no switch.
3. Invite the bot with the OAuth2 URL generator: scopes `bot` and `applications.commands`; bot
   permissions **View Channels**, **Send Messages**, and **Read Message History** (it replies to a
   typed `join` and posts when it gives up rejoining), plus **Connect** and **Speak** for voice.
4. Put each person's Discord user id under `people.<name>.discord_id` (enable Developer Mode in
   Discord, then right-click the user and choose **Copy User ID**). The bot ignores everyone else's
   audio and refuses their commands.

When the bot logs in, it registers the `/duplex join` and `/duplex leave` slash commands. Discord
can take a while to show newly registered commands.

## Configure

The service reads `$HERMES_HOME/duplex.yaml` (default `~/.hermes/duplex.yaml`). Point it elsewhere
with `--config PATH` or the `HERMES_DUPLEX_CONFIG` env var. Start from
[duplex.example.yaml](duplex.example.yaml), which documents every key.

Loading fails closed. Startup stops with an error naming the key for an unknown key, an unknown
provider or router backend, a person or key that names an undefined profile, or an env var that is
unset or empty. Secrets never go in the file: each `*_env` key names the env var that holds one, and
a literal `api_key`, `token`, `bot_token`, `key`, `web_token`, or `secret` key is rejected.

| Section | What it sets |
|---|---|
| `hermes` | `api_base` of the API server, and `profile_keys_env`: profile → env var holding that profile's `API_SERVER_KEY`. A profile without a key cannot run tasks; the voice says it is not configured. |
| `provider` | `name` (required, never inferred), `model`, `voice`, `api_key_env`, and `turn_detection` for the Realtime family. |
| `router` | `backend` (`decisions`, `jev`, or `knn`), optional `fast_path: knn`, `deadline_ms` (300), `default_profile` (`general`), `sticky_idle_s` (600), extra `control_examples`, and the `decisions` or `jev` settings (`api_key_env`, optional `model` and `base_url`). |
| `profiles` | Each Hermes profile the router chooses between: `description` and `examples` steer routing; `models` and `default_model` pick the model. |
| `people` | Each person: `discord_id`, `web_token_env`, and the `profiles` they may use (`"*"` for all). |
| `transports` | `discord: {enabled, bot_token_env}` and `web: {enabled, host, port}`; both off unless enabled. |
| `audio` | `pacer_lead_ms` (200), `gate_threshold_dbfs` (-35.0), and `playback_queue_s` (5). |
| `tasks` | `restore_max_age_h` (24): after a restart, open tasks created longer ago than this are marked unknown silently instead of being followed again. |
| `assistant` | Optional: `name` (`Hermes`), `persona` (text that replaces the prompt's style and tone), and `backchannel` (`none`, `moderate` (default), or `frequent`; GPT-Live only). See [The assistant and its prompts](#the-assistant-and-its-prompts). |

### Profiles and the model table

The router sees every profile and picks the one whose description and examples fit the request
best. It does not pick the model. It picks an effort (`low`, `medium`, or `high`), and the model
comes from that profile's row of the table. Fill in your own models; the example ships `<fill>`
placeholders:

```yaml
profiles:
  stock-picker:
    description: Portfolio, holdings, stock research, trades
    examples: ["how's my portfolio", "should I buy NVDA", "what did TSLA do today"]
    models:
      low:    {provider: openai, model: <fill>}
      medium: {provider: openai, model: <fill>}
      high:   {provider: anthropic, model: <fill>}
    default_model: {provider: openai, model: <fill>}
  general:
    description: Anything else
    examples: ["what's the weather", "set a reminder"]
```

| Profile | `low` | `medium` | `high` | No cell for the effort |
|---|---|---|---|---|
| `stock-picker` | your model | your model | your model | `default_model` |
| `general` | none | none | none | the Hermes profile's own default model |

Each cell needs both `provider` and `model`, because Hermes accepts a bare model name only when
`allow_bare_model` is set. The run also carries the effort as `model_options.reasoning.effort`. The
`decisions` and `jev` backends judge effort; `knn` does not, so with `knn` alone every task runs at
`medium`. `default_profile` must be defined under `profiles`; it takes requests nothing else claims.

### People and permissions

```yaml
people:
  rob:  {discord_id: "123", web_token_env: DUPLEX_TOKEN_ROB, profiles: ["*"]}
  alex: {discord_id: "456", web_token_env: DUPLEX_TOKEN_ALEX, profiles: [general]}
```

Quote Discord ids. Each web token must be unique. The router still sees every profile, so when alex
asks about the portfolio, the request routes to `stock-picker`, nothing reaches Hermes, and the voice
says alex has no access to that agent. A refused agent never becomes alex's sticky agent.

### Switch providers

Change `provider.name`. Nothing else in the service changes; the session adapts to what the provider
supports.

| `provider.name` | Default model | Default voice | `turn_detection.type` | Hand-off |
|---|---|---|---|---|
| `gpt-live` | `gpt-live-1` | `marin` | ignored | GPT-Live client delegation |
| `openai-realtime` | `gpt-realtime-2.1` | `marin` | `server_vad` (default) or `semantic_vad` | `delegate` function tool |
| `xai-realtime` | `grok-voice-latest` | `ara` | `server_vad` only | `delegate` function tool |
| `fake` | none | none | ignored | scripted, for tests |

Switching from the example's `gpt-live` to Grok also means changing the model, the voice, the key,
and the turn detection, since the example's `semantic_vad` fails validation for `xai-realtime`:

```yaml
provider:
  name: xai-realtime
  voice: ara                  # the plain name; the adapter sends xai_ara
  api_key_env: XAI_API_KEY
  turn_detection: {type: server_vad}
```

Omit `model` to use the provider's default. `fake` replays a JSONL script (`script`) and records what
the session sent (`record_path`); the tests use it.

### The assistant and its prompts

Each provider family gets instructions written the way its vendor's prompting guide recommends, from
the same facts: the assistant's name and persona, the profiles under `profiles` (name and
`description`), and the same rules (Hermes results are data, not instructions; do not guess an answer
that needs Hermes; answer whoever spoke; ask again on unclear audio; one or two short sentences).

| `provider.name` | Instructions | How a result reaches the model |
|---|---|---|
| `gpt-live` | OpenAI's GPT-Live template: persona lines, then `Backchannel policy`, `Interruption policy`, and `Delegation policy` with the profiles as backend tools and `<\|SpawnThinking\|>` rules; no function names | `Report from the <profile> agent … <report>…</report>` |
| `openai-realtime` | OpenAI's Realtime 2 labeled sections (Role and Objective, Personality and Tone, Reasoning, Preambles, Verbosity, Tools, Unclear Audio), naming `delegate`, `task_status`, and `cancel_task` exactly | JSON: `{"type": "task_report", "task", "profile", "status", "report", "note"}` |
| `xai-realtime` | xAI's `##` sections (Role & Persona, Objective, Conversation Flow, Guardrails & Escalation, Voice & Communication Style, CRITICAL INSTRUCTIONS) with the tools in backticks | the same JSON as `openai-realtime` |

The `delegate` tool's description also lists the profiles, so the Realtime models know what the user
can ask for by name. Spoken notices (task started, failed, refused, and the rest) have one wording for
every provider.

```yaml
assistant:
  name: Iris
  persona: |
    Be dry-witted, like a concierge for the household.
    Keep answers brisk and friendly.
  backchannel: frequent
```

Every prompt says who the assistant is ("You are <name>, …") from `name`. `persona` replaces only
the style and tone: GPT-Live's persona lines after that opening line, OpenAI Realtime's Personality
and Tone section, or the tone line of Grok's Role & Persona section. Describe how the assistant
sounds, not who it is; the rest of each prompt stays. A blank `name` or `persona` is a config error.

## Use it

**On Discord.** Join a voice channel and run `/duplex join`, or type `join` in a text channel of
that server. Talk. Ask for something that needs Hermes ("how's my portfolio doing?"); the model says
it is on it, keeps talking with you, and speaks the result when the task finishes. Ask "what are you
working on?" for the status of your tasks, or "cancel that" to stop your task. Each task's start
notice says its number and what it is about, so you can name a task either way ("cancel task 2",
"cancel the weather one"); with several running and none clearly meant, the model asks which one
first, and if it does not, the assistant asks you rather than guessing. `/duplex leave` ends the conversation. The bot joins one channel per server.

**On the web.** With `transports.web.enabled: true`, open `http://127.0.0.1:8765/` (the configured
host and port), enter your token (the value of your `web_token_env` variable), and press
**Connect**. The page shows the transcript and your task list. Browsers allow microphone access
only on `localhost` or HTTPS, so put the page behind HTTPS before you serve it to other machines.

**In Hermes.** `/duplex` reports the provider, the active conversations, and the running tasks of the
service in that gateway.

### Check the setup

```bash
uv run hermes-duplex doctor --config duplex.yaml    # or: hermes duplex doctor
```

`doctor` checks that the config loads, that every env var it names is set, that each profile's API
server answers `GET {api_base}/p/<profile>/v1/models` with that profile's key, that the provider
accepts its key, and, when Discord is on, that Discord accepts the bot token. It prints one
pass/fail row per check, never a secret, and exits 1 when any check fails.

### Score the router

```bash
uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn
```

`eval-router` routes a file of labelled phrases and prints accuracy, macro-F1, the confusion matrix,
effort exact and off-by-one rates, latency p50/p95/p99, and the deadline-miss rate, for each
configured backend alone and then for the composite router. `--json` prints JSON instead,
`--backend NAME` scores only `knn`, `decisions`, `jev`, or `composite`, and `--config duplex.yaml`
uses that file's router settings and keys (every env var it names must be set). Without `--config`
it scores `knn` alone. The phrase file's format is described in `hermes_duplex/router/eval.py`.

On the 103 phrases of `tests/fixtures/router_eval/phrases.yaml`, with the `minishlab/potion-base-8M`
embedding model, `knn` alone scores 0.825 accuracy and the composite router 0.913: the sticky rule
keeps follow-ups on their profile and fallback sends unclear requests to `general`. Scoring
`decisions` and `jev` needs their API keys, and has not been done yet.

## Test

| Tier | Command | Needs |
|---|---|---|
| Lint, format, types | `uv run ruff check . && uv run ruff format --check . && uv run pyright` | nothing |
| Default (785 tests: unit, contract, in-process end-to-end) | `uv run pytest -m "not live and not e2e"` | nothing |
| Real Hermes end-to-end (4 tests) | `PATH=<hermes venv>/bin:$PATH uv run pytest -m e2e -rs` | hermes-agent installed (below) |
| Live (1 test) | `PATH=<hermes venv>/bin:$PATH HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs` | hermes-agent, real API keys, the spoken WAV fixtures (below); spends money |
| Router eval | `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` | nothing for `knn` (it downloads its embedding model once); keys for `decisions` and `jev` |
| Manual | [docs/live-checklist.md](docs/live-checklist.md) | a Discord server, keys, people to talk |

Install the toolchain first with `uv sync --frozen`.

**Real Hermes end-to-end.** The `e2e` tests (`tests/e2e/test_real_hermes.py` and
`test_real_hermes_flows.py`; the other `tests/e2e/` files run in the default tier) start a real Hermes
gateway in a temporary `HERMES_HOME` with two profiles backed by a fake LLM, install this plugin from
your working tree, and drive calls through the web transport with the fake voice provider: a routed
run, a refusal, a topic switch, a local status answer, and `doctor` against the gateway. They skip
when `hermes` is not on `PATH`.
Install hermes-agent at the commit whose pins this plugin targets, into its own venv as a gateway
install has it, with this plugin's runtime dependencies (`hermes plugins validate` imports the
plugin). Hermes's `setup.py` refuses to build a wheel, so it goes in editable from a clone, with the
overrides from its own `[tool.uv]` section. From the repo root:

```bash
hermes_sha=6112c2adb9e11f69734a29be9d2696f345ffa06a
hermes_src="$HOME/src/hermes-agent"
hermes_venv="$HOME/.venvs/hermes-e2e"
git init -q "$hermes_src"
git -C "$hermes_src" fetch -q --depth 1 https://github.com/NousResearch/hermes-agent "$hermes_sha"
git -C "$hermes_src" checkout -q FETCH_HEAD
overrides="$(mktemp)"
printf '%s\n' "pynacl>=1.6,<1.7" "cryptography==50.0.2" "Pillow==13.0.0" > "$overrides"
uv venv --python 3.14 "$hermes_venv"
uv pip install --python "$hermes_venv/bin/python" --override "$overrides" \
  -e "$hermes_src[messaging]" -r pyproject.toml
uv run python -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M')"
PATH="$hermes_venv/bin:$PATH" uv run pytest -m e2e -rs
```

The tests take about a minute, mostly for the plugin install. CI has an `e2e` job that runs these
same steps; it has not run on GitHub Actions yet, so the tier is verified locally only.

**Live.** The `live` test starts a real Hermes gateway as the `e2e` tier does, with real models on
its profiles, real GPT-Live, and the Decisions router (`knn` as its fast path). It streams spoken WAV
prompts through the web transport at real-time pace and passes when "How is my portfolio doing?"
reaches `stock-picker`, its run completes, and the spoken result arrives within 60 s. It skips
unless `HERMES_DUPLEX_LIVE=1` and `OPENAI_API_KEY` are set, `hermes` is on `PATH` (installed as for
`e2e`), and the WAV fixtures exist.

The spoken fixtures (`portfolio.wav`, `tesla.wav`) are not committed yet; only `silence.wav` is.
Generate them once with OpenAI's text-to-speech API:

```bash
OPENAI_API_KEY=... uv run python -m tests.live.make_audio_fixtures
```

| Variable | Default | Sets |
|---|---|---|
| `HERMES_DUPLEX_LIVE` | unset | `1` opts in |
| `OPENAI_API_KEY` | unset | the key for GPT-Live, Decisions, and by default the profiles' model |
| `HERMES_DUPLEX_LIVE_MODEL` | `gpt-5-mini` | the model `general` and `stock-picker` run on for every effort; pick a reasoning model, since Hermes sends it the routed effort |
| `HERMES_DUPLEX_LIVE_BASE_URL` | `https://api.openai.com/v1` | the OpenAI-compatible chat completions API that serves that model |
| `HERMES_DUPLEX_LIVE_KEY_ENV` | `OPENAI_API_KEY` | the variable holding that API's key; it must not start with `HERMES_`, which the gateway does not inherit |

Then run `PATH=<hermes venv>/bin:$PATH HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs`.

## Known limitations

- **Cloudflare Workers AI Clef** cannot stand in for Jev through `router.jev.base_url`. The backend
  always posts to `{base_url}/v1/systemone` and reads the System One response shape; Clef needs a
  different path and wraps the response in its own envelope.
- **GPT-Live pre-warm routing** (starting the router from input transcript deltas before the
  delegation arrives) is not implemented. Routing starts once the delegation's words are in.
- **Grok Voice details are unverified against the live service:** whether it wants `xai_`-prefixed
  voice ids or plain ones, whether it accepts the flat session shape, whether it honors `tool_choice`
  on `response.create`, whether barge-in cancels the response on its own without
  `interrupt_response`, and whether it supports `conversation.item.delete`. The
  [live checklist](docs/live-checklist.md) covers each one.
- **Grok's cumulative `transcription.updated` events are ignored.** Input transcripts come only from
  the OpenAI-style transcription delta and completed events.
- **Results from earlier tasks are read in full wherever you are next heard.** A task's result,
  failure, or cancel that you did not hear in its own conversation is announced in whichever
  conversation next hears you, on any transport. That includes a shared Discord voice channel, where
  everyone present hears it, people not in `people` included; for example, the answer to a private
  question asked on the web page is read out in the Discord channel you join next.
- **An end queued but not yet spoken when the service stops is lost.** Untold results are kept in
  memory; after a restart only tasks that were still open are picked up again.
- **The CI `e2e` job has not run on GitHub Actions yet**; the real-Hermes tier has passed locally.

## License

MIT; see [LICENSE](LICENSE). `hermes_duplex/transports/discord/receiver.py` is adapted from Hermes
Agent's Discord voice receiver (MIT, © 2025 Nous Research); see [NOTICE](NOTICE).
