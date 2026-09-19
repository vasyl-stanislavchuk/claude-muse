# Changelog

## [Unreleased]

## [0.2.0] - 2026-09-19

### Added

- **The proxy now rides out rate limits and brief outages.** A 429, 503 or dropped connection waits and resends up to 3 times, honoring the server's `Retry-After`, and the proxy cools down for 60s and answers `503` fast when the upstream stays down.
- **Request-scoped failures return immediately with a pointer.** A 401, 402 or context-over-window error is never retried or cooled down; the log line says whether to check the key or trim the context.
- **Every request gets an id, a latency and a tally.** The log line reads `r123 POST /v1/messages 200 812ms attempts=2 [notes]`, and `/__health` reports uptime, per-status counts and cooldown state.
- **The batch harness now runs through the proxy.** `run-prompts.sh` shares its env with the shell function from `lib/model-env.sh`, so repairs apply and shapes are recorded; `--direct` preserves the old raw-endpoint behavior for A/B runs.
- **`python3 -m pytest tests/` verifies the proxy offline**, so most changes can be checked without spending a token.
- **File edits no longer wait on a safety verdict.** `Write`, `Edit`, `MultiEdit` and `NotebookEdit` are allowed outright, so a slow classifier can no longer take away the model's ability to change files partway through a long task.
- **Sessions come with instructions to keep going.** A profile `CLAUDE.md` tells the model that an approved plan is the go-ahead, and a `Stop` hook blocks a turn that ends by asking permission to continue. It steps aside whenever background work is in flight, and gives up after two nudges.
- **The log says why each response stopped and what was in it.** Every line carries `stop=`, a per-type block tally and a thinking-token count, and `/__health` counts empty responses, stop reasons, abandoned requests and how much traffic arrives with no reasoning tier set.
- **The probe reports wall time on every row**, and `PROBE_REPS=5` repeats each one, because a slow success and a fast one are different answers.

### Changed

- **State files are written atomically and capped** at 100 learned fields and 500 shapes, and each learned field records when it was first seen and how often it has fired.
- **Repair policy now lives in `rewrite-rules.yaml`.** New endpoint quirks are fixed by editing data instead of code, take effect on the next request without a restart, and never cost a re-probe; per-model reasoning values live in the same file.
- **A request that asks for no reasoning now gets the cheapest reasoning available** instead of having the instruction deleted. This endpoint cannot switch reasoning off, so "disabled" becomes its lowest tier; measured on one mechanical call, that halved both the wall time and the thinking tokens.
- **The census records `output_config`**, which is where the reasoning tier actually travels. Whether an effort setting reaches the endpoint is now something you look up rather than argue about.
- **Overloads embedded in streams retry like the 429s they behave as.** The proxy holds response headers for the first event-group - bounded at 32KB and 5s - so a 200-embedded overload can still wait and resend invisibly; anything vaguer passes through untouched.
- **Timeouts are split by phase.** Connecting fails fast at 10s, accepted streams may run long on a 600s stall budget, and a 3600s wall clock (`CLAUDE_MUSE_STREAM_TIMEOUT`) cuts true runaways.
- **Policy and learned state reload without a restart.** Hand edits to `rewrite-rules.yaml` or `learned.json` land on the next request; the proxy's own writes stay silent.
- **`count_tokens` is served locally from a calibrated estimate.** Every tool-less response teaches the proxy characters-per-token, so `/context` cost panels beat the old silent `chars/4` fallback; the log line shows the ratio and sample count behind each number.
- **The proxy reads usage and model from every response.** The log line carries `in=`/`out=` token counts, `/__health` reports running totals, and a served model that differs from the requested one is logged as a substitution.
- **The keychain lookup is configurable.** `CLAUDE_MUSE_KEYCHAIN_SERVICE` / `CLAUDE_MUSE_KEYCHAIN_ACCOUNT` repoint it when the item moves, and `CLAUDE_MUSE_API_KEY_FILE` names a 0600 fallback file tried only when the keychain misses.

### Fixed

- **An empty JSON response body no longer corrupts chunk framing.** It used to emit a second stream terminator mid-response.
- **A keychain item without `.api_key` no longer authenticates as the literal string "null".** It now falls through to the file fallback or a clear error.
- **The proxy can no longer learn away its own repairs.** A rejection naming a field the proxy sets itself is refused rather than recorded, which used to be able to silently undo a repair on every later request.
- **An abandoned request is counted as one.** A session that gives up waiting used to be indistinguishable from one that was served.
- **A slow first token no longer loses the request.** The proxy used to arm a short socket timeout while it peeked at the start of a stream, which left the connection unreadable for good once it fired. Since this model thinks for several seconds before it says anything, that was the common case, not the rare one; the peek now waits without touching the socket.
- **An upstream that stops mid-response is reported rather than dropped.** You get what arrived and an `upstream-cut` line saying why it is short, instead of a stack trace and a lost reply.

## [0.1.0] - 2026-09-19

First packaged release. Previously this lived as loose files in `~/.config/claude-muse` on one machine.

### Added

- **`./install.sh` sets the whole thing up** - proxy, launch agent, Claude Code profile and shell function - and is safe to re-run after a pull. `--check` validates the environment first, `--copy` freezes copies instead of symlinks, `--no-agent` places files without touching a running proxy.
- **`./uninstall.sh` removes it again**, leaving session history alone unless you pass `--purge`.
- **The proxy records every distinct request shape it sees** to `shapes.json`, so questions about which fields and beta headers Claude Code sends are answered by looking rather than guessing.
- **`proxy.log` rotates at 1 MiB**, and `proxy.err` now means a crash rather than a second copy of the log.

### Fixed

- **`tool_choice: {"type":"none"}` is no longer rewritten to `auto`.** Only the named form is repaired, which is the only form the endpoint rejects. The old blanket rule turned "do not call tools" into "call tools if you like" - the proxy granting a permission rather than repairing a shape.
- **A 400 that names several unsupported fields now costs one retry instead of several.** Error bodies are read to 64 KiB, so a field named late in a long message is no longer invisible.
- **The last repair attempt is no longer wasted.** On its final pass the retry loop learned a field, rewrote the request and then returned without sending it, so the caller got an empty 400 and the fix only took effect on the following request.
- **A client disconnecting mid-stream no longer writes a stack trace.** Those accounted for every traceback in `proxy.err`.

### Changed

- **The status line's one-turn lag is documented as inherent**, not as a bug waiting on the `count_tokens` failure. The two are unrelated; the earlier note in the README was wrong.
