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
# The `security` binary needs standing access to the item. If Claude Code
# stalls on startup, run this script once by hand and choose Always Allow.
set -euo pipefail

security find-generic-password -s ai.meta.dev.credentials -a meta -w \
  | jq -r '.api_key'
