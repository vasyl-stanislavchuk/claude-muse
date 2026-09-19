#!/usr/bin/env bash
# Installs claude-muse: the proxy, its launch agent, the Claude Code profile and
# the shell function. Idempotent — re-run it after a git pull.
#
# By default the installed files are symlinks back into this repo, so editing the
# repo is editing the install and preflight notices the changed hash on the next
# launch. --copy freezes copies instead, for a machine that should not track a
# working tree.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${CLAUDE_MUSE_DIR:-$HOME/.config/claude-muse}"
PROFILE_DIR="${CLAUDE_MUSE_PROFILE:-$HOME/.claude-profiles/muse}"
AGENT="${CLAUDE_MUSE_AGENT:-co.medallion.claude-muse-proxy}"
PLIST="$HOME/Library/LaunchAgents/$AGENT.plist"
PYTHON="${CLAUDE_MUSE_PYTHON:-/usr/bin/python3}"

MODE="link"
CHECK_ONLY=0
WITH_AGENT=1
for arg in "$@"; do
  case "$arg" in
    --copy)  MODE=copy ;;
    --link)  MODE="link" ;;
    --check) CHECK_ONLY=1 ;;
    --no-agent) WITH_AGENT=0 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[2mclaude-muse:\033[0m %s\n' "$1"; }
warn() { printf '\033[33mclaude-muse:\033[0m %s\n' "$1" >&2; }
fail() { printf '\033[31mclaude-muse:\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

[ "$(uname -s)" = "Darwin" ] || fail "macOS only — this installs a launchd agent and reads the login keychain."
for tool in curl jq shasum launchctl security; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing required tool: $tool"
done
[ -x "$PYTHON" ] || fail "no python3 at $PYTHON (override with CLAUDE_MUSE_PYTHON)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "$PYTHON is older than 3.9"
command -v claude >/dev/null 2>&1 || warn "claude is not on PATH — install Claude Code before first launch"

if [ "$CHECK_ONLY" = 1 ]; then
  say "environment looks good; run without --check to install"
  exit 0
fi

# ---------------------------------------------------------------- helpers

BACKUP="$STATE_DIR/.backups/$(date +%Y%m%d-%H%M%S)"

preserve() {  # keep whatever is already there before replacing it
  local path="$1"
  [ -e "$path" ] || [ -L "$path" ] || return 0
  mkdir -p "$BACKUP"
  cp -Rp "$path" "$BACKUP/$(basename "$path")"
}

place() {     # place() <repo-relative source> <destination>
  local src="$REPO/$1" dest="$2"
  [ -e "$src" ] || fail "missing from repo: $1"
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ] && [ "$MODE" = link ]; then
    return 0                       # already correct, leave it alone
  fi
  preserve "$dest"
  rm -f "$dest"
  if [ "$MODE" = link ]; then ln -s "$src" "$dest"; else cp -p "$src" "$dest"; fi
}

place_dir() {  # place_dir() <repo-relative source dir> <destination>
  local src="$REPO/$1" dest="$2"
  [ -d "$src" ] || fail "missing from repo: $1"
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ] && [ "$MODE" = link ]; then
    return 0                       # already correct, leave it alone
  fi
  preserve "$dest"
  rm -rf "$dest"
  if [ "$MODE" = link ]; then ln -s "$src" "$dest"; else cp -Rp "$src" "$dest"; fi
}

render() {    # render() <template> <destination>; never clobbers silently
  local out
  out="$(sed -e "s|__STATE_DIR__|$STATE_DIR|g" \
             -e "s|__PROFILE_DIR__|$PROFILE_DIR|g" \
             -e "s|__AGENT__|$AGENT|g" \
             -e "s|__PYTHON__|$PYTHON|g" "$REPO/$1")"
  if [ -e "$2" ] && [ "$out" != "$(cat "$2")" ]; then preserve "$2"; fi
  printf '%s\n' "$out" > "$2"
}

# ---------------------------------------------------------------- install

mkdir -p "$STATE_DIR" "$PROFILE_DIR" "$HOME/Library/LaunchAgents"

place proxy.py              "$STATE_DIR/proxy.py"
place_dir engine            "$STATE_DIR/engine"
place bin/probe.sh          "$STATE_DIR/probe.sh"
place bin/api-key.sh        "$STATE_DIR/api-key.sh"
place bin/run-prompts.sh    "$STATE_DIR/run-prompts.sh"
place lib/preflight.sh      "$STATE_DIR/preflight.sh"
place lib/model-env.sh      "$STATE_DIR/model-env.sh"
place profile/statusline.sh "$PROFILE_DIR/statusline.sh"
mkdir -p "$PROFILE_DIR/hooks"
place profile/CLAUDE.md          "$PROFILE_DIR/CLAUDE.md"
place profile/hooks/continue-gate "$PROFILE_DIR/hooks/continue-gate"
say "engine installed into $STATE_DIR ($MODE)"

# rewrite-rules.yaml is policy the user may edit, so it is seeded once and
# thereafter left alone like settings.json. A future version bump will need an
# upgrade path that preserves edits; version 1 has no predecessor to migrate.
if [ -e "$STATE_DIR/rewrite-rules.yaml" ]; then
  say "rewrite-rules.yaml exists — left as is (template: templates/rewrite-rules.yaml)"
else
  place templates/rewrite-rules.yaml "$STATE_DIR/rewrite-rules.yaml"
  say "wrote $STATE_DIR/rewrite-rules.yaml"
fi

# The proxy reads that file with pyyaml and falls back to baked-in rules without
# it. Best effort: a missing pip or no network must never fail the install.
"$PYTHON" -c 'import yaml' 2>/dev/null \
  || "$PYTHON" -m pip install --user --quiet --disable-pip-version-check pyyaml >/dev/null 2>&1 \
  || warn "pyyaml is missing for $PYTHON — the proxy uses baked-in rules until it is installed"
command -v pre-commit >/dev/null 2>&1 \
  || say "pre-commit is missing — commits skip the lint suite until it is installed (pipx install pre-commit)"

# settings.json carries the user's own permissions.allow list, so it is written
# once and thereafter left alone. A settings model pin would outrank
# ANTHROPIC_MODEL, which is why the template has no "model" key and why this
# refuses to overwrite a file that might have grown one deliberately.
if [ -e "$PROFILE_DIR/settings.json" ]; then
  say "settings.json exists — left as is (template: profile/settings.json.tmpl)"
else
  render profile/settings.json.tmpl "$PROFILE_DIR/settings.json"
  say "wrote $PROFILE_DIR/settings.json"
fi
if grep -q '"model"' "$PROFILE_DIR/settings.json" 2>/dev/null; then
  warn "settings.json has a \"model\" key — it outranks ANTHROPIC_MODEL. Remove it."
fi

# An existing settings.json is never overwritten, so a template that grew a key
# would otherwise reach only new installs. Name the gap; never close it.
if [ -e "$PROFILE_DIR/settings.json" ]; then
  drift="$("$PYTHON" - "$REPO/profile/settings.json.tmpl" "$PROFILE_DIR/settings.json" <<'PYEOF'
import json, sys
try:
    tmpl = json.load(open(sys.argv[1]))
    live = json.load(open(sys.argv[2]))
except Exception:
    sys.exit(0)
missing = [k for k in tmpl if k not in live]
t_allow = tmpl.get("permissions", {}).get("allow", [])
l_allow = live.get("permissions", {}).get("allow", [])
missing += [a for a in t_allow if a not in l_allow]
t_hooks = set(tmpl.get("hooks", {}))
l_hooks = set(live.get("hooks", {}))
missing += sorted("hooks." + h for h in t_hooks - l_hooks)
print(", ".join(missing))
PYEOF
)"
  if [ -n "$drift" ]; then
    warn "settings.json is missing what the template now has: $drift"
    warn "add them by hand — this file is yours and install.sh never rewrites it"
  fi
fi

# Point git at the tracked hooks, so a commit or a pull re-links the install
# without anyone remembering to. Local config, never pushed; harmless outside a
# checkout, and left alone if you have pointed hooksPath somewhere of your own.
if [ -d "$REPO/.git" ] && [ -d "$REPO/.githooks" ] && command -v git >/dev/null 2>&1; then
  current="$(git -C "$REPO" config --local --get core.hooksPath 2>/dev/null || true)"
  if [ -z "$current" ]; then
    git -C "$REPO" config --local core.hooksPath .githooks
    say "git hooks enabled — commits and pulls now re-link the install"
  elif [ "$current" != ".githooks" ]; then
    warn "core.hooksPath is $current, leaving it alone; .githooks/ is not active"
  fi
fi

if [ "$WITH_AGENT" = 0 ]; then
  say "--no-agent: skipping the launch agent and the health check"
  say "files are in place; start the proxy yourself with $PYTHON $STATE_DIR/proxy.py"
  [ -d "$BACKUP" ] && say "replaced files backed up to $BACKUP"
  exit 0
fi

render templates/launchagent.plist.tmpl "$PLIST"
launchctl bootout "gui/$(id -u)/$AGENT" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
  || launchctl load "$PLIST" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/$(id -u)/$AGENT" >/dev/null 2>&1 || true

port="$(sed -n 's/^CLAUDE_MUSE_PORT=\([0-9]*\)/\1/p' "$REPO/lib/preflight.sh" | head -1)"
port="${port:-8787}"
for _ in $(seq 1 40); do
  health="$(curl -fsS -m 2 "http://127.0.0.1:$port/__health" 2>/dev/null || true)"
  [ -n "$health" ] && break
  sleep 0.25
done
if [ -n "${health:-}" ]; then
  say "proxy up: $(printf '%s' "$health" | jq -r '"version \(.version), pid \(.pid), upstream \(.upstream)"')"
else
  warn "proxy did not answer on :$port — check $STATE_DIR/proxy.err"
fi

[ -d "$BACKUP" ] && say "replaced files backed up to $BACKUP"

cat <<EOF

Add the shell function, once:

    echo 'source $REPO/templates/shell-function.zsh' >> ~/.zshrc && exec zsh

Then authenticate Meta's CLI if you have not:  muse login
And start a session:                           claude-muse

EOF
