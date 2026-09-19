#!/usr/bin/env bash
# Run every prompts/NN.txt in a directory through muse-spark-1.3, writing replies/muse/NN.md.
#
# Non-interactive twin of the `claude-muse` zshrc function: a Bash tool call does not source
# .zshrc, so the env is replicated here rather than calling the function.
#
# Usage: run-prompts.sh <ab-dir>
#   <ab-dir>/prompts/NN.txt  ->  <ab-dir>/replies/muse/NN.md
#
# Sends the prompt bodies to https://api.meta.ai. For a context project that means the project's
# retrieved evidence leaves the machine, so settle the subscription's training and retention terms
# before pointing this at a real corpus.
set -euo pipefail

DIR="${1:?usage: run-prompts.sh <ab-dir>}"
[ -d "$DIR/prompts" ] || { echo "no $DIR/prompts" >&2; exit 2; }
mkdir -p "$DIR/replies/muse"

unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
export CLAUDE_CONFIG_DIR="$HOME/.claude-profiles/muse"
export ANTHROPIC_BASE_URL="https://api.meta.ai"
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=3600000
export ANTHROPIC_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_OPUS_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_SONNET_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="muse-spark-1.3"
export CLAUDE_CODE_SUBAGENT_MODEL="muse-spark-1.3"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000

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
