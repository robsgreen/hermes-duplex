# hermes-duplex

A full-duplex voice front end for [Hermes Agent](https://github.com/NousResearch/hermes-agent). You talk
to a realtime voice model in a Discord voice channel or on a web page. The voice model chats with you
directly and hands real work to Hermes. Each person talks to one Hermes profile at a time, their
**active profile**, until they ask to switch. A router picks the effort level and model for each
task, and may offer to switch when a request clearly belongs to another profile. The task runs on the
active profile through the Hermes API server while the conversation goes on. Results come back as
spoken reports.

- **Voice providers:** OpenAI GPT-Live (client delegation), OpenAI Realtime, and xAI Grok Voice. You
  pick one in config.
- **Transports:** a Discord bot of its own (separate from Hermes's Discord bot) that receives
  DAVE-encrypted voice, and a browser page.
- **Profiles:** one active profile per person, saved across conversations and restarts. Switch by
  voice, with `/duplex profile` on Discord, or with the picker on the web page.
- **Routing:** a local phrase matcher plus OpenAI Decisions or TypeSafe Jev pick the effort, which
  selects a model from the active profile's effort table, and suggest a switch when a request fits
  another profile clearly better.
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
up again and tells you what finished while you were away ("While you were away, "how is my portfolio doing" finished: …"),
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

The plugin runs its own bot with its own token; Hermes's Discord bot keeps working untouched. Create a
separate Discord application for it: never reuse the token of Hermes's Discord bot, or of a bot a
Hermes profile runs through its own gateway, so the voice bot and those bots never share an identity.

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

When the bot logs in, it registers the `/duplex join`, `/duplex leave`, and `/duplex profile` slash
commands. Discord can take a while to show newly registered commands.

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
| `hermes` | `api_base` of the API server, and `profile_keys_env`: profile → env var holding that profile's `API_SERVER_KEY`. A profile without a key cannot run tasks; the voice says it is not configured. Optional: `voice_context_files` (any of `soul`, `user`, `briefing`; none by default), `voice_context_max_chars` (4000, at most 8000), and `briefing_refresh_h` (6); see [Hermes context in the voice prompt](#hermes-context-in-the-voice-prompt). |
| `provider` | `name` (required, never inferred), `model`, `voice`, `api_key_env`, and `turn_detection` for the Realtime family. |
| `router` | `backend` (`decisions`, `jev`, or `knn`), optional `fast_path: knn`, `deadline_ms` (300), `default_profile` (`general`: where people start when their own `default_profile` is unset and they may use it), `suggest_switch` (true: may ask to switch when a request clearly fits another profile the speaker may use), `suggest_threshold` (0.85, greater than 0 and at most 1: how sure it must be before asking), extra `control_examples`, and the `decisions` or `jev` settings (`api_key_env`, optional `model` and `base_url`). |
| `profiles` | Each Hermes profile people can talk to: `description` and `examples` steer switch suggestions; `models` and `default_model` pick the model; `aliases` are other names people say for it, compared as spoken (case, punctuation, hyphens, and a leading "the" ignored): each needs a word besides "the", none may start with "me", "us", or "you" or be or start with another profile's name ("general news" while `general` is a profile), and no two profiles share one; `voice_context` (optional, at most 2000 characters) goes into the voice prompt while the profile is active. |
| `people` | Each person: `discord_id`, `web_token_env`, the `profiles` they may use (at least one; `"*"` for all), and an optional `default_profile` they start on, which must be one of those. Optional: `hermes_owner: true` for the one person Hermes's `USER.md` and briefing are about, and `voice_context` (at most 2000 characters), notes about the person the voice prompt carries while the conversation is with them. |
| `transports` | `discord: {enabled, bot_token_env}` and `web: {enabled, host, port}`; both off unless enabled. |
| `audio` | `pacer_lead_ms` (200), `gate_threshold_dbfs` (-35.0), and `playback_queue_s` (5). |
| `tasks` | `restore_max_age_h` (24): after a restart, open tasks created longer ago than this are marked unknown silently instead of being followed again. |
| `assistant` | Optional: `name` (`Hermes`), `persona` (text that replaces the prompt's style and tone), and `backchannel` (`none`, `moderate` (default), or `frequent`; GPT-Live only). See [The assistant and its prompts](#the-assistant-and-its-prompts). |

### Profiles and the model table

A profile here is a Hermes profile: a separate Hermes agent with its own memory, tools, and persona.
Hermes Desktop's Bot Mode calls the same thing a "Bot". Voice conversations do not show up as Desktop
Bot Chats: each runs in its own Hermes session per person and profile (see
[Active profile and switching](#active-profile-and-switching)). Bot titles are not read from Hermes;
to let people say a Bot's title, add it to the profile's `aliases` by hand.

Every task runs on the speaker's active profile. The router does not pick the profile or the model.
It picks an effort (`low`, `medium`, or `high`), and the model comes from the active profile's row of
the table. Fill in your own models; the example ships `<fill>` placeholders:

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
`medium`. `router.default_profile` must be defined under `profiles`.

### Active profile and switching

Each person has one active profile. It starts at their `people.<name>.default_profile`, else
`router.default_profile` when they may use it, else the first profile in their `profiles` list. It
stays until they switch, and each switch is saved in `$HERMES_HOME/duplex/active_profiles.json`, so
it holds across conversations and restarts. Hand-offs run in the Hermes session
`duplex-<person>-<profile>`, so each profile keeps its own context per person.

There are four ways to switch:

- **By voice**, on any provider: "switch to stock-picker", "let me talk to the stock picker", "ok,
  back to general". A switch phrase ("switch to", "talk to", "back to", "change to") that opens what
  you say, or one of its sentences (a comma doesn't start one: "if rates fall, switch to stocks" is
  a normal request), after lead-ins such as "let me",
  "can I", "can we", "I need to", "I'd rather", "please", "go", "hmm", "ok", "I'd like to", or
  "thanks" (one after another: "hmm, ok, let me talk to stocks"), followed by a profile's name or one
  of its `aliases`, switches; the request never reaches the router or Hermes. "Switch to stocks and
  check NVDA" switches, then hands off "check NVDA" on the new profile, and so does "talk to stocks
  about Tesla" with "Tesla".
  Mid-sentence only an exact profile name switches, never an alias. A phrase with a negation earlier
  in its sentence ("don't switch to stocks"), one followed by "me", "us", or "you" ("talk to me about
  the weather"), or one whose target is followed by more words ("switch to stocks from bonds",
  "remind me to talk to Sarah") is no switch: it is handed off as a normal request.
- **The `switch_profile` tool**, which OpenAI Realtime and Grok call when you ask by voice, passing
  your words for the agent (a name or a nickname); the session resolves them as above.
- **Discord:** `/duplex profile <name>` (see [Use it](#use-it)).
- **Web:** the **Talking to** picker on the page.

A name resolves to the longest profile name or alias it starts with, a name winning a tie. A short
name that fits no profile (at most three words, as in "switch to finance"), said with nothing else,
or a name that fits several gets "Sorry, which agent do you mean?" with the profiles you may use;
said with a request ("Switch to Celsius. What's the weather in Paris?") or after "back to" ("back to
Tesla, what's it at?"), it is handed off whole as a normal request. A profile you may not
use gets "Sorry, you don't have access to the stock-picker agent, so I didn't switch." Every live
conversation you are in confirms a switch aloud ("You're now talking to stock-picker."), wherever it
was made; a conversation you have left (a Discord voice channel you are no longer in) ignores it. If
your saved profile is removed from config or you lose access to it, you are moved back to your
default and hear "You're back on general." the next time you are heard, once: the saved file is
rewritten, so a restart does not announce it again.

**Suggestions.** When a request clearly fits another profile you may use better than your active
one, the assistant holds it and asks "That sounds like a stock-picker question. Want me to switch?"
Say yes and it switches, then runs the request there. Say no, ask for something else, or say nothing
for 30 seconds after the question is spoken, and it runs on your active profile. Nothing switches
without your yes. You have at most one yes-or-no question waiting at a time: a switch question, or,
when you ask to cancel something that seems not to be your only running task, "The only one running
is "…". Cancel that?" (yes cancels it, no leaves it running). A new question replaces the old one. A
stray yes or no within 30 seconds after your question was settled or timed out, while the assistant
has asked nothing else since, gets "There's no question waiting for your answer right now." and is
never handed to Hermes; a yes to a later offer ("Want me to set a price alert?") is handed off as
usual. A yes or no from someone with no question of their own while another person's question waits,
or just ended, gets "That question was for rob." (that person's name in `people`) and is not run;
only the person asked can answer. It asks only
when the other profile's probability is at least `router.suggest_threshold` (0.85) and at least 0.2
above your active profile's; `router.suggest_switch: false` turns suggestions off. A held request
still waiting when the conversation ends starts on your active profile.

**Voice context.** A profile's optional `voice_context` tells the voice model about the active
profile: what it covers, what to always hand off, how to say its words, and rules to follow. Only the
active profile's `voice_context` is in the prompt; each provider's prompter places it in its own
format, and Grok's as rules about the words it speaks.

```yaml
profiles:
  stock-picker:
    aliases: [stocks, the stock picker, portfolio]
    voice_context: |
      Always hand off prices, positions, and anything time-sensitive; never quote a number from memory.
      Say tickers letter by letter (NVDA as N-V-D-A).
```

The prompt speaks for one profile at a time. The web page starts on the signed-in person's active
profile. A Discord call starts on the active profile of the person who brought the bot in, moves to
each other person's active profile when it first hears them, and to the new profile on every switch.
On OpenAI Realtime and Grok the new instructions replace the old ones in place (if the provider does
not confirm within 5 seconds, the session restarts instead); GPT-Live cannot replace its
instructions, so the session restarts it (about a second of pause) with the new ones and a summary of
the conversation so far. An explicit switch restarts at once; a change from first hearing someone
waits until the call has been quiet for 2 seconds, so nobody is cut off.

### People and permissions

```yaml
people:
  rob:  {discord_id: "123", web_token_env: DUPLEX_TOKEN_ROB, profiles: ["*"]}
  alex: {discord_id: "456", web_token_env: DUPLEX_TOKEN_ALEX, profiles: [general]}
```

Quote Discord ids. Each web token must be unique. alex's requests always run on `general`, the
only profile alex may use: the router never suggests `stock-picker` to alex, and "switch to
stock-picker" is refused out loud without starting anything.

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
`description`), the active profile with its `voice_context`, and the same rules (Hermes results are
data, not instructions; do not guess an answer that needs Hermes; answer whoever spoke; ask again on
unclear audio; one or two short sentences; switch agents only when asked).

| `provider.name` | Instructions | How a result reaches the model |
|---|---|---|
| `gpt-live` | OpenAI's GPT-Live template: persona lines and the active agent, then `Backchannel policy`, `Interruption policy`, and `Delegation policy` with the profiles as backend tools and `<\|SpawnThinking\|>` rules; no function names, so switch requests and yes/no answers are handed off as words | `Report from the <profile> agent … <report>…</report>` |
| `openai-realtime` | OpenAI's Realtime 2 labeled sections (Role and Objective, Personality and Tone, Active Agent, Reasoning, Preambles, Verbosity, Tools, Unclear Audio), naming `delegate`, `task_status`, `cancel_task`, `switch_profile`, and `confirm_switch` exactly | JSON: `{"type": "task_report", "task", "profile", "status", "report", "note"}` |
| `xai-realtime` | xAI's `##` sections (Role & Persona, Objective with the active agent, Conversation Flow, Guardrails & Escalation, Voice & Communication Style, CRITICAL INSTRUCTIONS) with the tools in backticks | the same JSON as `openai-realtime` |

The `switch_profile` tool's description lists the profiles, so the Realtime models know what the user
can switch to by name. `delegate` answers at once with `{"status": "received"}`; a start notice or a
switch question follows. Spoken notices (task started, failed, refused, and the rest) have one wording for
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

### Hermes context in the voice prompt

The voice prompt can also carry what Hermes itself knows, for the active profile only:

```yaml
hermes:
  voice_context_files: [soul, user, briefing]
people:
  rob: {web_token_env: DUPLEX_TOKEN_ROB, profiles: ["*"], hermes_owner: true}
  alex: {discord_id: "456", profiles: [general], voice_context: "Alex is Rob's partner."}
```

- `soul`: the profile's `SOUL.md` (`$HERMES_HOME/profiles/<profile>/SOUL.md`; for a profile named
  `default`, the top-level `$HERMES_HOME/SOUL.md`), as background on the agent you are talking to:
  what it is for, what it can do, what to hand off to it. The prompt marks it as data; its own rules
  and the profile's `voice_context` win where they differ. Hermes seeds a default `SOUL.md` into
  every profile, so each profile has one.
- `user`: the profile's `memories/USER.md`, Hermes's notes on its owner, the person with
  `hermes_owner: true`.
- `briefing`: a short summary of the owner's current projects and priorities that Hermes writes from
  its memory and recent sessions.

**Privacy.** `USER.md` and the briefing go into the prompt only while the owner is alone in the
conversation: signed in on the web page, or in a Discord voice channel where everyone else is a bot.
When someone else joins the channel, they leave the prompt at once (an instruction update, or on
GPT-Live a restart of about a second), and whatever the assistant had not finished saying is cut off,
so the newcomer hears nothing meant for the owner alone; a later reconnect is re-seeded with the open
tasks only, not with what was said before they joined. When the owner is alone again, the texts come
back once the call is quiet. A person's own `voice_context` and their open tasks (queued and running, by topic and agent,
the newest 10) go in while the conversation is with them: the person who signed in or brought the bot
in, then whoever was last heard for the first time or last switched. When that person leaves, the
prompt moves back to the person who brought the bot in, or to the one person left, if either is
there. On a shared Discord call those
two are heard by everyone in the channel, as spoken task status already is. Nothing here logs the
text of a file.

**Rewrites.** Each profile rewrites its own `SOUL.md` and `USER.md` for a voice assistant (short plain
sentences, no tool or formatting rules, within `voice_context_max_chars`), and writes the briefing,
through the API server like any task, with its own key and default model, each in a fresh Hermes
session of its own (`duplex-voice-context-<profile>-<kind>-<id>`, `duplex-voice-briefing-<profile>-<id>`),
never a person's. The `SOUL.md` rewrite is told to say nothing about you or anyone else, since anyone
on a call may hear it. They run in the background: when the service starts, when a file has changed,
and for the briefing when it is older than `briefing_refresh_h` hours. While any call is on, they
wait until the last one ends; a call never waits for one. Until a rewrite of the current file
exists, or when it fails, the prompt carries the file itself, cut at a line or sentence end to
`voice_context_max_chars` (with a warning naming the file and sizes); until a briefing exists there
is none. The results are cached in `$HERMES_HOME/duplex/voice_context/<profile>.<kind>.md`, each with
a `.json` sidecar of fingerprints. Edit a cached file to say it your way: an edit is kept until the
source file changes, or, for the briefing, until a forced refresh. A changed file or a new rewrite
reaches the prompt only at the next connect or reconnect, profile switch, person first heard, or
change of who is on the call; never mid-turn and never with a task change. Open tasks are kept
current in place on OpenAI Realtime and Grok, and on GPT-Live at the next restart (spoken notices
tell of task changes meanwhile). On Grok each text is cut to 2,000 characters, as xAI's guide asks
for short prompts.

```bash
uv run hermes-duplex voice-context show stock-picker     # or: hermes duplex voice-context show stock-picker
uv run hermes-duplex voice-context refresh [stock-picker] [--force]
```

`show` prints the instructions the configured provider's model gets while that profile is active, as
if the owner were alone on the call, and notes on stderr where each text came from (Hermes's
rewrite, your edit, or the file itself); it starts no rewrite. `refresh` has Hermes write every
enabled text of the profile (or of every profile) now, prints one line per text, and exits 1 when one
could not be written; `--force` replaces current texts and your edits.

## Use it

**On Discord.** Join a voice channel and run `/duplex join`, or type `join` in a text channel of
that server. Talk. Ask for something that needs Hermes ("how's my portfolio doing?"); the model says
it is on it, keeps talking with you, and speaks the result when the task finishes. The task runs on
your active profile; if it clearly belongs to another one you may use, the assistant first asks
whether to switch. Ask "what are you
working on?" for the status of your tasks, or "cancel that" to stop your task. The assistant
names each task by its topic, a short quote of what you asked ("Started "how is my portfolio doing"
on the stock-picker agent."), and never by a number: task numbers are internal ids that appear only
in logs, `tasks.jsonl`, `/duplex`, and what the model reads. Name a task the same way ("cancel the
portfolio one", "how's the Tesla thing going?"); with several running and none clearly meant, the
model asks which one first, and if it does not, the assistant asks you by topic rather than
guessing. `/duplex leave` ends the conversation. The bot joins one channel per server.

`/duplex profile <name>` switches your active profile; it takes a profile name or alias, and
autocompletes the profiles you may use. The reply is private. When you are in a live conversation it
also confirms the switch aloud there. Otherwise, including when you have left the voice channel or
are in it but have not spoken yet, the reply says the switch applies the next time you talk; in the
call it takes effect when you are first heard. When you come back to a call you left, you are taken
in afresh when you next speak: the voice model's instructions follow a switch you made while away,
and tasks left for you meanwhile are picked up.

**On the web.** With `transports.web.enabled: true`, open `http://127.0.0.1:8765/` (the configured
host and port), enter your token (the value of your `web_token_env` variable), and press
**Connect**. The page shows the transcript, your task list, and a **Talking to** picker of the
profiles you may use, set to your active profile; choosing one switches it. Browsers allow microphone access
only on `localhost` or HTTPS, so put the page behind HTTPS before you serve it to other machines.

**In Hermes.** `/duplex` reports the provider, the active conversations, the running tasks, and each
person's active profile for the service in that gateway.

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

`eval-router` routes a file of labelled phrases, each said while the speaker is on a given `active`
profile, and prints, for each configured backend alone and then for the composite router: effort
accuracy and off-by-one rate; how many switch suggestions it made, with their precision and recall
(it should suggest exactly when a phrase belongs to a profile other than the active one); how well it
tells status and cancel requests from tasks (control precision and recall, and how often a task is
taken as one and so never runs); how often the top choice is the labelled profile (choice accuracy,
macro-F1, and the confusion matrix); latency p50/p95/p99; and the deadline-miss rate. `--json` prints JSON instead, `--backend NAME` scores only
`knn`, `decisions`, `jev`, or `composite`, and `--config duplex.yaml` uses that file's router
settings and keys (every env var it names must be set). Without `--config` it scores `knn` alone and
a composite router over `knn` with default settings. The phrase file's format is described in
`hermes_duplex/router/eval.py`.

A request is a status or cancel request only on strong evidence: the deciding backend must give
`_control` a probability of at least 0.995. Anything weaker runs as a task on your active profile,
because a request taken as a status question is silently lost while a status question taken as a task
is only a wasted run. The backends see `_control` described as "progress of a task already running,
or cancelling it", so that a new request does not read as one. `tests/fixtures/router_eval/descriptions_only.yaml` holds the same kind of
phrases for profiles configured with only a description, where tasks look most like status phrases.

With the `minishlab/potion-base-8M` embedding model, `knn` scores:

| Measure | `phrases.yaml` (103 phrases) | `descriptions_only.yaml` (40 phrases) |
|---|---|---|
| Choice accuracy | 0.825 | 0.700 |
| Choice macro-F1 | 0.823 | 0.724 |
| Control precision | 1.000 | 1.000 |
| Control recall | 0.645 (20 of 31) | 0.929 (13 of 14) |
| Task taken as control | 0.000 | 0.000 |
| Suggestions made | 20 | 2 |
| Suggestion precision | 1.000 (every suggestion was right) | 1.000 |
| Suggestion recall | 0.526 (20 of the 38 phrases that should have one) | 1.000 (2 of 2) |
| Effort accuracy | 0.306 (`knn` judges no effort, so this is the share labelled `medium`) | 0.308 |
| Latency p50 / p95 / p99 | under 1 ms | under 1 ms |

Choice accuracy counts the backend's top choice, so a `_control` below the bar counts as `_control`
there though it runs as a task. Before the bar, `knn` took 0.462 of the description-only tasks (and
0.056 of `phrases.yaml`'s) as status or cancel requests.

Scoring `decisions` and `jev` needs their API keys, and has not been done yet.

## Test

| Tier | Command | Needs |
|---|---|---|
| Lint, format, types | `uv run ruff check . && uv run ruff format --check . && uv run pyright` | nothing |
| Default (1310 tests: unit, contract, in-process end-to-end) | `uv run pytest -m "not live and not e2e"` | nothing |
| Real Hermes end-to-end (6 tests) | `PATH=<hermes venv>/bin:$PATH uv run pytest -m e2e -rs` | hermes-agent installed (below) |
| Live (1 test) | `PATH=<hermes venv>/bin:$PATH HERMES_DUPLEX_LIVE=1 uv run pytest -m live -rs` | hermes-agent, real API keys, the spoken WAV fixtures (below); spends money |
| Router eval | `uv run hermes-duplex eval-router tests/fixtures/router_eval/phrases.yaml --backend knn` (or `descriptions_only.yaml`) | nothing for `knn` (it downloads its embedding model once); keys for `decisions` and `jev` |
| Manual | [docs/live-checklist.md](docs/live-checklist.md) | a Discord server, keys, people to talk |

Install the toolchain first with `uv sync --frozen`.

**Real Hermes end-to-end.** The `e2e` tests (`tests/e2e/test_real_hermes.py`,
`test_real_hermes_flows.py`, and `test_real_hermes_context.py`; the other `tests/e2e/` files run in
the default tier) start a real Hermes gateway in a temporary `HERMES_HOME` with two profiles backed
by a fake LLM, install this plugin from your working tree, and drive calls through the web transport
with the fake voice provider: a routed run, a refusal, a topic switch, a local status answer,
`doctor` against the gateway, and a `SOUL.md` rewritten on its own profile, cached, and carried into
a call's instructions with a hand edit. They skip
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
- **The voice prompt speaks for one profile at a time.** On a Discord call with two people on
  different active profiles, each person's hand-offs still run on their own profile, but the voice
  model's instructions (and `voice_context`) are those of the last person who switched or was first
  heard.
- **Switch phrases are matched literally.** A switch by voice needs "switch to", "talk to", "back
  to", or "change to" followed by a profile's name or alias (see
  [Active profile and switching](#active-profile-and-switching)). On GPT-Live, other phrasings are
  handed off as a normal request on the active profile, where a switch suggestion may follow; OpenAI
  Realtime and Grok can still switch through the `switch_profile` tool. A switch phrase said alone
  with a short name that is no profile, such as "switch to Celsius", is taken as a switch and
  answered "Sorry, which agent do you mean?" instead of being handed off; said with anything more
  ("switch to Celsius and tell me the weather") it is handed off.
- **An end queued but not yet spoken when the service stops is lost.** Untold results are kept in
  memory; after a restart only tasks that were still open are picked up again.
- **The CI `e2e` job has not run on GitHub Actions yet**; the real-Hermes tier has passed locally.

## License

MIT; see [LICENSE](LICENSE). `hermes_duplex/transports/discord/receiver.py` is adapted from Hermes
Agent's Discord voice receiver (MIT, © 2025 Nous Research); see [NOTICE](NOTICE).
