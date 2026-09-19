#!/usr/bin/env bash
# Replays the request shapes that decide whether claude-muse works.
# Usage: probe.sh [base-url]   default https://api.meta.ai (the raw endpoint)
# Against the raw endpoint five of these fail, one returns empty text and the last
# finds no sources. Against the proxy all nine pass.
#
# Every row prints wall time, because for the reasoning rows latency IS the
# finding: a 200 that takes 60s is what makes Claude Code's auto-mode classifier
# time out and deny the tool call. PROBE_REPS=5 repeats each row, since the
# failure mode is p90 rather than median.
#
# The reasoning rows below only run against the raw endpoint. Sending a field
# the endpoint rejects through the proxy would teach it that name permanently
# (offending_fields -> remember -> learned.json), which is not something a probe
# should do as a side effect.
set -uo pipefail

BASE="${1:-https://api.meta.ai}"
KEY="$(~/.config/claude-muse/api-key.sh)"
M='"model":"'"${ANTHROPIC_MODEL:-muse-spark-1.3}"'"'
Q='"messages":[{"role":"user","content":"Answer in one word: what colour is a ripe banana?"}]'
R='"messages":[{"role":"user","content":"A cube is painted red on all faces, then cut into 3x3x3 unit cubes. How many unit cubes have exactly two painted faces? Show your reasoning, then give the number."}]'
S='"messages":[{"role":"user","content":"Perform a web search for the query: claude code latest version"}]'
WS='"tools":[{"type":"web_search_20250305","name":"web_search"'

REPS="${PROBE_REPS:-1}"

once() {
  local name="$1" body="$2"
  local out tail code secs err text out_tokens
  out=$(curl -s -m 180 -w '\n%{http_code} %{time_total}' "$BASE/v1/messages" \
    -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d "$body")
  tail=$(printf '%s' "$out" | tail -n1)
  code=${tail%% *}
  secs=${tail##* }
  body=$(printf '%s' "$out" | sed '$d')
  err=$(printf '%s' "$body" | jq -r '.error.message // empty' 2>/dev/null)
  text=$(printf '%s' "$body" | jq -r '[.content[]?|select(.type=="text")|.text]|join("")' 2>/dev/null)
  out_tokens=$(printf '%s' "$body" | jq -r '.usage.output_tokens // empty' 2>/dev/null)
  if [ -n "$err" ]; then
    printf '  %-28s %s %7ss  FAIL  %s\n' "$name" "$code" "$secs" "$err"
  elif [ -z "$text" ]; then
    printf '  %-28s %s %7ss  EMPTY (no text block, out=%s)\n' "$name" "$code" "$secs" "${out_tokens:-?}"
  else
    printf '  %-28s %s %7ss  ok  out=%-5s %s\n' "$name" "$code" "$secs" "${out_tokens:-?}" \
      "$(printf '%s' "$text" | tr '\n' ' ' | cut -c1-44)"
  fi
}

run() {
  local i=1
  while [ "$i" -le "$REPS" ]; do
    once "$1" "$2"
    i=$((i + 1))
  done
}

echo "probing $BASE"
run "baseline"              "{$M,\"max_tokens\":4096,$Q}"
run "web_search +max_uses"  "{$M,\"max_tokens\":4096,$S,$WS,\"max_uses\":8}]}"
run "web_search bare"       "{$M,\"max_tokens\":4096,$S,$WS}]}"
run "web_search +domains"   "{$M,\"max_tokens\":4096,$S,$WS,\"allowed_domains\":[\"anthropic.com\"]}]}"
run "tool_choice named"     "{$M,\"max_tokens\":4096,\"tool_choice\":{\"type\":\"tool\",\"name\":\"web_search\"},$S,$WS}]}"
run "tool_choice any"       "{$M,\"max_tokens\":4096,\"tool_choice\":{\"type\":\"any\"},$S,$WS}]}"
run "thinking disabled"     "{$M,\"max_tokens\":4096,\"thinking\":{\"type\":\"disabled\"},$Q}"
run "max_tokens 200"        "{$M,\"max_tokens\":200,$Q}"

# Reasoning rows. Raw endpoint only: see the note at the top of this file.
if [ "$BASE" = "${BASE#http://127.0.0.1}" ] && [ "$BASE" = "${BASE#http://localhost}" ]; then
  echo "  -- reasoning: what makes a mechanical call cheap, and what makes the main loop think --"
  # The classifier as Claude Code actually sends it: no reasoning directive at
  # all, small ceiling. This is the latency every row below is measured against.
  run "classifier bare @2112"      "{$M,\"max_tokens\":2112,$Q}"
  run "classifier bare @4096"      "{$M,\"max_tokens\":4096,$Q}"
  # Does the endpoint take a reasoning field from a client, or is
  # `reasoning_effort` only its own internal spelling of the thinking block?
  run "reasoning_effort none"      "{$M,\"max_tokens\":4096,\"reasoning_effort\":\"none\",$Q}"
  run "reasoning_effort low"       "{$M,\"max_tokens\":4096,\"reasoning_effort\":\"low\",$Q}"
  run "reasoning_effort minimal"   "{$M,\"max_tokens\":4096,\"reasoning_effort\":\"minimal\",$Q}"
  run "effort low"                 "{$M,\"max_tokens\":4096,\"effort\":\"low\",$Q}"
  # The lowest budget the Anthropic schema allows, at both ceilings.
  run "thinking budget 1024"       "{$M,\"max_tokens\":4096,\"thinking\":{\"type\":\"enabled\",\"budget_tokens\":1024},$Q}"
  run "thinking budget 1024 @2112" "{$M,\"max_tokens\":2112,\"thinking\":{\"type\":\"enabled\",\"budget_tokens\":1024},$Q}"
  # The other direction: is the main loop's wire shape (thinking adaptive, no
  # budget) actually reasoning, and does naming a large budget change it? $R
  # rewards reasoning, so out= separates "thought harder" from "wrote more".
  run "adaptive (main-loop shape)" "{$M,\"max_tokens\":8192,\"thinking\":{\"type\":\"adaptive\"},$R}"
  run "budget 32000"               "{$M,\"max_tokens\":8192,\"thinking\":{\"type\":\"enabled\",\"budget_tokens\":32000},$R}"
  run "no thinking key"            "{$M,\"max_tokens\":8192,$R}"
fi

# The endpoint never returns web_search_tool_result blocks, so Claude Code has no
# sources to render and no URL to follow up with. The proxy rebuilds that block
# from the pages the model opened; this is the check that it still does.
sources=$(curl -s -m 300 "$BASE/v1/messages" \
  -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d "{$M,\"max_tokens\":6000,\"messages\":[{\"role\":\"user\",\"content\":\"Perform a web search for the query: claude code release notes. Read a couple of the pages you find.\"}],$WS}]}" \
  | jq '[.content[]?|select(.type=="web_search_tool_result")|.content[]]|length' 2>/dev/null)
if [ "${sources:-0}" -gt 0 ]; then
  printf '  %-28s %s  ok    %s structured sources\n' "web_search sources" 200 "$sources"
else
  printf '  %-28s %s  NONE  no web_search_tool_result block\n' "web_search sources" 200
fi
