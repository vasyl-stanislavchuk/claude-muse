# Promoting learned state

`learned.json` is the proxy teaching itself: every entry is a field the endpoint rejected by name. Left alone that memory adapts one install and never ships, so review turns accidents into policy and clears what went stale. The `/review-learned` skill runs this flow in a session; it duplicates the operational core below because a session runs in any repo, so if the two disagree the skill says so and this page wins.

## What learned state is

The file holds field names in one of two shapes: the legacy bare list, or the current dict mapping each field to its `first_seen` timestamp and `hits` count. Two seeds, `stop_sequences` and `safeguards`, are always present in memory on top of whatever the file holds, so pruning one from disk changes nothing. New fields land through atomic writes, the file caps at 100 entries, and the proxy re-reads it on every request, so a hand-edit takes effect on the next request and logs a `reloaded` line. How entries get there in the first place is in [`api-subset.md`](api-subset.md), under the learn-and-retry loop.

## When you review

You review on events, never on a calendar. Five triggers: a `learned-full` line (the cap stopped recording); a `learned: refusing` line (a 400 named a field the proxy sets itself); a repeat-hitting field you do not recognize; a new `shape:` signature in the log; or a surprise session where a 400 or an empty reply reached the user.

## The review packet

Set `R="$HOME/.config/claude-muse"` and `P="${CLAUDE_MUSE_PROXY_PORT:-8787}"`. Everything below is read-only: you read the log, the state files, and `/__health`, and you send no other request anywhere.

Record two facts first, since they scope everything else: the `version` hash from `/__health` (your findings belong to that engine), and the log-generation coverage from `ls -l "$R"/proxy.log*` (rotation keeps one `.1` generation at 1 MiB; note whether it exists and whether you searched it).

1. Live state: `curl -s -m 5 "http://127.0.0.1:$P/__health" | jq '{version, learned, learned_hits, shapes, requests, by_status, uptime_s, pid, no_reasoning_requests, empty_content_200s}'`. If curl fails the proxy is down: take the learned set from the on-disk file in step 2 instead and keep going. `learned` is the sorted in-memory list, which is what `rewrite()` strips right now; `learned_hits` counts first-time learns this process only, never strips.
2. Learned inventory, tolerating both on-disk formats: `jq -r 'if type == "array" then .[] | "\(.) (legacy, no timestamps)" else to_entries[] | "\(.key) first_seen=\(.value.first_seen // "none") hits=\(.value.hits // 0)" end' "$R/learned.json"`.
3. Strip counts per field, over every log generation present: `jq -r 'if type == "array" then .[] else keys[] end' "$R/learned.json" | while IFS= read -r f; do strips=$(grep -h -- "-$f" "$R"/proxy.log* 2>/dev/null | wc -l); printf '%s strips=%s\n' "$f" "$strips"; done`. Request lines carry one `-{field}` note per stripped field.
4. Learn, refuse, cap, and reload events: `grep -h 'learned: drop\|learned: refusing\|learned-full\|reloaded .*learned.json' "$R"/proxy.log*`.
5. New-shape sightings: `grep -h 'shape: ' "$R"/proxy.log* | tail -30`.
6. Wire presence per field: `jq -r 'if type == "array" then .[] else keys[] end' "$R/learned.json" | while IFS= read -r f; do shapes=$(jq --arg f "$f" '[.[] | select(test("keys=[^ ]*\\b" + $f + "\\b"))] | length' "$R/shapes.json"); printf '%s shapes=%s\n' "$f" "$shapes"; done`. The census is credential-safe by header allowlist, so this never touches the key.
7. Unrepaired 400s, the watch-list feed: `grep -h 'unnamed\|unrecoverable\|attempts-exhausted' "$R"/proxy.log*; grep -h ' 400 ' "$R"/proxy.log* | cut -c1-300 | tail -20`.

## Reading the packet

You give every field one verdict: `promote` when it strips on live traffic, still arrives on the wire, and reads as stable policy rather than an accident; `watch` when it strips rarely or its wire presence is unclear, since staying learned costs nothing and the next trigger re-checks it; `prune` for the `top_k` shape of zero or one strip ever and zero wire presence, which is dead weight against the cap; `investigate` for a strip-miss, any `refusing` line, or any `learned-full` line, where you name the cause before picking one of the other three. Two traps apply throughout: on-disk `hits` is stale by design, since re-hits bump the in-memory count only, so you never rank by it; and `-tool_choice` log notes are never learned entries, since that key travels a separate retry path.

## Promoting an entry

A promotion is done when all five hold: an explicit repo change rather than learned state, an offline test pinning it, a measured row in `api-subset.md`, a probe row proving it, and a changelog line. A promotion without the probe row is a claim nobody checks, which is exactly what the learned entry already was.

## Pruning an entry

You prune by hand-editing `learned.json`, which lands on the next request through hot reload. Afterwards the strip counts for that field stay zero and no re-learn line appears; if a `learned: drop` line for it does appear, the endpoint still rejects it and the prune was wrong.

## What you never do

You never replay a pruned or candidate field through the proxy to test it. A 400 naming it runs `offending_fields -> remember` and re-learns exactly what you removed. You verify through the census `keys=` bits and the strip counts only. `bin/probe.sh`'s header states the same rule for why its reasoning rows stay on the raw endpoint.
