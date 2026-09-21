# Design

Why the pieces sit where they do. In short: the shell function holds only what never changes, because it is a copy in every open terminal, and everything else is read fresh.

## Load-bearing settings

- **No `model` in `settings.json`.** A settings-file model pin outranks `ANTHROPIC_MODEL`. Both `~/.claude/settings.json` and the shelestni profile pin `"model": "opus[1m]"`, which is why muse needs its own profile rather than sharing one.
- **`unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN`** in the function. Either would outrank `apiKeyHelper`; Claude Code warns and auth fails with `401`.
- **`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`.** `muse-spark-1.3` isn't in this Claude Code version's model catalog, so it assumes a 200k window and auto-compacts at a fifth of the real one.
- **`CLAUDE_CODE_EFFORT_LEVEL=max`.** The only persistent route to `max` reasoning - `effortLevel`/`modelSettings` in settings.json accept `low`..`xhigh` only. The endpoint takes the tier in `output_config.effort` and accepts `max` there (measured 2026-09-19, along with `low`/`medium`/`high`/`xhigh`; `minimal` and a top-level `effort` are both rejected). The census records `output_config` so the question of whether this setting reaches the wire is answered by looking rather than by arguing; `docs/api-subset.md` has the full measurement.

## Why the logic is not in the function

A shell function is a **copy**, taken when the shell read `~/.zshrc`. Change the function and every terminal already open keeps running the old body, indefinitely. That is not theoretical: the day the proxy landed, a session launched from a shell opened an hour earlier went straight at `api.meta.ai`, lost web search and every subagent, and reported it as a *"model classifier hiccup"* - because the auto-mode classifier is the first thing the endpoint rejects. Nothing about the symptom pointed at the shell.

So the function now holds nothing at all: it forwards to `bin/claude-muse`, an executable that sources `model-env.sh` and `preflight.sh` and execs `claude`. Those files are read fresh on every launch, so a change to the checks, the port, or the base URL reaches every shell immediately, open or not. The launcher being a file is also what lets md and Switchboard start a Muse session, because `env` and a cmux tab can run a program but never a function.

What survives a stale function is the status line, which Claude Code evaluates live from the session's own environment. `direct — relaunch` there is the tell, and the fix is always the same: quit and relaunch from a new shell. `exec zsh` does not rescue a session already running, because it inherited its environment at launch.

**On every launch, free and instant:** base URL set, proxy confirmed up via `GET /__health`, and the running proxy's source hash compared against the engine on disk - an edited proxy does nothing until it restarts, which is the difference between a fix landing and a fix appearing to land.

**Only when that version changes:** two real calls, one per failure class - a short `max_tokens` request that must come back with text, and the exact web search shape Claude Code sends, `max_uses` and all, which must come back `200`. Verified once, recorded, and skipped until the code moves again. A normal launch costs nothing and takes about a tenth of a second; the first launch after an edit takes around half a minute and proves the edit works. [D4 of the architecture gallery](architecture.md#d4) draws this lane.

`CLAUDE_MUSE_SKIP_PROBE=1` skips the paid probe. `CLAUDE_MUSE_DIRECT=1` bypasses the proxy entirely, which is a debugging tool rather than a fallback - web search and subagents do not work without it.

## Auto mode

Auto mode runs a classifier over actions like shell commands before they run. Since v2.1.278 Claude Code prefers to have the **server** do that inside the session's own requests, asking with the `safeguards` request field and reading `safeguard_results` back.

`api.meta.ai` rejects `safeguards` outright - `400 unknown parameter` - so the proxy strips it. The server's checks therefore can never reach a claude-muse session. Left alone, Claude Code finds that out the expensive way: it **holds the first checked action**, prints the "this session isn't eligible" notice, waits for you to press Enter, and only then falls back to running the classifier itself. That held action is what a blocked-looking `bash` usually is.

`preflight.sh` sets `CLAUDE_CODE_AUTO_MODE_SERVER=0`, which tells Claude Code not to ask, so there is no doomed round trip and nothing is held. `/status` shows this as **Auto mode server: Disabled**, which is correct and expected here.

**What that does not change is who judges.** The fallback classifier runs on `muse-spark-1.3`, because it is the only model this endpoint serves. Verdicts are therefore Spark's, and they are less consistent than Claude Code's usual classifier - the occasional refusal of an ordinary command is that, not a bug in the proxy.

**Mostly, though, it does not refuse. It runs out of time.** Of 44 denials measured across two days of transcripts, all but one read `muse-spark-1.3 is temporarily unavailable (timed out)`, not a verdict. The reason is in the shape: the classifier sends no reasoning directive, this endpoint has no setting for "do not reason", and its default tier is the slowest one measured. Classifier round trips ran a median of 2.7s and a p90 of 60.4s, and the classifier is a little over half of all requests. Claude Code gives up well before the p90 and denies the tool.

That is why `permissions.allow` carries `Write`, `Edit`, `MultiEdit` and `NotebookEdit` as well as the `Bash(...)` entries. An allowed tool never reaches the classifier, so it cannot be denied by a timeout. Without them a long task dies the same way every time: the model loses the ability to write files and degrades into handing you heredocs to paste. Three honest ways to deal with a denial:

- **Name what you trust.** `permissions.allow` takes tool names and entries like `Bash(git status:*)`. Specific rather than blanket, and the mechanism Claude Code provides for exactly this.
- **Leave auto mode** for that session with `Shift+Tab`, which drops to a mode with no classifier in the path at all.
- **Read `proxy.log`.** A `no-reasoning` line with a high `ms=` is the classifier being slow; `/__health` counts how much of your traffic that is.

What the proxy will not do is answer the classifier on the endpoint's behalf, or quietly make its calls cheaper by asserting a reasoning tier the client never asked for. It repairs request *shapes* the endpoint cannot parse; the rest would apply silently to every future session, and the mechanisms above are supported and visible.

## Continuing a long task

A model that ends its turn with "say the word and I'll start" costs a round trip and reads as laziness. Measured across the same transcripts, 63 turns ended in a way that needed the user to type something, and 25 of those were asking for permission to carry on with work already approved. A good share of them were downstream of the denials above: a model that cannot write files has nothing left to do but ask.

Two things address the remainder, both in the profile rather than the proxy. `profile/CLAUDE.md` is loaded into every muse session and says plainly that an approved plan is the go-ahead and that a decision only the user can make belongs in `AskUserQuestion`, which keeps the turn alive, rather than in prose, which ends it. `profile/hooks/continue-gate` is a `Stop` hook that enforces it: when a turn ends asking to continue, it blocks once with that reminder, and Claude Code feeds the text back to the model.

The hook is deliberately timid. It reads `background_tasks` and `session_crons` from the hook input and allows whenever either is non-empty, because a turn that ends waiting on a subagent is correct and a notification will wake it. It matches a narrow list of phrases rather than guessing at intent. It gives up after two blocks in an episode, because a gate that cannot give up is a hang. And it fails open on every absence - no `jq`, no input, unparseable input.

Doing this at the proxy was considered and rejected. Detecting "the model asked a question" and injecting a continuation would manufacture conversation, and it would apply to every future session with nothing in the transcript to show for it.

## The context readout

`Ctx: 14% 142k/1M`. The numerator is trustworthy: the endpoint returns a real usage block, and `input + cache_creation + cache_read` is the true prompt size even though `cache_creation_input_tokens` always reports 0 - written tokens land in `input_tokens` instead. Measured both ways, the same prompt reports `in=3021` cold and `in=92 + read=2929` warm.

The denominator is the soft part. `muse-spark-1.3` is not in the model catalog and `/v1/models` returns `401` here, so the window is not a fact from the provider - it is whatever `CLAUDE_CODE_MAX_CONTEXT_TOKENS` asserted. Lose that variable and a 1M window is divided as 200k, making every reading five times too high with nothing on screen to say so. That is why the denominator is printed rather than hidden behind a bare percentage, and why the segment turns red when it drops below 500k. `/1M` is correct; `/200k` means the env var went missing. Muse's own catalog puts the real limit at 1,007,997, so 1,000,000 is 0.8% conservative.

`Ctx: ?` in yellow means no usage has been reported yet. The stock status line falls back to cumulative session counters there, which only grow, double count every turn and eventually exceed 100%; a number that confident and that wrong is worse than none.

The reading always lags one turn, and that is inherent rather than fixable. The status line reads the last real `usage` block, so a large tool result stays invisible until the next response carries a count that includes it.

An earlier version of this file blamed the lag on `count_tokens` returning `402`. That was wrong: `count_tokens` was never in the status line's path. That 402 is real and it breaks something else - see **Counting tokens** in [`api-subset.md`](api-subset.md) - but fixing it does not move this number, and the lag is kept deliberately rather than papered over with an estimate.

## The census

`shapes.json` records every distinct request shape Claude Code sends, once each, and `proxy.log` gets one `shape:` line when a new one appears. A shape is the sorted body keys, the tool types, whether any block carries `cache_control` or an image, the `max_tokens` value, the `thinking` and `effort` shapes, and the `anthropic-beta` list.

Headers are captured from an allowlist - `anthropic-beta`, `anthropic-version`, `accept` - so the credential is excluded by construction rather than by remembering to filter it.

It exists because most questions about this setup are settled by looking at the wire rather than by reasoning about the binary: whether `CLAUDE_CODE_EFFORT_LEVEL=max` reaches the request or is inert, which beta tokens leave the client, whether `cache_control` is ever sent, what `max_tokens` the main loop carries. One session answers all of them, for free. It records `output_config` field by field for the first of those, and it is what showed the auto-mode classifier sends no reasoning directive at all - which is not what this file used to say.

## Repair rules

`rewrite-rules.yaml` is the repair policy as data: which shapes the proxy rewrites, in which order, for which models. Editing it changes behavior without touching `engine/`, which matters because the engine hash is what preflight watches - a policy tweak costs no re-probe, while a code edit costs two paid calls. [D2](architecture.md#d2) draws the four layers in order.

The shape of it - declarative match-and-mutate rules with model wildcards - is borrowed from CLIProxyAPI's `payload` rules (router-for-me/CLIProxyAPI, MIT), cut down to six ops. Per-model reasoning values (the `max_tokens` floor, the thinking budget) live in the same file under `models:`, read by one fixed pipeline rather than by rules, because those normalizations only make sense together. What the proxy does when the file is missing, malformed, or pyyaml is absent is always the same: fall back to the baked-in copy and say so in `proxy.log`. A typo in policy degrades to no repair, never to a failed request.
