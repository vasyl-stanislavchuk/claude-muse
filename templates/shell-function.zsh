# claude-muse — Claude Code's interface, running Meta's muse-spark-1.3.
#
# Deliberately thin. A shell function is a copy taken when the shell read this
# file, so anything that changes belongs in preflight.sh, which is read fresh on
# every launch. Only identity lives here: which profile, which model, how big the
# window is. See docs/design.md.
claude-muse() {
  (
    # Either of these would outrank the apiKeyHelper, so neither may reach claude.
    unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

    export CLAUDE_CONFIG_DIR="$HOME/.claude-profiles/muse"
    export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=3600000

    export ANTHROPIC_MODEL="muse-spark-1.3"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="muse-spark-1.3"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="muse-spark-1.3"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="muse-spark-1.3"
    export CLAUDE_CODE_SUBAGENT_MODEL="muse-spark-1.3"

    export ENABLE_TOOL_SEARCH="true"
    # muse-spark-1.3 is not in Claude Code's model catalog, so without this it
    # assumes 200k and auto-compacts at a fifth of the real window.
    export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
    export CLAUDE_CODE_EFFORT_LEVEL="max"

    source "$HOME/.config/claude-muse/preflight.sh" || return 1

    exec claude "$@"
  )
}
