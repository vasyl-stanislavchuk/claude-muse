# claude-muse — Claude Code's interface, running Meta's muse-spark-1.3.
#
# A forwarder and nothing else. A shell function is a copy taken when the shell
# read this file, so everything that might change lives in the launcher it
# calls, which is read fresh on every launch. md and Switchboard run the same
# launcher, so a session you start here and one they open are one program.
# See docs/design.md.
claude-muse() {
  "$HOME/.config/claude-muse/claude-muse" "$@"
}
