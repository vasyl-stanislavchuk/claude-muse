#!/usr/bin/env bash
# Prints the Muse Code subscription API key. `muse login` provisions that key
# server-side and stores it in the login keychain wrapped in a small JSON
# envelope; this unwraps it.
#
# Claude Code calls this through the apiKeyHelper setting in
# ~/.claude-profiles/muse/settings.json, so a later `muse login` that rotates
# the key is picked up on the next read with nothing to edit. Nothing is
# cached on disk — the keychain stays the only copy.
#
# Two escape hatches, both unset by default. CLAUDE_MUSE_KEYCHAIN_SERVICE and
# CLAUDE_MUSE_KEYCHAIN_ACCOUNT repoint the lookup when the item moves;
# CLAUDE_MUSE_API_KEY_FILE names a 0600 file holding the bare key, tried only
# when the keychain misses. The file path may appear in errors; the key never
# does.
#
# The `security` binary needs standing access to the item. If Claude Code
# stalls on startup, run this script once by hand and choose Always Allow.
set -euo pipefail

SERVICE="${CLAUDE_MUSE_KEYCHAIN_SERVICE:-ai.meta.dev.credentials}"
ACCOUNT="${CLAUDE_MUSE_KEYCHAIN_ACCOUNT:-meta}"

key=""
if key="$(security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null \
    | jq -r '.api_key // empty' 2>/dev/null)" && [ -n "$key" ]; then
  printf '%s\n' "$key"
  exit 0
fi

KEY_FILE="${CLAUDE_MUSE_API_KEY_FILE:-}"
if [ -n "$KEY_FILE" ] && [ -f "$KEY_FILE" ]; then
  perms="$(stat -f '%Lp' "$KEY_FILE" 2>/dev/null || true)"
  if [ "$perms" != "600" ] && [ "$perms" != "400" ]; then
    echo "api-key.sh: refusing $KEY_FILE (permissions ${perms:-unknown}, want 600)" >&2
    exit 1
  fi
  printf '%s\n' "$(cat -- "$KEY_FILE")"
  exit 0
fi

echo "api-key.sh: no key in keychain ($SERVICE/$ACCOUNT) and no readable CLAUDE_MUSE_API_KEY_FILE" >&2
exit 1
