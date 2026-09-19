#!/usr/bin/env bash
# Removes the launch agent and the installed engine files. Leaves the Claude Code
# profile (sessions, history, settings) and the state files alone unless --purge,
# because losing a session history to a typo is not recoverable.
set -euo pipefail

STATE_DIR="${CLAUDE_MUSE_DIR:-$HOME/.config/claude-muse}"
PROFILE_DIR="${CLAUDE_MUSE_PROFILE:-$HOME/.claude-profiles/muse}"
AGENT="${CLAUDE_MUSE_AGENT:-co.medallion.claude-muse-proxy}"
PLIST="$HOME/Library/LaunchAgents/$AGENT.plist"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '\033[2mclaude-muse:\033[0m %s\n' "$1"; }

launchctl bootout "gui/$(id -u)/$AGENT" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
say "launch agent removed"

rm -f "$STATE_DIR"/{proxy.py,probe.sh,api-key.sh,preflight.sh,model-env.sh,run-prompts.sh}
rm -rf "$STATE_DIR/engine"
say "engine files removed from $STATE_DIR"

if [ "$PURGE" = 1 ]; then
  rm -rf "$STATE_DIR"
  say "purged $STATE_DIR (learned.json, shapes.json, logs, backups)"
  say "profile left at $PROFILE_DIR — delete it by hand if you mean to lose the history"
else
  say "state kept: $STATE_DIR (learned.json, shapes.json, logs). --purge removes it"
fi

say "remove the 'source .../shell-function.zsh' line from ~/.zshrc to finish"
