---
name: review-learned
description: Review the proxy's learned state and report a promote, watch, prune, or investigate verdict per field. Use when proxy.log shows learned-full or learned: refusing, when learned.json holds a field you do not recognize, when a new shape: signature appears, when a surprise 400 reaches the user, or when asked to review learned state or decide what to promote or prune.
---

# Review learned

You review what the proxy taught itself and report one verdict per field. You change nothing in this skill: promotion and pruning are separate steps the user approves after reading your table.

This skill is self-contained. A muse session runs in any repo, so the operational core below duplicates `docs/promotion.md` in the claude-muse checkout instead of linking it. If the checkout is at hand and the two disagree, the doc wins and you say so in your report.

## When to run this

Run on events, never on a calendar. Invoke with `/review-learned` when you see one of these: a `learned-full` line (the 100-field cap stopped recording); a `learned: refusing` line (a 400 named a field the proxy sets itself); a repeat-hitting field you do not recognize; a new `shape:` signature in the log; or a surprise session where a 400 or an empty reply reached the user.

## Step 1: assemble the packet

All state lives under one dir. Set `R="$HOME/.config/claude-muse"` and `P="${CLAUDE_MUSE_PROXY_PORT:-8787}"`. Everything here is read-only: you read the log, the state files, and `/__health`, and you send no other request anywhere.

Record two facts first, since they scope everything else: the `version` hash from `/__health` (findings belong to that engine), and the log-generation coverage from `ls -l "$R"/proxy.log*` (rotation keeps one `.1` generation at 1 MiB; note whether it exists and whether you searched it).

1. Live state: `curl -s -m 5 "http://127.0.0.1:$P/__health" | jq '{version, learned, learned_hits, shapes, requests, by_status, uptime_s, pid, no_reasoning_requests, empty_content_200s}'`. If curl fails the proxy is down: say so, take the learned set from the on-disk file in step 2 instead, and keep going. `learned` is the sorted in-memory list, which is what `rewrite()` strips right now; `learned_hits` counts first-time learns this process only, never strips.
2. Learned inventory, tolerating both on-disk formats: `jq -r 'if type == "array" then .[] | "\(.) (legacy, no timestamps)" else to_entries[] | "\(.key) first_seen=\(.value.first_seen // "none") hits=\(.value.hits // 0)" end' "$R/learned.json"`.
3. Strip counts per field, over every log generation present: `jq -r 'if type == "array" then .[] else keys[] end' "$R/learned.json" | while IFS= read -r f; do strips=$(grep -h -- "-$f" "$R"/proxy.log* 2>/dev/null | wc -l); printf '%s strips=%s\n' "$f" "$strips"; done`. Request lines carry one `-{field}` note per stripped field.
4. Learn, refuse, cap, and reload events: `grep -h 'learned: drop\|learned: refusing\|learned-full\|reloaded .*learned.json' "$R"/proxy.log*`.
5. New-shape sightings: `grep -h 'shape: ' "$R"/proxy.log* | tail -30`.
6. Wire presence per field: `jq -r 'if type == "array" then .[] else keys[] end' "$R/learned.json" | while IFS= read -r f; do shapes=$(jq --arg f "$f" '[.[] | select(test("keys=[^ ]*\\b" + $f + "\\b"))] | length' "$R/shapes.json"); printf '%s shapes=%s\n' "$f" "$shapes"; done`. The census is credential-safe by header allowlist, so this grep-equivalent never touches the key.
7. Unrepaired 400s, the watch-list feed: `grep -h 'unnamed\|unrecoverable\|attempts-exhausted' "$R"/proxy.log*; grep -h ' 400 ' "$R"/proxy.log* | cut -c1-300 | tail -20`.

## Step 2: verdict every entry

One row per field: field, strips, wire shapes, first seen, verdict, why.

- `promote`: it strips on live traffic, it still arrives on the wire, and the drop reads as stable policy rather than an accident. The follow-up has five parts: an explicit repo change, an offline test, an api-subset row, a probe row, and a changelog line.
- `watch`: it strips rarely or its wire presence is unclear. It stays learned at no cost; the next trigger re-checks it.
- `prune`: the `top_k` shape, zero or one strip ever and zero wire presence across every shape. Dead weight against the cap. Seeds (`stop_sequences`, `safeguards`) are the exception: they are always present in memory, so pruning them from disk changes nothing.
- `investigate`: a strip-miss (the field is learned and arrives on the wire but never strips), any `refusing` line, any `learned-full` line. Name the cause first, then pick one of the other three.

Two traps: on-disk `hits` is stale by design, since re-hits bump the in-memory count only, so never rank by it; and `-tool_choice` log notes are never learned entries, since that key travels a separate retry path.

## Step 3: report, do not change

End with the per-field table plus your recommended action per row and stop. If the user approves a promotion, its definition of done is the five parts above. If they approve a prune, it is a hand-edit to `learned.json` that lands on the next request through hot reload; afterwards strip counts stay zero and no re-learn line appears.

## Prohibition

Never replay a pruned or candidate field through the proxy to test it. A 400 naming it runs `offending_fields -> remember` and re-learns exactly what you removed. Verify through the census `keys=` bits and the strip counts only. This is a hard stop, not guidance; `bin/probe.sh`'s header comment states the same rule for why its reasoning rows stay on the raw endpoint.
