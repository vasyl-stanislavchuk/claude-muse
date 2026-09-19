# claude-muse — Claude Code's interface, running Meta's muse-spark-1.3.
#
# Deliberately thin. A shell function is a copy taken when the shell read this
# file, so anything that changes belongs in preflight.sh, which is read fresh on
# every launch. Identity lives in model-env.sh beside it, shared with the batch
# harness; only the two interactive-session knobs stay here. See docs/design.md.
claude-muse() {
  (
    source "$HOME/.config/claude-muse/model-env.sh"

    export ENABLE_TOOL_SEARCH="true"
    export CLAUDE_CODE_EFFORT_LEVEL="max"

    source "$HOME/.config/claude-muse/preflight.sh" || return 1

    exec claude "$@"
  )
}
