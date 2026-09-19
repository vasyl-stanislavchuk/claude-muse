# Identity env shared by the interactive function and the batch harness.
# Sourced, never executed. POSIX sh only: zsh and bash both read this file.
#
# Either of these would outrank the apiKeyHelper, so neither may reach claude.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

export CLAUDE_CONFIG_DIR="$HOME/.claude-profiles/muse"
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=3600000

export ANTHROPIC_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_OPUS_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_SONNET_MODEL="muse-spark-1.3"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="muse-spark-1.3"
export CLAUDE_CODE_SUBAGENT_MODEL="muse-spark-1.3"

# muse-spark-1.3 is not in Claude Code's model catalog, so without this it
# assumes 200k and auto-compacts at a fifth of the real window.
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000
