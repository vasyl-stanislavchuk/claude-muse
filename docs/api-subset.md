# What api.meta.ai implements

Every row here was measured, not inferred. `bin/probe.sh` is how they were measured and how they are re-checked when something changes.

## The API subset

Measured 2026-09-19. Each row is a request Claude Code makes as a matter of course, and the endpoint's answer to it.

| What Claude Code sends | Endpoint | What it broke |
| --- | --- | --- |
| `web_search_20250305` with `max_uses: 8` (hardcoded in the binary) | `400 web_search field 'max_uses' is not supported` | WebSearch, every call |
| `allowed_domains` / `blocked_domains` on that tool | `400 ... 'allowed_domains' is not supported` | WebSearch with domain filters |
| `tool_choice: {type:"tool", name:"web_search"}` | `400 named 'tool_choice' is not supported` | WebSearch, even with `max_uses` gone |
| `thinking: {type:"disabled"}` on mechanical side queries | `400 reasoning_effort 'none' is not supported for model 'muse-spark-1.3'. Supported values: [minimal, low, medium, high, xhigh, max]` | nothing today, but see **Reasoning** |
| `stop_sequences`, `safeguards`, `top_k` | `400 ... is not supported` / `unknown parameter` | the classifier again |
| a classifier-sized `max_tokens` | `200` with `content: []` | the auto-mode permission classifier: Spark spends the whole budget thinking and emits nothing |
| `effort` or `reasoning_effort` at the top level | `400 unknown parameter` | nothing; the tier travels in `output_config` instead |
| `POST /v1/messages/count_tokens` | `402 billing_error` | the `/context` cost panel, silently — now answered locally, see **Counting tokens** |
| `GET /v1/models` | `401` | model discovery, so the context window has to be asserted rather than read |

Those rows are why **subagents never launched**. Claude Code's auto mode asks a classifier whether a tool call is safe before running it; read-only tools are exempt, which is why `Read` and `Grep` always worked while `Agent` produced `muse-spark-1.3 is temporarily unavailable, so auto mode cannot determine the safety of Agent` every single time. The classifier is a short mechanical call, so it hit those rows and came back 400 or empty, and Claude Code reads both as "model down".

**Correction.** Earlier versions of this file said the classifier sends `thinking: {type:"disabled"}`. It does not. The census records its shape as `keys=max_tokens,messages,metadata,model,stop_sequences,system` with `max_tokens=2112` and no `thinking` key at all; the only `thinking=disabled` row in `shapes.json` is `probe.sh` sending one. The classifier carries **no reasoning directive**, which matters because this endpoint has no way to switch reasoning off - see below.

The engine repairs all of it: it strips the unsupported `web_search` fields, rewrites a **named** `tool_choice` to `auto`, translates `thinking: {type:"disabled"}` into the cheapest tier the endpoint has, raises `max_tokens` to a floor of 4096, and clamps `budget_tokens` below it. [D2 of the architecture gallery](architecture.md#d2) draws these repairs as four ordered layers.

Only the named form is rewritten. `any` and `none` pass through, because the only rejection ever measured is the named one, and forcing `none` to `auto` would turn "do not call tools" into "call tools if you like" - the proxy granting a permission rather than repairing a shape. If `any` or `none` does turn out to be rejected, the retry path drops the field instead of replacing it: absence asserts nothing, where `auto` asserts something.

**It learns the rest.** A `400` that names a field in backticks is the endpoint describing its own subset, so the proxy records the name, first sighting and hit count in `learned.json`, strips it and retries - before anything reaches Claude Code, so the only visible cost is one slow request the first time a gap appears. `stop_sequences`, `safeguards` and `top_k` were all found this way rather than by hand. [D3](architecture.md#d3) draws the learn-and-retry loop it feeds. Turning a find like this into repo policy is the [promotion flow](promotion.md).

Two things worth knowing:

- **Web search really does run at api.meta.ai**, and results come back current and correct. What never comes back is a `web_search_tool_result` block, so out of the box Claude Code has no sources to render and - worse - no URL to hand to WebFetch, which breaks the search-then-read loop. The URLs are in the response all the same: every page the model opens arrives as a `server_tool_use` whose input is `{"type": "open_page", "url": ...}`. The proxy harvests those and appends the block Claude Code already knows how to parse, which restores both the source list and the follow-up fetch. Titles are derived from the URL, because the endpoint sends none and inventing one would be a fabricated citation.

  The limit is real: sources appear only for pages the model opened. A search it answers from result snippets alone still cites nothing, because nothing in that response identifies a page. Asking it to read a few pages is what produces citations, and that costs more - a search that opened twelve pages ran 25k input tokens against 2k for one that opened none.
- **The `max_tokens` floor changes behaviour.** A caller that asked for 50 output tokens can now get up to 4096. That is the right trade - an over-long generated title is recoverable, an empty response is not - but it is a real effect, not a no-op.

## Reasoning

Measured 2026-09-19 with `bin/probe.sh` and the `usage.output_tokens_details.thinking_tokens` the endpoint reports back.

**The tier travels in `output_config.effort`, and nowhere else.** A top-level `effort` or `reasoning_effort` is `400 unknown parameter`; so is `thinking.effort`. `output_config.effort` takes `low`, `medium`, `high`, `xhigh` and `max`, and rejects anything else - including `minimal`, which the `thinking: {type:"disabled"}` error lists as a valid `reasoning_effort`. That gap is the endpoint describing its internal enum rather than its public one.

**`thinking.budget_tokens` does not choose a tier.** 1024, 8000 and 32000 against the same prompt returned 706, 663 and 503 thinking tokens - noise, not a trend. The budget is still validated (at least 1024, and strictly less than `max_tokens`), so the clamp stays, but it buys no reasoning. Neither does `thinking: {type:"adaptive"}`, which is what Claude Code's main loop sends.

**There is no way to switch reasoning off.** `disabled` maps to `reasoning_effort 'none'`, which is rejected, and `minimal` is rejected too. The floor is `low`. A request carrying no reasoning directive at all - which is every auto-mode classifier call - gets whatever the endpoint defaults to, and that default measured as the *most* expensive shape tried: 14.7s and 696 thinking tokens against 10.3s and 431 at `low`.

So the repair for `thinking: {type:"disabled"}` is a translation rather than a deletion. Deleting it is the larger intervention: it turns "do not reason" into "reason however you like". Measured through the proxy on one classifier-shaped request, translating it to `output_config: {effort: low}` took the call from **7538ms and 352 thinking tokens to 3806ms and 200**.

**What the proxy does not do** is add a tier to a request that named none. That population is 9 of every 11 requests in a probe run and the majority of real traffic, and speeding it up would mean asserting a reasoning level Claude Code never asked for - the same category as answering the safety classifier on the endpoint's behalf. The `no-reasoning` token in `proxy.log` and the `no_reasoning_requests` counter in `/__health` exist so the size of that population is a number rather than a guess. The supported fix for the classifier timing out is `permissions.allow`.

One caveat worth keeping: a cheaper tier can change what the classifier concludes. The proxy is not choosing the verdict, but it is changing the conditions under which Spark reaches one.

## Counting tokens

Claude Code asks `POST /v1/messages/count_tokens` to build the **Projected token cost** panel in `/context` - the one that splits always-on from on-invoke cost for every skill and agent. This key returns `402 billing_error` for it, every time. One session logged 45 of them, twelve inside a single second.

Nothing appears to break, and that is the problem. Claude Code's fallback when the call fails is `Math.ceil(JSON.stringify({system, messages, tools}).length / 4)` - a flat four-characters-per-token guess, applied silently. So the panel is not empty, it is **confidently approximate**, with no indication that the number never came from a tokenizer. Claude Code does print *"Token counts are estimates and may differ from actual usage"* next to it, which is true but does not distinguish a measured estimate from a fallback one.

The proxy now serves that call itself, replacing the guess with a ratio calibrated against this endpoint's own `usage` blocks: every tool-less `/v1/messages` response teaches it characters-per-token, pooled over recent traffic and checkpointed to `calibration.json`. Responses to tool-using requests don't teach it - their input count folds in whatever the model went and read, which no estimate made beforehand could know. Anything measured beats `chars/4`, and the bar was that low. It stays an estimate - the log line says `estimated` with the ratio and sample count behind the number, and the status line never eats it, because that numerator is real.

Two things this is *not*. It is not the status line's one-turn lag, which is a different code path - see **The context readout**. And it is not a case for feeding an estimate into the status line, whose numerator is real and should stay that way.

## When it breaks

| Symptom | Cause |
| --- | --- |
| `402 billing_error` | a pay-as-you-go key is in play instead of the subscription key. `count_tokens` is answered locally, so a `402` from it should no longer appear at all |
| `429` / `503` from upstream | the proxy waits and resends up to 3 times, then cools down for 60s and answers `503` fast until it clears |
| `401` + "Both ANTHROPIC_AUTH_TOKEN and apiKeyHelper set" | stale shell holding an old function definition — `exec zsh` |
| `401`, helper prints nothing | keychain item renamed or login expired — re-run `muse login`, then check `CLAUDE_MUSE_KEYCHAIN_SERVICE` / `CLAUDE_MUSE_KEYCHAIN_ACCOUNT` |
| context compacts far too early | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` lost from the function |
| every request fails, nothing in `proxy.log` | the proxy is down — preflight should have started it; check `proxy.err` |
| anything at all in `proxy.err` | a genuine crash. The two files stopped being duplicates, so this one is signal now |
| `upstream-cut` in `proxy.log` | the upstream stopped sending after headers were committed. What arrived is served; there is nothing to retry into at that point |
| `/context` numbers look implausible | the count is the proxy's calibrated estimate, not a tokenizer fact - `proxy.log` shows the ratio and sample count behind it |
| a field is being stripped that the endpoint now supports | `learned.json` holds 100 entries and only grows to there. Remove the entry by hand as [promotion.md](promotion.md#pruning-an-entry) describes; the proxy picks it up on the next request |
| `bash` looks blocked in auto mode, once, early | the held "auto mode isn't eligible" notice. `CLAUDE_CODE_AUTO_MODE_SERVER=0` in preflight prevents it; if you see it, a shell predating that change is in play |
| auto mode refuses an ordinary command | a genuine verdict from `muse-spark-1.3`, which is the fallback classifier here. Add a `Bash(...)` allow rule for it, or leave auto mode with `Shift+Tab` |
| status line says `direct — relaunch` | the launching shell predates the current function. Quit the session and start a new one from a fresh shell; `exec zsh` will not fix a running session |
| status line says `proxy down` | the session is pointed at the proxy but nothing answers. `launchctl kickstart -k gui/$(id -u)/co.medallion.claude-muse-proxy` |
| an engine edit seems to do nothing | the proxy holds its source in memory. Preflight restarts it on the next launch; mid-session, kickstart it |
| `400 ... is not supported` reaches the session | a shape the proxy could not repair by name. Read `proxy.log` for the exact field and add an explicit rule |
| a short reply comes back empty | the `max_tokens` floor is not being applied — the session is talking to `api.meta.ai` directly rather than through the proxy |
| subagents blocked, `auto mode cannot determine the safety of` | the classifier timed out rather than refused. It carries no reasoning directive, so it pays the endpoint's default tier; `grep no-reasoning proxy.log` shows how slow. The fix is a `permissions.allow` entry, not a proxy change |
| a tool is denied that should not be | check `permissions.allow` first. `Write`, `Edit`, `MultiEdit` and `NotebookEdit` are on it deliberately: without them every file change waits on a classifier verdict from a model that often does not answer in time |
| responses look truncated or empty | `proxy.log` now carries `stop=` and `blocks=` per request. `blocks=none` on a 2xx is the empty-content failure, and `/__health` counts it as `empty_content_200s` |

A useful first probe, since it separates endpoint problems from credential ones - `401` means the credential isn't recognized, `402` means it is but isn't billable:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.meta.ai/v1/messages \
  -H "x-api-key: $(~/.config/claude-muse/api-key.sh)" \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"muse-spark-1.3","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```
