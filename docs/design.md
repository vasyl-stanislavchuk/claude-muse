# Design

Why the pieces sit where they do. The short version: the shell function holds only what never changes, because it is a copy in every open terminal, and everything else is read fresh.

## Load-bearing settings

- **No `model` in `settings.json`.** A settings-file model pin outranks `ANTHROPIC_MODEL`. Both `~/.claude/settings.json` and the shelestni profile pin `"model": "opus[1m]"`, which is why muse needs its own profile rather than sharing one.
- **`unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN`** in the function. Either would outrank `apiKeyHelper`; Claude Code warns and auth fails with `401`.
- **`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`.** `muse-spark-1.3` isn't in this Claude Code version's model catalog, so it assumes a 200k window and auto-compacts at a fifth of the real one.
- **`CLAUDE_CODE_EFFORT_LEVEL=max`.** The only persistent route to `max` reasoning - `effortLevel`/`modelSettings` in settings.json accept `low`..`xhigh` only. The Meta Anthropic-compatible endpoint honors the `thinking` block (verified 2026-09-17: `redacted_thinking` + output) and maps it onto Spark effort tiers; an unsupported level degrades to the highest supported one, never errors.

## Why the logic is not in the function

A shell function is a **copy**, taken when the shell read `~/.zshrc`. Change the function and every terminal already open keeps running the old body, indefinitely. That is not theoretical: the day the proxy landed, a session launched from a shell opened an hour earlier went straight at `api.meta.ai`, lost web search and every subagent, and reported it as a *"model classifier hiccup"* — because the auto-mode classifier is the first thing the endpoint rejects. Nothing about the symptom pointed at the shell.

So the function holds only what rarely changes, and `preflight.sh` holds everything else. That file is read fresh on every launch, so a change to the checks, the port, or the base URL reaches every shell immediately, open or not.

What survives a stale function is the status line, which Claude Code evaluates live from the session's own environment. `direct — relaunch` there is the tell, and the fix is always the same: quit and relaunch from a new shell. `exec zsh` does not rescue a session already running, because it inherited its environment at launch.

**On every launch, free and instant:** base URL set, proxy confirmed up via `GET /__health`, and the running proxy's source hash compared against `proxy.py` on disk — an edited proxy does nothing until it restarts, which is the difference between a fix landing and a fix appearing to land.

**Only when that version changes:** two real calls, one per failure class — a short `max_tokens` request that must come back with text, and the exact web search shape Claude Code sends, `max_uses` and all, which must come back `200`. Verified once, recorded, and skipped until the code moves again. A normal launch costs nothing and takes about a tenth of a second; the first launch after an edit takes around half a minute and proves the edit works.

`CLAUDE_MUSE_SKIP_PROBE=1` skips the paid probe. `CLAUDE_MUSE_DIRECT=1` bypasses the proxy entirely, which is a debugging tool rather than a fallback — web search and subagents do not work without it.

## Auto mode

Auto mode runs a classifier over actions like shell commands before they run. Since v2.1.278 Claude Code prefers to have the **server** do that inside the session's own requests, asking with the `safeguards` request field and reading `safeguard_results` back.

`api.meta.ai` rejects `safeguards` outright — `400 unknown parameter` — so the proxy strips it. The server's checks therefore can never reach a claude-muse session. Left alone, Claude Code finds that out the expensive way: it **holds the first checked action**, prints the "this session isn't eligible" notice, waits for you to press Enter, and only then falls back to running the classifier itself. That held action is what a blocked-looking `bash` usually is.

`preflight.sh` sets `CLAUDE_CODE_AUTO_MODE_SERVER=0`, which tells Claude Code not to ask, so there is no doomed round trip and nothing is held. `/status` shows this as **Auto mode server: Disabled**, which is correct and expected here.

**What that does not change is who judges.** The fallback classifier runs on `muse-spark-1.3`, because it is the only model this endpoint serves. Verdicts are therefore Spark's, and they are less consistent than Claude Code's usual classifier — the occasional refusal of an ordinary command is that, not a bug in the proxy. Two honest ways to deal with it:

- **Name the commands you trust.** `permissions.allow` in the profile's `settings.json` takes entries like `Bash(git status:*)`, `Bash(make:*)`, `Bash(uv run pytest:*)`. This is the mechanism Claude Code provides for exactly this, and it is specific rather than blanket.
- **Leave auto mode** for that session with `Shift+Tab`, which drops to a mode with no classifier in the path at all.

What the proxy will not do is answer the classifier on the endpoint's behalf. It repairs request *shapes* the endpoint cannot parse; manufacturing a safety verdict is a different thing, it would apply silently to every future session, and the two mechanisms above are both supported and visible.

## The context readout

`Ctx: 14% 142k/1M`. The numerator is trustworthy: the endpoint returns a real usage block, and `input + cache_creation + cache_read` is the true prompt size even though `cache_creation_input_tokens` always reports 0 — written tokens land in `input_tokens` instead. Measured both ways, the same prompt reports `in=3021` cold and `in=92 + read=2929` warm.

The denominator is the soft part. `muse-spark-1.3` is not in the model catalog and `/v1/models` returns `401` here, so the window is not a fact from the provider — it is whatever `CLAUDE_CODE_MAX_CONTEXT_TOKENS` asserted. Lose that variable and a 1M window is divided as 200k, making every reading five times too high with nothing on screen to say so. That is why the denominator is printed rather than hidden behind a bare percentage, and why the segment turns red when it drops below 500k. `/1M` is correct; `/200k` means the env var went missing. Muse's own catalog puts the real limit at 1,007,997, so 1,000,000 is 0.8% conservative.

`Ctx: ?` in yellow means no usage has been reported yet. The stock status line falls back to cumulative session counters there, which only grow, double count every turn and eventually exceed 100%; a number that confident and that wrong is worse than none.

The reading always lags one turn, and that is inherent rather than fixable. The status line reads the last real `usage` block, so a large tool result stays invisible until the next response carries a count that includes it.

An earlier version of this file blamed the lag on `count_tokens` returning `402`. That was wrong: `count_tokens` was never in the status line's path. The 402 is real and it breaks something else — see **Counting tokens locally** — but fixing it does not move this number, and the lag is kept deliberately rather than papered over with an estimate.

## The census

`shapes.json` records every distinct request shape Claude Code sends, once each, and `proxy.log` gets one `shape:` line when a new one appears. A shape is the sorted body keys, the tool types, whether any block carries `cache_control` or an image, the `max_tokens` value, the `thinking` and `effort` shapes, and the `anthropic-beta` list.

Headers are captured from an allowlist — `anthropic-beta`, `anthropic-version`, `accept` — so the credential is excluded by construction rather than by remembering to filter it.

It exists because most questions about this setup are settled by looking at the wire rather than by reasoning about the binary. Does `CLAUDE_CODE_EFFORT_LEVEL=max` actually put an `effort` key on the request, or is it inert? Which beta tokens leave the client? Is `cache_control` ever sent? What `max_tokens` does the main loop carry? One session answers all of them, for free.

