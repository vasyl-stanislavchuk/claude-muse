# What api.meta.ai actually implements

Every row here was measured, not inferred. `bin/probe.sh` is how they were measured and how they are re-checked when something changes.

## The API subset

Measured 2026-09-19. Each row is a request Claude Code makes as a matter of course, and the endpoint's answer to it.

| What Claude Code sends | Endpoint | What it broke |
| --- | --- | --- |
| `web_search_20250305` with `max_uses: 8` (hardcoded in the binary) | `400 web_search field 'max_uses' is not supported` | WebSearch, every call |
| `allowed_domains` / `blocked_domains` on that tool | `400 ... 'allowed_domains' is not supported` | WebSearch with domain filters |
| `tool_choice: {type:"tool", name:"web_search"}` | `400 named 'tool_choice' is not supported` | WebSearch, even with `max_uses` gone |
| `thinking: {type:"disabled"}` on mechanical side queries | `400 reasoning_effort 'none' is not supported` | the auto-mode permission classifier |
| `stop_sequences`, `safeguards`, `top_k` | `400 ... is not supported` / `unknown parameter` | the classifier again |
| a classifier-sized `max_tokens` | `200` with `content: []` | same: Spark spends the whole budget thinking and emits nothing |
| `POST /v1/messages/count_tokens` | `402 billing_error` | the `/context` cost panel, silently — see **Counting tokens** |
| `GET /v1/models` | `401` | model discovery, so the context window has to be asserted rather than read |

The last two rows are why **subagents never launched**. Claude Code's auto mode asks a classifier whether a tool call is safe before running it; read-only tools are exempt, which is why `Read` and `Grep` always worked while `Agent` produced `muse-spark-1.3 is temporarily unavailable, so auto mode cannot determine the safety of Agent` every single time. The classifier is a short mechanical call, so it hit those rows and came back 400 or empty, and Claude Code reads both as "model down".

`proxy.py` repairs all of it: it strips the unsupported `web_search` fields, rewrites a **named** `tool_choice` to `auto`, drops `thinking: {type:"disabled"}`, raises `max_tokens` to a floor of 4096, and clamps `budget_tokens` below it.

Only the named form is rewritten. `any` and `none` pass through, because the only rejection ever measured is the named one, and forcing `none` to `auto` would turn "do not call tools" into "call tools if you like" — the proxy granting a permission rather than repairing a shape. If `any` or `none` does turn out to be rejected, the retry path drops the field instead of replacing it: absence asserts nothing, where `auto` asserts something.

**It learns the rest.** A `400` that names a field in backticks is the endpoint describing its own subset, so the proxy records the name in `learned.json`, strips it and retries — before anything reaches Claude Code, so the only visible cost is one slow request the first time a gap appears. `stop_sequences`, `safeguards` and `top_k` were all found this way rather than by hand.

Two things worth knowing:

- **Web search really does run at api.meta.ai**, and results come back current and correct. What never comes back is a `web_search_tool_result` block, so out of the box Claude Code has no sources to render and — worse — no URL to hand to WebFetch, which breaks the search-then-read loop. The URLs are in the response all the same: every page the model opens arrives as a `server_tool_use` whose input is `{"type": "open_page", "url": ...}`. The proxy harvests those and appends the block Claude Code already knows how to parse, which restores both the source list and the follow-up fetch. Titles are derived from the URL, because the endpoint sends none and inventing one would be a fabricated citation.

  The limit is real: sources appear only for pages the model actually opened. A search it answers from result snippets alone still cites nothing, because nothing in that response identifies a page. Asking it to read a few pages is what produces citations, and that costs more — a search that opened twelve pages ran 25k input tokens against 2k for one that opened none.
- **The `max_tokens` floor changes behaviour.** A caller that asked for 50 output tokens can now get up to 4096. That is the right trade — an over-long generated title is recoverable, an empty response is not — but it is a real effect, not a no-op.

## Counting tokens

Claude Code asks `POST /v1/messages/count_tokens` to build the **Projected token cost** panel in `/context` — the one that splits always-on from on-invoke cost for every skill and agent. This key returns `402 billing_error` for it, every time. The log has shown 45 in a session, twelve inside one second.

Nothing appears to break, and that is the problem. Claude Code's fallback when the call fails is `Math.ceil(JSON.stringify({system, messages, tools}).length / 4)` — a flat four-characters-per-token guess, applied silently. So the panel is not empty, it is **confidently approximate**, with no indication that the number never came from a tokenizer. Claude Code does print *"Token counts are estimates and may differ from actual usage"* next to it, which is true but does not distinguish a measured estimate from a fallback one.

Replacing that guess with a ratio calibrated against this endpoint's own `usage` blocks is the open work. It is worth doing precisely because the bar is so low: anything measured beats `chars/4`.

Two things this is *not*. It is not the status line's one-turn lag, which is a different code path — see **The context readout**. And it is not a case for feeding an estimate into the status line, whose numerator is real and should stay that way.

## When it breaks

| Symptom | Cause |
| --- | --- |
| `402 billing_error` | a pay-as-you-go key is in play instead of the subscription key |
| `401` + "Both ANTHROPIC_AUTH_TOKEN and apiKeyHelper set" | stale shell holding an old function definition — `exec zsh` |
| `401`, helper prints nothing | keychain item renamed or login expired — re-run `muse login`, then check the `service`/`account` hardcoded in `api-key.sh` |
| context compacts far too early | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` lost from the function |
| every request fails, nothing in `proxy.log` | the proxy is down — preflight should have started it; check `proxy.err` |
| anything at all in `proxy.err` | a genuine crash. The two files stopped being duplicates, so this one is signal now |
| `/context` numbers look implausible | the count is Claude Code's `chars/4` fallback, not a measurement — see **Counting tokens** |
| a field is being stripped that the endpoint now supports | `learned.json` only grows. Remove the entry by hand and restart the proxy |
| `bash` looks blocked in auto mode, once, early | the held "auto mode isn't eligible" notice. `CLAUDE_CODE_AUTO_MODE_SERVER=0` in preflight prevents it; if you see it, a shell predating that change is in play |
| auto mode refuses an ordinary command | a genuine verdict from `muse-spark-1.3`, which is the fallback classifier here. Add a `Bash(...)` allow rule for it, or leave auto mode with `Shift+Tab` |
| status line says `direct — relaunch` | the launching shell predates the current function. Quit the session and start a new one from a fresh shell; `exec zsh` will not fix a running session |
| status line says `proxy down` | the session is pointed at the proxy but nothing answers. `launchctl kickstart -k gui/$(id -u)/co.medallion.claude-muse-proxy` |
| a proxy.py edit seems to do nothing | the proxy holds its source in memory. Preflight restarts it on the next launch; mid-session, kickstart it |
| `400 ... is not supported` reaches the session | a shape the proxy could not repair by name. Read `proxy.log` for the exact field and add an explicit rule |
| a short reply comes back empty | the `max_tokens` floor is not being applied — the session is talking to `api.meta.ai` directly rather than through the proxy |
| subagents blocked, `auto mode cannot determine the safety of` | the classifier is failing upstream. `proxy.log` names the field |

A useful first probe, since it separates endpoint problems from credential ones — `401` means the credential isn't recognized, `402` means it is but isn't billable:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.meta.ai/v1/messages \
  -H "x-api-key: $(~/.config/claude-muse/api-key.sh)" \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"muse-spark-1.3","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

