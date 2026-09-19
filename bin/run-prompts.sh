#!/usr/bin/env bash
# Run every prompts/NN.txt in a directory through muse-spark-1.3, writing replies/muse/NN.md.
#
# Non-interactive twin of the `claude-muse` zshrc function: a Bash tool call does not source
# .zshrc, so identity env comes from lib/model-env.sh rather than the function.
#
# Usage: run-prompts.sh [--direct] <ab-dir>
#   <ab-dir>/prompts/NN.txt  ->  <ab-dir>/replies/muse/NN.md
#
# Requests go through the local proxy like interactive sessions, so repairs apply and shapes
# are recorded. --direct skips the proxy for deliberate A/B comparisons.
#
# Sends the prompt bodies to https://api.meta.ai. For a context project that means the project's
# retrieved evidence leaves the machine, so settle the subscription's training and retention terms
# before pointing this at a real corpus.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/model-env.sh
source "$SCRIPT_DIR/../lib/model-env.sh"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
DIRECT=0
if [ "${1:-}" = "--direct" ]; then DIRECT=1; shift; fi
DIR="${1:?usage: run-prompts.sh [--direct] <ab-dir>}"
[ -d "$DIR/prompts" ] || { echo "no $DIR/prompts" >&2; exit 2; }
mkdir -p "$DIR/replies/muse"

if [ "$DIRECT" = 1 ]; then
  echo "run-prompts: --direct, bypassing the proxy (repairs off, shapes unrecorded)" >&2
  export ANTHROPIC_BASE_URL="https://api.meta.ai"
else
  # Must match CLAUDE_MUSE_PORT in lib/preflight.sh, which owns the port.
  export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
  curl -fsS -m 2 "$ANTHROPIC_BASE_URL/__health" >/dev/null 2>&1 || {
    echo "run-prompts: proxy is down on :8787 — launch claude-muse once, or check ~/.config/claude-muse/proxy.err" >&2
    exit 1
  }
fi

for f in "$DIR"/prompts/*.txt; do
  n=$(basename "$f" .txt)
  out="$DIR/replies/muse/$n.md"
  if [ -s "$out" ]; then
    echo "  $n  skipped, already written"
    continue
  fi
  # Tools off keeps this pure synthesis from the prompt, matching how the sonnet side ran.
  # --disallowed-tools is variadic, so each name is its own argument.
  if claude -p "$(cat "$f")" \
      --disallowed-tools Bash Read Write Edit Glob Grep WebFetch WebSearch Task \
      > "$out" 2>"$out.err"; then
    echo "  $n  ok  $(wc -w < "$out" | tr -d ' ') words"
  else
    echo "  $n  FAILED  $(tail -1 "$out.err" 2>/dev/null)"
    rm -f "$out"
  fi
done
