#!/bin/bash
#
# Status line for the muse profile. A copy of ~/.claude/statusline.sh with two
# changes: it shows the raw provider model id instead of a generic "Claude", and
# it reports whether this session is reaching the rewriting proxy.
#
# The flow this profile runs:
#
#   claude-muse()            a shell function in ~/.zshrc. Sets the config dir,
#     |                      the model ids and the context window, then sources
#     |                      preflight and execs claude.
#     v
#   preflight.sh             ~/.config/claude-muse/. Read fresh every launch.
#     |                      Sets ANTHROPIC_BASE_URL to the proxy, starts the
#     |                      proxy if it is down, restarts it if proxy.py changed
#     v                      since it started, and probes the repairs after a change.
#   claude                   talks to 127.0.0.1:8787, not api.meta.ai.
#     |
#     v
#   proxy.py                 repairs the request: strips web_search fields the
#     |                      endpoint rejects, rewrites a named tool_choice to auto, drops
#     |                      thinking:{type:disabled}, floors max_tokens at 4096,
#     |                      and learns any other field a 400 names. On the way
#     v                      back it rebuilds the missing web search sources block.
#   api.meta.ai              runs muse-spark-1.3.
#
# Why the indicator below matters: the function body is a copy held in whatever
# shell you launched from, so a shell opened before the function last changed
# still points at api.meta.ai. A session in that state loses web search and every
# subagent, and says nothing about why — it reports a "model classifier hiccup",
# because the auto-mode classifier is the first thing the endpoint rejects. The
# status line is evaluated live, so unlike the function it cannot go stale, and
# "direct" here is the signal to quit and relaunch from a fresh shell.

# Read JSON input from stdin
input=$(cat)

# Extract model name - use display_name if available
model_display=$(echo "$input" | jq -r '.model.display_name // empty')
if [ -z "$model_display" ]; then
    model_full=$(echo "$input" | jq -r '.model.id')
    if [[ "$model_full" == *"opus-4-5"* ]]; then
        model_display="Opus 4.5"
    elif [[ "$model_full" == *"sonnet-4-5"* ]]; then
        model_display="Sonnet 4.5"
    elif [[ "$model_full" == *"sonnet-4"* ]]; then
        model_display="Sonnet 4"
    elif [[ "$model_full" == *"opus"* ]]; then
        model_display="Opus"
    elif [[ "$model_full" == *"sonnet"* ]]; then
        model_display="Sonnet"
    elif [[ "$model_full" == *"haiku"* ]]; then
        model_display="Haiku"
    else
        # muse profile: show the raw provider id rather than a generic "Claude",
        # since this profile runs models the catalog doesn't name
        model_display="$model_full"
    fi
fi

# Calculate context - use current_usage if available, fallback to totals
current_dir=$(echo "$input" | jq -r '.workspace.current_dir')
size=$(echo "$input" | jq '.context_window.context_window_size')
current_usage=$(echo "$input" | jq '.context_window.current_usage')

# On this endpoint input_tokens excludes anything served from cache and
# cache_creation_input_tokens is always reported as 0, with written tokens landing
# in input_tokens instead. The three-way sum is still the true prompt size:
# measured, the same prompt reports in=3021 cold and in=92 + read=2929 warm.
if [ "$current_usage" != "null" ]; then
    tokens=$(echo "$current_usage" | jq '.input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens')
else
    # The upstream statusline falls back to total_input + total_output here. Those
    # are cumulative session counters, not occupancy: they only grow, they double
    # count every turn, and on a long session they sail past 100%. A number that
    # confident and that wrong is worse than no number, so this says so instead.
    tokens=""
fi

# 1000 rather than 1024: these are token counts, not bytes.
human_tokens() {
    if [ "$1" -ge 1000000 ] 2>/dev/null; then
        whole=$(( $1 / 1000000 )); tenth=$(( ($1 % 1000000) / 100000 ))
        if [ "$tenth" -eq 0 ]; then printf '%dM' "$whole"; else printf '%d.%dM' "$whole" "$tenth"; fi
    elif [ "$1" -ge 1000 ] 2>/dev/null; then
        printf '%dk' $(( ($1 + 500) / 1000 ))
    else
        printf '%d' "$1"
    fi
}

# muse-spark-1.3 is absent from the model catalog and /v1/models returns 401 here,
# so this window is not a fact from the provider — it is whatever
# CLAUDE_CODE_MAX_CONTEXT_TOKENS asserted. Lose that env var and the real 1M window
# is divided as 200k and every reading is five times too high, silently. Printing
# the denominator makes that visible, and colouring it makes it loud.
ctx_colour=""
if [ -z "$tokens" ]; then
    ctx_display="?"
    ctx_colour="\033[33m"
elif [ "$size" != "null" ] && [ "$size" -gt 0 ] 2>/dev/null; then
    pct=$((tokens * 100 / size))
    ctx_display="${pct}% $(human_tokens "$tokens")/$(human_tokens "$size")"
    if [ "$size" -lt 500000 ]; then
        # Far below muse-spark-1.3's real ~1M window: the assertion went missing.
        ctx_colour="\033[31m"
    fi
else
    ctx_display="-"
fi

# Get last path component (miloshadzic theme: %1~)
dir_display=$(basename "$current_dir")

# Get git info: worktree name + branch, or just branch, with dirty marker
if git -C "$current_dir" rev-parse --git-dir &>/dev/null; then
    git_dir=$(git -C "$current_dir" rev-parse --git-dir 2>/dev/null)
    branch=$(git -C "$current_dir" branch --show-current 2>/dev/null)
    [ -z "$branch" ] && branch="HEAD"

    # Dirty check (⚡ matches miloshadzic theme)
    if [ -n "$(git -C "$current_dir" --no-optional-locks status --porcelain 2>/dev/null)" ]; then
        dirty="⚡"
    else
        dirty=""
    fi

    if [[ "$git_dir" == *".git/worktrees/"* ]]; then
        # We're in a worktree - show worktree:branch
        worktree_name=$(basename "$git_dir")
        git_info="${worktree_name}:${branch}${dirty}"
    else
        # Main worktree - just show branch
        git_info="${branch}${dirty}"
    fi
else
    git_info=""
fi

# Proxy state for this session. ANTHROPIC_BASE_URL is inherited from the shell
# that launched claude, so it is the truth about this session rather than about
# what is currently on disk.
proxy_display=""
if [ -n "$ANTHROPIC_BASE_URL" ]; then
    case "$ANTHROPIC_BASE_URL" in
        *127.0.0.1:8787*|*localhost:8787*)
            if curl -fsS -m 1 http://127.0.0.1:8787/__health >/dev/null 2>&1; then
                proxy_display="proxied"
                proxy_colour="\033[32m"
            else
                # Pointed at the proxy but nothing is answering: every call fails.
                proxy_display="proxy down"
                proxy_colour="\033[31m"
            fi
            ;;
        *api.meta.ai*)
            proxy_display="direct — relaunch"
            proxy_colour="\033[31m"
            ;;
    esac
fi

# Colors
magenta="\033[35m"
yellow="\033[33m"
green="\033[32m"
cyan="\033[36m"
reset="\033[0m"

# Build status line: dir | model | ctx | git-branch | proxy
line="${cyan}%s${reset} | ${magenta}%s${reset} | ${ctx_colour:-$green}Ctx: %s${reset}"
args=("$dir_display" "$model_display" "$ctx_display")
if [ -n "$git_info" ]; then
    line="$line | ${green}%s${reset}"
    args+=("$git_info")
fi
if [ -n "$proxy_display" ]; then
    line="$line | ${proxy_colour}%s${reset}"
    args+=("$proxy_display")
fi
printf "$line" "${args[@]}"
