#!/usr/bin/env bash
# Replays the eight request shapes that decide whether claude-muse works.
# Usage: probe.sh [base-url]   default https://api.meta.ai (the raw endpoint)
# Against the raw endpoint five of these fail, one returns empty text and the last
# finds no sources. Against the proxy all nine pass.
set -uo pipefail

BASE="${1:-https://api.meta.ai}"
KEY="$(~/.config/claude-muse/api-key.sh)"
M='"model":"muse-spark-1.3"'
Q='"messages":[{"role":"user","content":"Answer in one word: what colour is a ripe banana?"}]'
S='"messages":[{"role":"user","content":"Perform a web search for the query: claude code latest version"}]'
WS='"tools":[{"type":"web_search_20250305","name":"web_search"'

run() {
  local name="$1" body="$2"
  local out code err text
  out=$(curl -s -m 180 -w '\n%{http_code}' "$BASE/v1/messages" \
    -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d "$body")
  code=$(printf '%s' "$out" | tail -n1)
  body=$(printf '%s' "$out" | sed '$d')
  err=$(printf '%s' "$body" | jq -r '.error.message // empty' 2>/dev/null)
  text=$(printf '%s' "$body" | jq -r '[.content[]?|select(.type=="text")|.text]|join("")' 2>/dev/null)
  if [ -n "$err" ]; then
    printf '  %-28s %s  FAIL  %s\n' "$name" "$code" "$err"
  elif [ -z "$text" ]; then
    printf '  %-28s %s  EMPTY (no text block)\n' "$name" "$code"
  else
    printf '  %-28s %s  ok    %s\n' "$name" "$code" "$(printf '%s' "$text" | tr '\n' ' ' | cut -c1-60)"
  fi
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
