# Architecture

Five diagrams, wide to narrow. Start at the top if you are new here; drop to the level that matches the question you are asking. Every figure states one claim, and every number on every figure matches the code it draws.

- [D0 · System context](#d0)
- [D1 · Request lifecycle](#d1)
- [D2 · Repair internals](#d2)
- [D3 · Relay and retry](#d3)
- [D4 · Launch and probe](#d4)

<a id="d0"></a>
## D0 · System context

One localhost hop repairs, retries, and records every request; responses return to Claude Code. Runtime state lives under [~/.config/claude-muse/](auth.md), outside this repo.

![System context: Claude Code sends requests through one local proxy to api.meta.ai; responses return to Claude Code.](architecture.svg)

<a id="d1"></a>
## D1 · Request lifecycle

Every request passes seven numbered stages; local paths answer without touching the network and only the relay loop talks upstream. Read this before you touch [the engine](design.md).

![Request lifecycle: reload, short-circuit check, rewrite, census, relay, pump, and post stages with local exits.](architecture-request.svg)

<a id="d2"></a>
## D2 · Repair internals

Repairs apply in four layers, data before code; the retry loop teaches layer three. The measured rules behind each layer are in [the API subset](api-subset.md).

![Repair layers: static rules, reasoning, learned drops, and the tool-choice hatch.](architecture-rewrite.svg)

<a id="d3"></a>
## D3 · Relay and retry

Three failure classes, three treatments: wait and resend, learn and resend, or stop with a hint. Match each path to its log line when you debug a slow or failed request.

![Relay flowchart: cooldown gate, send, outcome decision, wait and learn resend loops, pump, respond.](architecture-relay.svg)

<a id="d4"></a>
## D4 · Launch and probe

A normal launch costs nothing; the first launch after an edit spends two calls proving it works. The launch rules behind this lane are in [the design notes](design.md).

![Launch loop: shell, model-env, pin and start, hash check, paid probe, exec, plus the manual probe harness.](architecture-launch.svg)
