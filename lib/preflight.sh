# shellcheck shell=bash
# Sourced by bin/claude-muse before it execs claude. Not executable on its own.
#
# Everything that might change lives here rather than in the shell function,
# because this file is read fresh on every launch while the function is a copy
# held in whatever shell you happen to be sitting in. A shell opened before the
# function last changed keeps running the old body forever, which is how a
# session silently went straight at api.meta.ai and lost web search and every
# subagent for an entire morning. Put a check in the function and only new
# shells get it; put it here and everyone does.
#
# What it guarantees before claude starts:
#   1. the base URL points at the proxy, whatever the calling shell believed
#   2. the proxy is running, and is running the current engine
#   3. the proxy answers its health endpoint
#   4. the repairs actually work end to end — re-probed only when the engine changed
#
# Escape hatches: CLAUDE_MUSE_SKIP_PROBE=1 skips step 4, CLAUDE_MUSE_DIRECT=1
# skips all of it and talks to the endpoint raw.

CLAUDE_MUSE_DIR="$HOME/.config/claude-muse"
CLAUDE_MUSE_PORT=8787
CLAUDE_MUSE_AGENT="co.medallion.claude-muse-proxy"
CLAUDE_MUSE_VERIFIED="$CLAUDE_MUSE_DIR/verified"

_muse_say()  { printf '\033[2mclaude-muse:\033[0m %s\n' "$1" >&2; }
_muse_warn() { printf '\033[33mclaude-muse:\033[0m %s\n' "$1" >&2; }
_muse_fail() { printf '\033[31mclaude-muse:\033[0m %s\n' "$1" >&2; }

if [ "${CLAUDE_MUSE_DIRECT:-0}" = "1" ]; then
  export ANTHROPIC_BASE_URL="https://api.meta.ai"
  _muse_warn "CLAUDE_MUSE_DIRECT=1 — bypassing the proxy. Web search and subagents will fail."
  return 0 2>/dev/null || true
fi

# Authoritative, so moving the proxy never needs anyone to reload a shell.
export ANTHROPIC_BASE_URL="http://127.0.0.1:$CLAUDE_MUSE_PORT"

# Auto mode can have the server run its safety checks inside the session's own
# requests, asked for with the `safeguards` request field and answered with
# `safeguard_results`. api.meta.ai rejects that field outright (400, unknown
# parameter), so the proxy strips it — meaning the checks can never reach this
# session no matter what. Left alone, Claude Code discovers that the hard way:
# it holds the first checked action, prints the "not eligible" notice, waits for
# you, and only then falls back to running the classifier itself. Declaring the
# gateway incapable up front skips the doomed round trip and the held action.
# Verdicts are unaffected either way — they are muse-spark-1.3's, which is why
# they are less consistent than Claude Code's usual classifier.
export CLAUDE_CODE_AUTO_MODE_SERVER=0

_muse_health() { curl -fsS -m 2 "http://127.0.0.1:$CLAUDE_MUSE_PORT/__health" 2>/dev/null; }

_muse_start() {
  launchctl load "$HOME/Library/LaunchAgents/$CLAUDE_MUSE_AGENT.plist" 2>/dev/null
  launchctl kickstart -k "gui/$(id -u)/$CLAUDE_MUSE_AGENT" >/dev/null 2>&1
  local i=0
  while [ $i -lt 40 ]; do
    _muse_health >/dev/null && return 0
    sleep 0.25
    i=$((i + 1))
  done
  return 1
}

health="$(_muse_health)"
if [ -z "$health" ]; then
  _muse_say "proxy not responding, starting it"
  _muse_start || { _muse_fail "proxy would not start. Check $CLAUDE_MUSE_DIR/proxy.err"; return 1 2>/dev/null || exit 1; }
  health="$(_muse_health)"
fi

running="$(printf '%s' "$health" | jq -r '.version // "unknown"')"
# The version covers the entry plus every engine module, concatenated in byte
# order to match source_hash() in engine/server.py. The LC_ALL=C subshell pins
# the glob order: an exotic LANG reordering __init__.py would restart the proxy
# on every launch instead of only when the code moved.
ondisk="$(LC_ALL=C; cat "$CLAUDE_MUSE_DIR/proxy.py" "$CLAUDE_MUSE_DIR"/engine/*.py 2>/dev/null | shasum -a 256 | cut -c1-12)"

# The proxy holds its source in memory, so an edited engine file does nothing until
# it is restarted. Catching that here is the difference between a fix landing and
# a fix appearing to land.
if [ -n "$ondisk" ] && [ "$running" != "$ondisk" ]; then
  _muse_say "engine changed since the proxy started — restarting"
  _muse_start || { _muse_fail "proxy would not restart. Check $CLAUDE_MUSE_DIR/proxy.err"; return 1 2>/dev/null || exit 1; }
  health="$(_muse_health)"
  running="$(printf '%s' "$health" | jq -r '.version // "unknown"')"
fi

# The paid probe. One call per repair class, and only when the code changed, so a
# normal launch spends nothing and a launch after an edit proves the edit works.
if [ "${CLAUDE_MUSE_SKIP_PROBE:-0}" != "1" ] && [ "$(cat "$CLAUDE_MUSE_VERIFIED" 2>/dev/null)" != "$running" ]; then
  _muse_say "new proxy version $running — probing the repairs"
  _muse_key="$("$CLAUDE_MUSE_DIR/api-key.sh" 2>/dev/null)"
  _muse_call() {
    curl -s -m 120 "http://127.0.0.1:$CLAUDE_MUSE_PORT/v1/messages" \
      -H "x-api-key: $_muse_key" -H 'anthropic-version: 2023-06-01' \
      -H 'content-type: application/json' -d "$1"
  }
  _muse_model="${ANTHROPIC_MODEL:-muse-spark-1.3}"
  # A classifier-shaped call: small max_tokens, which raw returns 200-but-empty.
  short="$(_muse_call '{"model":"'"$_muse_model"'","max_tokens":200,"messages":[{"role":"user","content":"Reply with one word: ok"}]}' \
    | jq -r '[.content[]?|select(.type=="text")|.text]|join("")')"
  # The exact web search shape Claude Code sends, max_uses and all.
  search="$(_muse_call '{"model":"'"$_muse_model"'","max_tokens":4096,"tool_choice":{"type":"tool","name":"web_search"},"messages":[{"role":"user","content":"Perform a web search for the query: what day is it today"}],"tools":[{"type":"web_search_20250305","name":"web_search","max_uses":8}]}' \
    | jq -r '.error.message // "ok"')"

  if [ -n "$short" ] && [ "$search" = "ok" ]; then
    printf '%s' "$running" > "$CLAUDE_MUSE_VERIFIED"
    _muse_say "repairs verified — short replies return text, web search accepted"
  else
    [ -z "$short" ] && _muse_warn "short replies still come back empty — the max_tokens floor is not applying"
    [ "$search" != "ok" ] && _muse_warn "web search still rejected: $search"
    _muse_warn "continuing anyway. $CLAUDE_MUSE_DIR/proxy.log has the detail."
  fi
  unset _muse_key _muse_model
fi

learned="$(printf '%s' "$health" | jq -r '.learned|length' 2>/dev/null)"
_muse_say "proxy $running ok, ${learned:-0} learned fields"

# _muse_call only exists when the probe branch ran, and unset -f on a function
# that was never defined returns 1. This file is sourced as `source ... || return 1`,
# so that stray 1 would abort the launch before claude ever execs. The trailing
# `true` is what makes the exit status mean "preflight passed" and nothing else.
unset -f _muse_say _muse_warn _muse_fail _muse_health _muse_start _muse_call 2>/dev/null
true
