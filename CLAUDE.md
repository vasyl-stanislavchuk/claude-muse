# claude-muse

Claude Code's client, pointed at Meta's `muse-spark-1.3` through a rewriting proxy on `127.0.0.1:8787`. The repo is the engine plus its installer; everything it produces at runtime lives outside the repo.

Read [`docs/design.md`](docs/design.md) before changing where a piece lives, and [`docs/api-subset.md`](docs/api-subset.md) before changing what the proxy repairs. Both record measurements, not intentions.

## Project structure

- `bin/proxy.py` - the rewriting proxy. Repairs the request shapes `api.meta.ai` rejects, passes everything else byte for byte
- `bin/probe.sh` - replays the request shapes that decide whether this works, against the proxy or the raw endpoint
- `bin/api-key.sh` - the `apiKeyHelper`. Unwraps the subscription key from the login keychain
- `bin/run-prompts.sh` - non-interactive harness for comparing models on the same prompts
- `lib/preflight.sh` - sourced on every launch. Sets the base URL, starts or restarts the proxy, probes it when the code moved
- `lib/model-env.sh` - identity env shared by the shell function and the batch harness
- `profile/`, `templates/` - the Claude Code profile and the two files that carry machine paths
- `install.sh` - wires all of it into `~/.config/claude-muse` and `~/.claude-profiles/muse`

## Editing the repo is editing the install

`install.sh` symlinks `bin/` and `lib/` into `~/.config/claude-muse`, so a change here is live at the next launch with nothing to copy. `.githooks/` re-runs it on every commit, pull and branch switch, which matters because `install.sh` names each file explicitly: **a new file needs a `place` line, and without one it is never linked no matter how many times the hook fires.** Three consequences worth holding onto:

- **The running proxy holds its source in memory.** An edited `proxy.py` does nothing until the proxy restarts. `preflight.sh` compares the running proxy's hash against the file and restarts it, which is the difference between a fix landing and a fix appearing to land.
- **Every `proxy.py` edit costs a probe.** The hash moves, so the next launch spends two real API calls proving the repairs still work. That runs about 28 seconds, almost all of it the web search call. Batching proxy changes is cheaper than trickling them.
- **`settings.json` is yours, so nothing rewrites it.** The template grows keys over time and the rendered file never gets them; `install.sh` names what is missing and leaves the merge to you. The hook surfaces that notice on every commit, which is the only reason you will hear about drift at all.

**Never restart the proxy while a session is using it.** `install.sh --no-agent` places files and leaves launchd alone.

## Testing a change

`python3 -m pytest tests/ -q` pins every rewrite rule offline. Run it before anything that spends a token.

`rewrite()` is a pure function over a JSON body, so most of the proxy is testable without spending a token:

```bash
# call rewrite() on a payload without touching the network
/usr/bin/python3 -c "
import importlib.util, json
spec = importlib.util.spec_from_file_location('p', 'bin/proxy.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.rewrite(json.dumps({'max_tokens': 200, 'tool_choice': {'type': 'none'}}).encode()))
"

# a second instance on a spare port, safe beside a live one
CLAUDE_MUSE_PROXY_PORT=8799 CLAUDE_MUSE_PROXY_LOG=/tmp/muse-test.log /usr/bin/python3 bin/proxy.py &
curl -s 127.0.0.1:8799/__health | jq
```

`bin/probe.sh https://api.meta.ai` is how you find out what the endpoint rejects; `bin/probe.sh http://127.0.0.1:8787` is how you find out whether the proxy fixed it. A repair added without a probe row is a claim nobody checks.

Say what you ran. "The rewrite rules pass offline; the paid probe runs on the next launch" is a complete answer.

## Constraints

- **Python 3.9, standard library only.** The launch agent runs `/usr/bin/python3`, which is 3.9.6. No `match`, no runtime `X | Y`. One exception, and it has to stay one: `pyyaml`, imported behind a `try` so the proxy falls back to the baked-in rules without it. A dependency that cannot be absent is not allowed here. `pytest` is dev-only and never imported by the engine.
- **Never read or log the credential.** `x-api-key` is forwarded as received. The key stays in the keychain, and nothing here caches it.
- **Repair shapes, never manufacture a verdict.** The proxy fixes requests the endpoint cannot parse. Answering the auto-mode safety classifier on the endpoint's behalf is a different thing, it would apply silently to every future session, and `permissions.allow` already exists for it.
- **`settings.json` must never gain a `model` key.** A settings model pin outranks `ANTHROPIC_MODEL` and would send every request to Anthropic on a Meta key. `install.sh` warns if one appears.
- **Runtime state stays out of the repo.** `learned.json`, `shapes.json`, `verified` and the logs live in `~/.config/claude-muse` and are gitignored.
- **Rarely-changing things go in the shell function, everything else in `preflight.sh`.** A shell function is a copy taken when the shell read it, so a check added there reaches only new terminals. `docs/design.md` has the morning this cost.

## Style

Docs and commit bodies follow the house voice: sentence case headings, contractions, second person, present tense, **no em-dashes**, and one paragraph on one line with no hard wrapping. Bold what the reader acts on, once per paragraph. Numbers beat adjectives. Comments explain a non-obvious why, never restate the code.

No AI attribution in commits.

`CHANGELOG.md` is release notes for whoever installs this: one sentence per change, bold the thing they can now do, plain text for the consequence. No file names or function names unless they have to type them.
