# Live checklist

Manual checks for what the automated tiers cannot reach: real Discord voice with DAVE encryption,
real provider barge-in, several people talking, long calls, and how each real voice model handles
profile switches. Run them before a release and
after upgrading discord.py, `davey`, or a provider model.

## Before you start

- A Discord server where you can invite bots, the bot set up as the README's "Set up the Discord
  bot" describes, and a second person (or a second Discord account on another device) for the
  multi-speaker checks.
- A Hermes gateway with the API server and two profiles, `general` and `stock-picker`, each with
  its own `API_SERVER_KEY` and a working model.
- A `duplex.yaml` from `duplex.example.yaml` with Discord and web enabled, your Discord id under
  `people` with `profiles: ["*"]` and `default_profile: general`, the example's `aliases` and
  `voice_context` on `stock-picker`, and keys for GPT-Live (`OPENAI_API_KEY`) and xAI (`XAI_API_KEY`).
- Run the service standalone, so its log is in front of you:

  ```bash
  uv run hermes-duplex doctor --config duplex.yaml
  uv run hermes-duplex serve --config duplex.yaml
  ```

  Every `doctor` row should read `pass` before you go on.

Everyone starts on their default profile. When a request in an item draws a switch question ("That
sounds like a stock-picker question. Want me to switch?"), say yes unless the item says otherwise;
the Profiles items check the question itself.

Each item lists its steps and what you should see. Note the date, provider, model, and outcome of
each run next to the item when you file results.

## Discord

### 1. Bot login and `/duplex` registration

1. Start the service.
2. In Discord, type `/duplex` in a text channel of your server.

Expected: the log shows `Discord bot logged in as <bot name>` and no `could not register the /duplex
Discord commands` warning. Discord offers `/duplex join`, `/duplex leave`, and `/duplex profile`. Newly registered
commands can take a while to appear; restarting the Discord client refreshes them.

### 2. Join

1. Join a voice channel.
2. Run `/duplex join`. Leave with `/duplex leave`, then join again by typing `join` in a text channel.
3. While the bot is in the channel, run `/duplex join` again.

Expected: the bot enters your channel, and the slash command replies (only to you) `Joined
#<channel>`. The typed `join` gets a reply in the text channel. The third step answers `I'm already
in #<channel>; use /duplex leave first.` The log shows `Discord conversation discord-<id>-<n>
started in <channel>` and `Discord voice receiver started`. If the Message Content intent is off in
the developer portal, Discord refuses the bot's gateway connection: the log shows `the Discord bot's
gateway connection ended` with `PrivilegedIntentsRequired`, and the bot never comes online.

### 3. DAVE receive and talking

1. Check that the channel is end-to-end encrypted (Discord shows the encryption badge in the voice
   panel).
2. Say "Hi, can you hear me?" and have a short exchange.
3. Run `/duplex leave`.

Expected: the model answers what you said, in time, with no white noise and no talk about things
you never said. The receiver's stop line in the log reads `frames ok=` with a large count and
`decrypt failed=0` (a handful at join, while keys settle, is fine); `stale key dropped` stays small.

### 4. Delegate a task

1. Say "Switch to stock-picker", then "How's my portfolio doing?"
2. While it runs, ask something small ("what time is it?").
3. Then say "And what about Tesla?"

Expected: the switch is confirmed ("You're now talking to stock-picker.") and starts no Hermes run.
For the portfolio request the model says it is on it and keeps talking. The log shows a `route
speaker=<you> profile=stock-picker ...` line, and Hermes shows a run on `stock-picker` with session
`duplex-<you>-stock-picker`. The second question is answered right away. When the run finishes, the
model relays the result in its own words. The Tesla follow-up also logs `profile=stock-picker` and
reuses the same Hermes session.

### 5. Status and cancel

1. Start a slow task ("research three dividend stocks in depth").
2. Ask "what are you working on?"
3. Say "cancel that".

Expected: the status answer names the task by its topic (what you asked), its agent, and its state,
never by a number, and for a running task says it will tell you when it finishes. The cancel stops
the run in Hermes (its status turns cancelled) and the model confirms `Cancelled "<topic>"`. Neither request
creates a Hermes run of its own. With GPT-Live both are routed as `_control` (the `route` line shows
`control=status` or `control=cancel`); Realtime models call the `task_status` and `cancel_task` tools
instead. The router takes a request as status or cancel only on strong evidence, so with GPT-Live a
question worded far from the built-in phrases may run as a task instead; that is by design.

Then, with a profile configured with only a `description` (no `examples`), ask for something that is
plainly a task ("what's the weather tomorrow?", "remind me to call mom"). Expected: each runs as a task
(a start notice, a Hermes run, `control=None` on its `route` line), never a status answer such as "no
tasks running".

### 6. Multi-speaker attribution

Two allowlisted people, A with `profiles: ["*"]` and B with `profiles: [general]`.

1. A says "switch to stock-picker", then asks "how's my portfolio doing?"; B stays quiet.
2. B says "switch to stock-picker"; A stays quiet.
3. B asks "how's my portfolio doing?", then "what's the weather tomorrow?"
4. A and B talk over each other, then A asks for something clearly.

Expected: step 1 runs on `stock-picker` for A. Step 2 is refused out loud ("Sorry, you don't have
access to the stock-picker agent, so I didn't switch.") and creates no run. Both requests in step 3
run on `general` with session `duplex-<B>-general`, and B is never asked to switch to `stock-picker`.
A's active profile stays `stock-picker` throughout. In step 4 the task is credited to whoever spoke
most in the 4 s before the request, which the `route speaker=` line shows.

### 7. Allowlist removal

1. Have a third person, not under `people`, join the channel and talk over you; ask them to run
   `/duplex join` and `/duplex leave`.
2. Stop the service, remove B from `people`, start it again, and rejoin. Have B talk and ask for
   something.

Expected: the model never responds to the outsider or to B after removal, and neither can be
credited with a task. Their `/duplex` commands get `Sorry, you are not on this voice assistant's
list of people.` (a typed `join` from them is ignored without a reply).

### 8. Rejoin after a move or disconnect

1. While the model is talking or a task runs, drag the bot into another voice channel, then talk to
   it there.
2. Right-click the bot and choose **Disconnect**.
3. With exactly one allowlisted person in the channel (and nobody else besides this bot), keep talking
   through the rejoin without leaving or toggling mute (Discord may not resend the SPEAKING event
   then).
4. Repeat step 3 with two members in the channel: two allowlisted people, then one allowlisted and
   one not, then one allowlisted person and another bot (a music or TTS bot) playing audio.
5. Block the bot from the channel (deny it **Connect**), then disconnect it.

Expected: after a move, the bot rejoins the new channel; after a disconnect, the old one. The log
shows `rejoining`, then `rejoined` within a few seconds, and the provider session carries on (no
`voice provider session closed` line): the model remembers the conversation and running tasks still report. In step 3
the log shows `Mapped ssrc=<n> -> user=<id> (the only person in the channel)` and the model hears you.
In step 4 the unmapped audio is dropped until each person's next SPEAKING event (they stop and start
talking again), and nobody else's voice is credited to them, the other bot's audio included. In step 5, after three failed attempts
the bot posts `Ending the voice conversation: I lost the voice connection to <channel> and could not
rejoin.` in the text chat and the conversation ends.

## Barge-in

### 9. GPT-Live barge-in (loudness gate)

With `provider.name: gpt-live`:

1. Ask for something long ("tell me a long story about a lighthouse").
2. Talk over the model at normal volume a few seconds in.
3. Cough, tap the desk, and let background noise (music, a fan) run while the model talks.

Expected: within a fraction of a second of you speaking, the model's audio stops (the log shows
`barge-in: flushed playback after <ms> ms of the item, dropped <ms> ms held`) and the model then answers you or
carries on, as it decides. Step 3 should not cut the model off; if it does, the gate threshold is too
low (next item).

### 10. Loudness-gate threshold tuning

The gate, and Discord's who-was-voiced marking, trip when a person's tick is louder than
`audio.gate_threshold_dbfs` (default -35.0).

1. Repeat item 9 with the threshold at -45, -35, and -25 (restart the service each time).
2. For each, note whether normal speech trips the gate at once, and whether noise trips it.
3. Check item 6 step 1 at the chosen value: a quiet speaker must still get credit.

Expected: a value where normal speech flushes within about 200 ms, coughs and steady noise do not,
and quiet speakers are still credited. Record the value, microphone, and room in the results. If no
value satisfies all three, file the readings; the default needs changing.

### 11. OpenAI Realtime barge-in

With `provider.name: openai-realtime`:

1. Ask for something long, and talk over the model a few seconds in.
2. Ask "what did you just say?"
3. Ask for a task, then interrupt the model while it is still starting to answer.

Expected: the audio stops at once. In step 2 the model recalls only what you heard before you cut
in, since the session truncated the interrupted item to the played ms. In step 3 a function call from
the interrupted response does not start a Hermes task; only a request the model makes again after
the interruption does. The log shows `barge-in: flushed playback after <ms> ms of the item`, and in step 3
`ignored call <id> (delegate): its response was interrupted`.

## Grok

### 12. Switch to Grok

1. Change `provider` to `name: xai-realtime`, `voice: ara`, `api_key_env: XAI_API_KEY`,
   `turn_detection: {type: server_vad}`, and remove `model`. Change nothing else.
2. Run `doctor`, then `serve`, join, and repeat items 3, 4, and 5.

Expected: `doctor` shows `provider xai-realtime pass`. The log shows `xai-realtime session started
(model grok-voice-latest)` with no `rejected the session` error. The call, delegation, status, and
cancel behave as with OpenAI, and the voice is Ara.

### 13. Grok protocol details

These are unverified against the live service. Run each and record what happened.

1. **Voice id prefix.** With `voice: ara` the adapter sends `xai_ara`. If the session is rejected or
   the voice is wrong, try `voice: xai_ara` (sent unchanged) and note which xAI accepts.
2. **Flat session shape.** In item 12 the session starts; check that turn detection and voice took
   effect (the model stops when you stop talking; the voice is the one configured). Note any
   `voice provider error` lines in the log.
3. **`tool_choice` on `response.create`.** xAI has no out-of-band responses, so results are spoken
   through a system item and `response.create` with `tool_choice: "none"`. Delegate a task and let
   it finish: the model should speak the result and not call `delegate` again for it.
4. **Barge-in without `interrupt_response`.** The xAI session does not send `interrupt_response`,
   and truncate is off. Repeat item 11 step 1: note whether Grok stops speaking on its own when you
   talk over it.
5. **`conversation.item.delete`.** After a result is spoken, the adapter deletes its system item.
   Check the log for an `error` event after the announcement, and ask "what did you just tell me?"
   twice: the model should not repeat old results as new.

Expected: record yes or no and any error text for each. A "no" is a bug to file against
`hermes_duplex/providers/realtime/` (the xAI dialect is `xai/dialect.py`).

## Long calls

### 14. A 60-minute call

1. With `openai-realtime` (sessions capped at 60 minutes), stay in a call for 65 minutes. Talk every
   few minutes, and start a slow task at about minute 58.
2. Repeat with `gpt-live` (its cap is undocumented; note when, or whether, it closes).
3. On the web transport, keep the page open across the same mark.

Expected: when the provider closes the session, the log shows `voice provider session closed: <reason>`
and then `reconnected the voice provider` within a few seconds. The model carries on
without mentioning the drop and knows what was said recently and which tasks are open. The task
started at minute 58 has its result spoken on the new session. Speaker credit still works after the
reconnect (item 6 step 1). If reconnecting fails three times, the web page shows "I lost the
connection to the voice service..." and the conversation ends; Hermes tasks keep running.

### 14a. Tasks outlive the conversation

1. Start a slow task ("research three dividend stocks in depth"), then end the conversation
   (`/duplex leave`, or close the web page) before it finishes.
2. Wait until Hermes shows the run completed, then join again (on Discord or the web) and say
   anything.
3. Start another slow task, stop the service (Ctrl-C on `serve`) while it runs, start it again, wait
   for the run to finish in Hermes, and join again.
4. Start a slow task, leave, rejoin while it still runs, and ask "what are you working on?", then
   "cancel that".
5. Join on Discord and on the web page at once. Start a slow task on the web page, close the page,
   and keep talking on Discord until the run finishes in Hermes.
6. With `gpt-live`, ask "how's the top 5 stocks report going?" while you have no task 5.
7. Stop the service, set `tasks: {restore_max_age_h: 0.01}`, start a slow task, stop the service
   while it runs, wait a minute, start it again, and join.

Expected: in steps 2 and 3 the model tells you "While you were away, "<topic>" finished: …" and
relays the result, shortly after you first speak; nobody else's tasks are announced to you. In step 4
the status names the old task as running and the cancel stops its Hermes run. In step 5 the Discord
conversation reports the web task's result when it finishes, read in full in the channel. In step 6
the answer is the summary of your tasks by topic, not "I couldn't find that task", and no task number
is spoken. In step 7 nothing is announced,
the status names no old task, and the log says the task was marked unknown without restoring it.

## Prompts

### 15. Each provider follows its own prompt

Each provider gets instructions in its vendor's recommended shape (README "The assistant and its
prompts"; the exact text is in `tests/fixtures/prompts/<provider>/instructions.txt`). With
`gpt-live`, then `openai-realtime`, then `xai-realtime`:

1. Ask "how's my portfolio doing?", then ask a small question while it runs.
2. Ask "what are you working on?", then "cancel that" on a slow task. Then start two slow tasks and
   say "cancel that" without naming either. Start two more and cancel one by what it is about
   ("cancel the weather one"), then ask "how's the other one going?". While a task runs, say "check again".
3. Set `assistant: {name: Iris, persona: "Be dry-witted.", backchannel: frequent}`, restart, and ask
   "what's your name?"; with `gpt-live`, tell a long story and listen for listening sounds.
4. On `gpt-live`, ask for something with a long, number-heavy answer (for example "list the closing
   price and daily change of the twenty largest S&P 500 stocks").

Expected: in step 1 the model says one short line, hands the request off, keeps talking, and relays
the result in its own words, summary first, without saying the task is done before the result
arrives. On `openai-realtime` and `xai-realtime` it never reads the JSON report aloud as JSON. In
step 2 status and the first cancel (one task running) answer without a preamble or a confirmation
question; with two tasks running and none named, the model asks which one first, naming each by its
topic, then cancels the one you name without asking again. "Cancel the weather one" cancels that
task, not the latest: on `openai-realtime` and `xai-realtime` the `cancel_task` call carries `about`
(or the `task_id` an earlier `task_status` result gave it); on `gpt-live` the session matches your
words to the task's topic. Throughout step 2 no task number is spoken, by the model or by a notice. "Check again" gets a status answer and starts no second
Hermes run. In step 3 the model answers "Iris" in a dry-witted tone; `gpt-live` backchannels more often than with the default `moderate`. In step 4 the whole report is
spoken (the log shows no GPT-Live error for the commentary appends, which it splits into parts of at
most 500 characters), ending with "…more in Hermes" when it was cut to 1250 characters.

## Profiles

Run items 16 to 19 and 24 with `gpt-live`, then `openai-realtime`, then `xai-realtime`, and note which
provider each result is for. Start each on `general` (say "back to general" first if needed).

### 16. Switch by voice

1. Say "switch to stock-picker".
2. Say "let me talk to the stock picker" (an alias for the profile already active).
3. Say "switch to another agent", then "switch to Sarah".
4. Say "back to general and what's the weather tomorrow?"
5. Stop the service, start it again, rejoin, and ask "what's the weather?"
6. Say "don't switch to stocks", then "remind me to talk to Sarah tomorrow", then "should I move my
   savings back to stocks?"
7. Say "talk to stocks about Tesla".
8. Say "Hmm, can we go back to general?", then "Switch to Celsius. What's the weather in Paris?",
   then "back to Tesla, what's it at?"
9. On Discord: leave the voice channel, run `/duplex profile stock-picker`, rejoin, and say "what's
   NVDA at?"
10. Say "go to the stock trader profile", then "back to general", then "use my portfolio agent",
    then "back to general", then "connect me with the investing agent, what's Apple at?", then "back
    to general", then "go to stocks and check NVDA".
11. Say "remind me to go to the dentist on Friday", then "can I talk to you about my weekend plans",
    then "connect me with the Sarah agent", then "can I talk to Sarah about the stock picker", then
    "change my profile picture", then "find me a travel agent".
12. On Discord, run `/duplex profile the stock trader agent` (typed, not picked from the list), then
    `/duplex profile the stock trader`.

Expected: step 1 is confirmed aloud ("You're now talking to stock-picker."; on `gpt-live`,
"Switching you to stock-picker." just before a short pause) and the log shows `switch
of <you> to profile stock-picker: switched` and `voice instructions now speak for profile
stock-picker`; no Hermes run starts. On `openai-realtime` and `xai-realtime` the model calls
`switch_profile` or hands the words to `delegate`; on `gpt-live` the words are handed off. Step 2
answers "You're already talking to stock-picker." Step 3 answers "Sorry, which agent do you mean?
You can switch to stock-picker or general." to the first and nothing changes; the second is handed
off as an ordinary request (only a near-exact profile name switches without "agent"). Step 4 switches to `general` and
then runs the weather request there, with no switch question. In step 5 the request runs on
`general`, the profile saved before the restart (`$HERMES_HOME/duplex/active_profiles.json`). In
step 6 nothing switches: each is handed off as an ordinary request on `general` (the last may draw a
switch question; answer no). Step 7 switches to `stock-picker` and then hands off "Tesla" there.
In step 8 the first switches to `general` (on `gpt-live` too); the other two never get "which agent
do you mean?": each is handed off whole as an ordinary request. In step 9 the reply says the switch
applies next time; once you speak again the log shows `voice instructions now speak for profile
stock-picker` (on `gpt-live` a restart once the call is quiet), no "You're now talking to" is spoken,
and the request runs on `stock-picker`. Step 10 switches to `stock-picker` each time without a
Hermes run and no `route` line for them: each is read by the switch parser and resolved by meaning
(the log shows `switch target 'stock trader' by meaning: stock-picker`, then `'portfolio'`, then
`'investing'`); "what's Apple at?" then runs on `stock-picker`, and the last switches and then runs
"check NVDA" there. In step 11 every one runs as an ordinary task on `general` (a `route` line each;
the Sarah one may draw a switch question: answer no), and none gets "Sorry, which agent do you
mean?". In step 12 the first switches to `stock-picker`; the second names no profile (no "agent",
and no near-exact name) and is answered with the profiles you may use.

### 17. Switch suggestions

1. On `general`, ask "what did NVDA do today?" and answer "yes".
2. Say "back to general", ask "what did NVDA do today?" again, and answer "no thanks".
3. Ask it once more and say nothing for 30 seconds.
4. Ask it once more, then ask "what's the weather tomorrow?" before answering.

Expected: each time the assistant asks "That sounds like a stock-picker question. Want me to
switch?" and the log shows `holding a request of <you> while asking to switch to stock-picker`. On
`openai-realtime` and `xai-realtime` the answer arrives as a `confirm_switch` call (on
`openai-realtime`, "yes" must give `confirm_switch` with `accept: true`, not a `delegate` call); on
`gpt-live` as a handed-off "yes" or "no". In step 1 the switch is confirmed and the NVDA request runs
on `stock-picker`. In step 2 it runs on `general` and nothing switches. In step 3, 30 s after the
question was spoken, the log shows `did not answer their question; ending it` and it runs on
`general`. In step 4 the NVDA request runs
on `general` without an answer, and the weather request is handled as a new request. No answer is
ever started as a Hermes run of its own. If no question comes in step 1, record the `route` line's
`top3` and `source`: the suggestion needs `stock-picker` at 0.85 or more and 0.2 above `general`.

### 18. Voice context

1. Switch to `stock-picker` and ask "what's NVDA trading at?"
2. Ask the model to buy ten shares of NVDA.
3. Switch to `general` and ask "what's the ticker for Nvidia?"

Expected: on `stock-picker` the model hands the price off rather than quoting one from memory, says
tickers letter by letter (N-V-D-A), and asks you to confirm before handing off the buy. On `general`
none of that `voice_context` applies. Note how closely each provider follows it; Grok gets it as
rules about the words it speaks.

### 19. GPT-Live switch pause

With `gpt-live`:

1. Start a slow task, then say "switch to stock-picker" while it runs.
2. Right after the confirmation, ask "what did we just talk about?"
3. Ask a question with a long answer, and while it is being spoken run `/duplex profile general` on
   Discord (or pick `general` on the web page).
4. Ask another long one, and while it is spoken run `/duplex profile stock-picker`, then at once
   `/duplex profile general`.

Expected: the assistant finishes what it is saying, then says "Switching you to stock-picker." and,
once nobody speaks, only then the log shows `restarting the voice provider for profile stock-picker`, then `reconnected
the voice provider` within about a second; the pause is short, the conversation carries on, and the
restarted session does not say "You're now talking to stock-picker." too. In step 3 the answer is
not cut off: the restart comes after it and after "Switching you to general."; if you start
speaking right after that line, the restart waits until you stop. If the slow task's result arrives
around the line, it is heard in full or said again after the restart. In step 4 the
assistant says "You're now talking to general." without a restart (the call was already on
`general`). The
model remembers the recent conversation, the slow task's result is still spoken when it finishes,
and the conversation does not end even after several switches in a row. With `openai-realtime` and
`xai-realtime` the same switch causes no restart: the log shows no `restarting the voice provider`
line and no `rejected the new instructions` warning, and the switch is confirmed without a 5 s
delay. On `xai-realtime` this checks that Grok acknowledges an instructions-only `session.update`
with `session.updated`; if every switch logs `could not update the voice instructions; restarting
the provider` after 5 s, it does not. After the switch, ask "what are you working on?" and start a
task: the tools (`task_status`, `delegate`) still work.

### 20. `/duplex profile` on Discord

1. Without being in a voice conversation, run `/duplex profile` and look at the autocomplete list.
   Pick `stock-picker`.
2. Join a voice call, ask "what's NVDA trading at?", then run `/duplex profile general` (or the alias
   of a profile) in a text channel while still in the call.
3. As a person with `profiles: [general]`, run `/duplex profile stock-picker`.
4. In Hermes, run `/duplex`.
5. Leave the voice channel while the bot stays in it with someone else, then run `/duplex profile
   stock-picker`.
6. Have a third allowlisted person, who has not spoken in this call yet, join the channel, stay
   silent, and run `/duplex profile stock-picker`. Then have them speak.

Expected: autocomplete lists only the profiles you may use. Step 1 replies privately "You're now
talking to stock-picker. You're not in a voice conversation, so it applies the next time you talk to
me.", and step 2's request runs on `stock-picker`. The switch in step 2 replies privately "You're now
talking to general." and the call confirms it aloud. Step 3 replies "Sorry, you don't have access to
the stock-picker agent, so I didn't switch." Step 4 lists each person's active profile under
`Active profiles:`. Step 5 replies with the "applies the next time you talk to me" text and the call
says nothing (the log shows `switched to stock-picker after leaving the conversation; ignored`). In
step 6 the reply also says it applies next time, since the call has not heard them; when they speak,
the instructions move to their profile (`voice instructions now speak for profile stock-picker`).

### 21. Web picker

1. Connect on the web page.
2. Choose `stock-picker` in **Talking to**, then ask "what's NVDA trading at?"
3. Run `/duplex profile general` on Discord while the page is connected.

Expected: after connecting, the picker lists only the profiles you may use, set to your active one.
Step 2 is confirmed aloud and the request runs on `stock-picker`. In step 3 the page's picker moves
to `general` and the page confirms the switch aloud.

### 22. Two people on different profiles

Two allowlisted people on one Discord call: A on `stock-picker`, B on `general`.

1. A runs `/duplex join`; then A speaks, then B, with `gpt-live` and again with `openai-realtime`.
2. A asks "how's my portfolio doing?"; then B asks "what's the weather tomorrow?"
3. A says "back to general".

Expected: in step 1 the voice prompt starts on `stock-picker`, the profile of A, who brought the bot
in (no `voice instructions now speak for profile` line when A speaks), and moves to `general` when B
is first heard. On `gpt-live` that move restarts the provider only after about 2 s of quiet
(`restarting the voice provider for profile general` after B stops), so B's first words are not
cut off and B's request, if any, is still handed off; on `openai-realtime` it is updated in place. In step 2 A's request runs on
`stock-picker` and B's on `general`, each in their own Hermes session, whatever the voice prompt
speaks for. Step 3 is confirmed aloud and changes nothing for B. This is the documented limitation:
the prompt speaks for one profile at a time, the last one a person was first heard on or switched to.

### 23. A saved profile that is gone

1. Switch to `stock-picker` by voice, then end the call.
2. Remove `stock-picker` from the person's `profiles` in `duplex.yaml` (or from `profiles:`), then restart.
3. Start a call and say anything.

Expected: the person hears "You're back on general." once, when first heard, and not again later in
the call, nor after another restart and call (the service rewrote `active_profiles.json` without the
dropped profile). The next request runs on `general`. Restore the config afterwards.

### 24. "Cancel that?" and late answers

1. Start a slow task ("research three dividend stocks in depth"), then say "cancel the weather one".
2. Answer "yes".
3. Right after the cancel confirmation, say "yes" again.
4. Start another slow task, say "cancel the weather one", and answer "no".
5. Say "cancel the weather one" once more and say nothing for 30 seconds, then say "yes".
6. On Discord with a second person B: ask "what did NVDA do today?" so the switch question comes,
   and have B say "yes" before you answer; then answer "no" yourself.
7. Ask something whose answer ends with an offer ("…Want me to set a price alert?"), right after a
   question of yours was answered, and say "yes" to the offer.

Expected: steps 1, 4, and 5 ask "The only one running is "research three dividend stocks…". Cancel
that?" and stop nothing yet. In step 2 the task is cancelled (`Cancelled "…"`); on `openai-realtime`
and `xai-realtime` the yes arrives as `confirm_switch` or as `cancel_task` with the task's topic as
`about`, on `gpt-live` as a handed-off "yes". Step 3 answers "There's no question waiting for your
answer right now." and starts no Hermes run (the log shows `<you>'s 'yes' is taken as an answer to
<you>'s question; not run`). In step 4 the task keeps running. In step 5 the question ends after 30
s, the task keeps running, and the late "yes" gets the same "no question waiting" answer. In step 6
B's yes gets "That question was for <you>." and switches nothing; your "no" then runs the request
on your active profile. In step 7 the "yes" is handed off as a request with the offer as context.

### 25. Hermes context: SOUL.md, USER.md, the briefing, and open tasks

Set `hermes.voice_context_files: [soul, user, briefing]` and `hermes_owner: true` on yourself, give
`stock-picker` a `SOUL.md` and a `memories/USER.md` with a fact the model could not guess (your
favourite cheese), and start the service. Wait for the log's "Hermes wrote the SOUL.md of profile
stock-picker for voice" lines, then run `hermes-duplex voice-context show stock-picker` and read it.

1. On the web page, on `stock-picker`, ask "what can you do for me?", then "what's my favourite
   cheese?", then "what am I busy with these days?".
2. On Discord, alone in the channel with the bot, ask the same three questions.
3. Ask "tell me everything you know about me" and, while the answer plays, have person B join the
   channel (not speak); then ask the cheese question again; then have B leave and, after a quiet
   moment, ask once more. On `openai-realtime` or `xai-realtime`, repeat it with B speaking as they
   join, right after you start a task (so an instruction update is under way).
4. Start a slow task ("research three dividend stocks in depth") and, while it runs, ask "what's
   still running?" and edit `stock-picker`'s `USER.md`; on `openai-realtime` or `xai-realtime` check
   that the log shows an instruction update when it starts and ends, and on `gpt-live` that no
   restart follows; and that no "Hermes wrote" line appears until you have left the call.
5. Edit `$HERMES_HOME/duplex/voice_context/stock-picker.soul.md` by hand, reconnect, and ask what
   the agent can do; then run `hermes-duplex voice-context refresh stock-picker` (it says it kept
   your edit) and `--force`.

Expected: in steps 1 and 2 the answers follow the rewritten SOUL.md, the cheese is known, and the
briefing's projects come up. In step 3 the answer stops as B joins (the log shows "someone joined: flushed playback"), and with B
in the channel the cheese is not known (the log shows
"voice instructions now speak for profile stock-picker, to <you>" without "alone" and a restart at
once on every provider, and no more of the earlier answer is heard after it), and once B has left it is known again (at once on `openai-realtime` and `xai-realtime`, once the call is quiet on `gpt-live`). In the repeat the restart still comes at once, and nothing is said before it. In step 4 the task is named by
topic and no task number is said, and the edit reaches the prompt only at the next connect. In step 5 the edit is what the model goes by until `--force`
replaces it. Neither the log nor `doctor` ever prints a file's text.
